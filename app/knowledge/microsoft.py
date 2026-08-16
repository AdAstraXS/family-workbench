import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

import msal
from django.conf import settings
from django.utils import timezone

from .crypto import TokenDecryptionError
from .models import SourceConnection


logger = logging.getLogger(__name__)
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
ALLOWED_MICROSOFT_HOSTS = {
    "graph.microsoft.com",
    "www.onenote.com",
    "onenote.com",
}


class MicrosoftKnowledgeError(RuntimeError):
    pass


class MicrosoftConfigurationError(MicrosoftKnowledgeError):
    pass


class MicrosoftAuthorizationError(MicrosoftKnowledgeError):
    pass


class MicrosoftSourceUnavailableError(MicrosoftKnowledgeError):
    pass


def microsoft_is_configured():
    return bool(
        settings.KNOWLEDGE_MICROSOFT_CLIENT_ID
        and settings.KNOWLEDGE_MICROSOFT_CLIENT_SECRET
    )


def _configuration():
    if not microsoft_is_configured():
        raise MicrosoftConfigurationError(
            "OneNote 授权尚未配置，请设置 Microsoft 应用 Client ID 和 Client Secret。"
        )
    tenant = settings.KNOWLEDGE_MICROSOFT_TENANT.strip() or "common"
    if not all(character.isalnum() or character in ".-_" for character in tenant):
        raise MicrosoftConfigurationError("Microsoft Tenant 配置格式不正确。")
    return {
        "client_id": settings.KNOWLEDGE_MICROSOFT_CLIENT_ID,
        "client_secret": settings.KNOWLEDGE_MICROSOFT_CLIENT_SECRET,
        "authority": f"https://login.microsoftonline.com/{tenant}",
        "scopes": list(settings.KNOWLEDGE_MICROSOFT_SCOPES),
    }


def _build_app(serialized_cache=""):
    config = _configuration()
    cache = msal.SerializableTokenCache()
    if serialized_cache:
        cache.deserialize(serialized_cache)
    app = msal.ConfidentialClientApplication(
        config["client_id"],
        authority=config["authority"],
        client_credential=config["client_secret"],
        token_cache=cache,
    )
    return app, cache, config


def start_authorization_flow(redirect_uri):
    app, cache, config = _build_app()
    flow = app.initiate_auth_code_flow(
        scopes=config["scopes"],
        redirect_uri=redirect_uri,
    )
    if "auth_uri" not in flow:
        raise MicrosoftAuthorizationError(
            flow.get("error_description") or "Microsoft 未返回可用的授权地址。"
        )
    return flow


def finish_authorization_flow(flow, query_parameters):
    app, cache, _ = _build_app()
    try:
        result = app.acquire_token_by_auth_code_flow(flow, query_parameters)
    except ValueError as exc:
        raise MicrosoftAuthorizationError("Microsoft 授权状态校验失败，请重新绑定。") from exc
    if "access_token" not in result:
        raise MicrosoftAuthorizationError(
            result.get("error_description")
            or result.get("error")
            or "Microsoft 授权未完成。"
        )
    return result, cache.serialize()


def _safe_absolute_url(value):
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_MICROSOFT_HOSTS:
        raise MicrosoftSourceUnavailableError("Microsoft 返回了不受信任的资源地址。")
    if parsed.username or parsed.password:
        raise MicrosoftSourceUnavailableError("Microsoft 资源地址包含不允许的认证信息。")
    return urllib.parse.urlunsplit(parsed)


@dataclass
class GraphResponse:
    body: bytes
    content_type: str
    content_disposition: str


class MicrosoftGraphClient:
    def __init__(self, connection):
        self.connection = connection
        try:
            serialized_cache = connection.get_token_cache()
        except TokenDecryptionError as exc:
            self._mark_error(str(exc))
            raise MicrosoftAuthorizationError(str(exc)) from exc
        self.app, self.cache, self.config = _build_app(serialized_cache)

    def _mark_error(self, message):
        SourceConnection.objects.filter(pk=self.connection.pk).update(
            status=SourceConnection.STATUS_ERROR,
            last_error=str(message)[:2000],
            last_used_at=timezone.now(),
        )

    def _persist_cache(self):
        if not self.cache.has_state_changed:
            return
        self.connection.set_token_cache(self.cache.serialize())
        self.connection.save(update_fields=["encrypted_token_cache", "updated_at"])

    def access_token(self):
        accounts = self.app.get_accounts()
        if not accounts:
            message = "Microsoft 登录已失效，请成员重新绑定账户。"
            self._mark_error(message)
            raise MicrosoftAuthorizationError(message)
        result = self.app.acquire_token_silent(
            scopes=self.config["scopes"],
            account=accounts[0],
        )
        self._persist_cache()
        if not result or "access_token" not in result:
            message = (
                (result or {}).get("error_description")
                or "Microsoft 登录已过期或授权已撤销，请重新绑定。"
            )
            self._mark_error(message)
            raise MicrosoftAuthorizationError(message)
        return result["access_token"]

    def request(self, url_or_path, *, max_bytes=None):
        url = (
            _safe_absolute_url(url_or_path)
            if str(url_or_path).startswith("http")
            else _safe_absolute_url(f"{GRAPH_ROOT}/{str(url_or_path).lstrip('/')}")
        )
        limit = max_bytes or settings.KNOWLEDGE_MAX_RESOURCE_BYTES
        for attempt in range(3):
            request = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {self.access_token()}",
                    "Accept": "*/*",
                    "User-Agent": "FamilyWorkbenchKnowledge/1.0",
                },
                method="GET",
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    content_length = response.headers.get("Content-Length")
                    if content_length and int(content_length) > limit:
                        raise MicrosoftSourceUnavailableError(
                            f"资源超过单文件大小限制（{limit} 字节）。"
                        )
                    body = response.read(limit + 1)
                    if len(body) > limit:
                        raise MicrosoftSourceUnavailableError(
                            f"资源超过单文件大小限制（{limit} 字节）。"
                        )
                    SourceConnection.objects.filter(pk=self.connection.pk).update(
                        status=SourceConnection.STATUS_ACTIVE,
                        last_used_at=timezone.now(),
                        last_success_at=timezone.now(),
                        last_error="",
                    )
                    return GraphResponse(
                        body=body,
                        content_type=(
                            response.headers.get_content_type()
                            or "application/octet-stream"
                        ),
                        content_disposition=response.headers.get(
                            "Content-Disposition",
                            "",
                        ),
                    )
            except urllib.error.HTTPError as exc:
                retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                if exc.code in {429, 500, 502, 503, 504} and attempt < 2:
                    delay = min(int(retry_after), 10) if retry_after.isdigit() else 2**attempt
                    time.sleep(delay)
                    continue
                if exc.code in {401, 403}:
                    message = "Microsoft 授权已失效或无权访问该 OneNote 内容。"
                    self._mark_error(message)
                    raise MicrosoftAuthorizationError(message) from exc
                message = f"Microsoft Graph 请求失败（HTTP {exc.code}）。"
                self._mark_error(message)
                raise MicrosoftSourceUnavailableError(message) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < 2:
                    time.sleep(2**attempt)
                    continue
                message = f"Microsoft Graph 暂时不可访问：{exc}"
                self._mark_error(message)
                raise MicrosoftSourceUnavailableError(message) from exc
        raise MicrosoftSourceUnavailableError("Microsoft Graph 请求失败。")

    def get_json(self, url_or_path):
        response = self.request(url_or_path, max_bytes=10 * 1024 * 1024)
        try:
            return json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MicrosoftSourceUnavailableError(
                "Microsoft Graph 返回了无法解析的数据。"
            ) from exc

    def paged_values(self, url_or_path):
        next_url = url_or_path
        while next_url:
            payload = self.get_json(next_url)
            values = payload.get("value")
            if not isinstance(values, list):
                raise MicrosoftSourceUnavailableError(
                    "Microsoft Graph 分页结果缺少 value 列表。"
                )
            yield from values
            next_url = payload.get("@odata.nextLink", "")

    def profile(self):
        return self.get_json(
            "/me?$select=id,displayName,mail,userPrincipalName"
        )

    def notebooks(self):
        path = (
            "/me/onenote/notebooks"
            "?$top=100&$select=id,displayName,createdDateTime,"
            "lastModifiedDateTime,links,self"
        )
        return list(self.paged_values(path))

    def sections_for_notebook(self, notebook_id):
        path = (
            "/me/onenote/sections"
            "?$top=100&$select=id,displayName,createdDateTime,"
            "lastModifiedDateTime,self"
            "&$expand=parentNotebook($select=id,displayName),"
            "parentSectionGroup($select=id,displayName)"
        )
        sections = []
        for section in self.paged_values(path):
            parent = section.get("parentNotebook") or {}
            if str(parent.get("id")) == str(notebook_id):
                sections.append(section)
        return sections

    def pages_for_section(self, section_id):
        encoded = urllib.parse.quote(str(section_id), safe="")
        path = (
            f"/me/onenote/sections/{encoded}/pages"
            "?$top=100&pagelevel=true"
            "&$select=id,title,createdDateTime,lastModifiedDateTime,"
            "contentUrl,links,level,order"
        )
        return list(self.paged_values(path))

    def page_content(self, page_id):
        encoded = urllib.parse.quote(str(page_id), safe="")
        return self.request(
            f"/me/onenote/pages/{encoded}/content?includeIDs=true",
            max_bytes=10 * 1024 * 1024,
        ).body

    def resource(self, absolute_url):
        return self.request(absolute_url)


def safe_notebook_cache(notebooks):
    safe = []
    for notebook in notebooks:
        links = notebook.get("links") or {}
        web_link = (links.get("oneNoteWebUrl") or {}).get("href", "")
        client_link = (links.get("oneNoteClientUrl") or {}).get("href", "")
        safe.append(
            {
                "id": str(notebook.get("id", "")),
                "displayName": str(notebook.get("displayName", ""))[:300],
                "createdDateTime": notebook.get("createdDateTime"),
                "lastModifiedDateTime": notebook.get("lastModifiedDateTime"),
                "webUrl": str(web_link)[:1000],
                "clientUrl": str(client_link)[:1000],
            }
        )
    return [item for item in safe if item["id"] and item["displayName"]]
