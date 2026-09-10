"""Trusted read-tool registry for global AI model execution."""

from knowledge.models import KnowledgeDocument

from .models import AiConversation, AiFamilyOutboundAuthorization, AiOutboundAuthorization, AiProvider
from .read_tools import (
    GlobalAiReadError,
    knowledge_search,
    ledger_asset_snapshot,
    ledger_cashflow_budget,
    portfolio_account_snapshot,
    portfolio_accounts,
)


class GlobalAiRuntimeError(ValueError):
    """Raised before any unsafe or unsupported tool result leaves the host."""


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "ledger_asset_snapshot",
            "description": "读取最新或指定的正式账户资产快照；不读取投资模块。",
            "parameters": {
                "type": "object",
                "properties": {"snapshot_id": {"type": "integer", "minimum": 1}},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ledger_cashflow_budget",
            "description": (
                "读取账本中的年度收入、支出、月度与分类汇总；全家财务范围同时返回家庭年度预算执行情况。"
                "不读取资产快照或投资模块。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "year": {"type": "integer", "minimum": 2000, "maximum": 2100},
                },
                "required": ["year"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "portfolio_accounts",
            "description": "列出当前财务范围内可选择的投资账户；不读取账本余额。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "portfolio_account_snapshot",
            "description": "读取一个具体投资账户的持仓与估值；不读取账本对应账户余额。",
            "parameters": {
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer", "minimum": 1},
                    "snapshot_id": {"type": "integer", "minimum": 1},
                },
                "required": ["account_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "knowledge_search",
            "description": "检索当前成员有权读取的正式家庭知识资料，并返回精确版本依据。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 200},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirmed_memory",
            "description": "读取当前成员可用且已经人工确认的个人背景和家庭共同记录。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]


_ALLOWED_ARGUMENTS = {
    "ledger_asset_snapshot": {"snapshot_id"},
    "ledger_cashflow_budget": {"year"},
    "portfolio_accounts": set(),
    "portfolio_account_snapshot": {"account_id", "snapshot_id"},
    "knowledge_search": {"query", "limit"},
    "confirmed_memory": set(),
}
_FORBIDDEN_ARGUMENTS = {"actor", "member_id", "family_id", "scope", "financial_scope"}


def _validate_host_context(actor, conversation, provider):
    if (
        not actor
        or not actor.pk
        or not actor.is_active
        or not conversation
        or conversation.member_id != actor.pk
        or conversation.family_id != actor.family_id
        or conversation.is_archived
    ):
        raise GlobalAiRuntimeError("当前会话不可用于工具查询。")
    if not provider or not provider.pk or not provider.is_active:
        raise GlobalAiRuntimeError("AI 服务商不可用。")


def _validate_arguments(name, arguments):
    if name not in _ALLOWED_ARGUMENTS:
        raise GlobalAiRuntimeError("模型请求了未开放的工具。")
    if not isinstance(arguments, dict):
        raise GlobalAiRuntimeError("工具参数必须是对象。")
    keys = set(arguments)
    if keys & _FORBIDDEN_ARGUMENTS or not keys <= _ALLOWED_ARGUMENTS[name]:
        raise GlobalAiRuntimeError("模型不能指定成员或覆盖工具范围。")
    for key in ("snapshot_id", "account_id", "limit"):
        if key in arguments and (
            not isinstance(arguments[key], int)
            or isinstance(arguments[key], bool)
            or arguments[key] < 1
        ):
            raise GlobalAiRuntimeError("工具参数不可用。")
    if name == "ledger_cashflow_budget" and (
        not isinstance(arguments.get("year"), int)
        or isinstance(arguments.get("year"), bool)
        or not 2000 <= arguments["year"] <= 2100
    ):
        raise GlobalAiRuntimeError("收支分析年份不可用。")
    if name == "portfolio_account_snapshot" and "account_id" not in arguments:
        raise GlobalAiRuntimeError("投资账户参数不能为空。")
    if name == "knowledge_search":
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise GlobalAiRuntimeError("知识检索词不可用。")
        if arguments.get("limit", 5) > 10:
            raise GlobalAiRuntimeError("知识检索条数不可用。")


def _require_cloud_grants(
    provider,
    *,
    family_id,
    member_ids,
    data_type,
    family_financial_scope=False,
):
    if provider.execution_location != AiProvider.LOCATION_CLOUD:
        return
    if data_type == AiOutboundAuthorization.DATA_FINANCIAL and family_financial_scope:
        if AiFamilyOutboundAuthorization.objects.filter(
            family_id=family_id,
            provider=provider,
            data_type=AiFamilyOutboundAuthorization.DATA_FINANCIAL,
            is_allowed=True,
        ).exists():
            return
        raise GlobalAiRuntimeError("家庭管理员尚未允许向当前云端模型发送全家财务资料。")
    member_ids = set(member_ids)
    allowed = set(
        AiOutboundAuthorization.objects.filter(
            family_id=family_id,
            member_id__in=member_ids,
            provider=provider,
            data_type=data_type,
            is_allowed=True,
        ).values_list("member_id", flat=True)
    )
    if allowed != member_ids:
        raise GlobalAiRuntimeError("相关成员尚未授权把这类资料发送给当前云端模型。")


def _knowledge_for_cloud(provider, actor, result):
    if provider.execution_location != AiProvider.LOCATION_CLOUD:
        return result
    _require_cloud_grants(
        provider,
        family_id=actor.family_id,
        member_ids={actor.pk},
        data_type=AiOutboundAuthorization.DATA_KNOWLEDGE,
    )
    allowed_ids = set(
        KnowledgeDocument.objects.filter(
            pk__in=[item["document_id"] for item in result["results"]],
            source__allow_cloud_ai=True,
        ).values_list("pk", flat=True)
    )
    return {**result, "results": [item for item in result["results"] if item["document_id"] in allowed_ids]}


def dispatch_read_tool(actor, *, conversation, provider, name, arguments):
    """Execute one named tool while deriving identity and scope only from the host."""

    _validate_host_context(actor, conversation, provider)
    _validate_arguments(name, arguments)
    _require_cloud_grants(
        provider,
        family_id=actor.family_id,
        member_ids={actor.pk},
        data_type=AiOutboundAuthorization.DATA_CONVERSATION,
    )
    scope = conversation.financial_scope
    family_financial_scope = scope == AiConversation.SCOPE_FAMILY
    try:
        if name == "ledger_asset_snapshot":
            result = ledger_asset_snapshot(actor, scope=scope, **arguments)
            member_ids = {item["member_id"] for item in result["accounts"]}
            _require_cloud_grants(
                provider,
                family_id=actor.family_id,
                member_ids=member_ids,
                data_type=AiOutboundAuthorization.DATA_FINANCIAL,
                family_financial_scope=family_financial_scope,
            )
            references = [{"kind": "ledger_snapshot", "snapshot_id": result["snapshot_id"]}]
            data_type = AiOutboundAuthorization.DATA_FINANCIAL
        elif name == "ledger_cashflow_budget":
            result = ledger_cashflow_budget(actor, scope=scope, **arguments)
            _require_cloud_grants(
                provider,
                family_id=actor.family_id,
                member_ids=set(result["member_ids"]),
                data_type=AiOutboundAuthorization.DATA_FINANCIAL,
                family_financial_scope=family_financial_scope,
            )
            references = [{
                "kind": "ledger_cashflow_budget",
                "year": result["year"],
                "as_of_date": result["as_of_date"],
                "fingerprint": result["evidence_fingerprint"],
            }]
            data_type = AiOutboundAuthorization.DATA_FINANCIAL
        elif name == "portfolio_accounts":
            result = portfolio_accounts(actor, scope=scope)
            member_ids = {item["member_id"] for item in result["accounts"]}
            _require_cloud_grants(
                provider,
                family_id=actor.family_id,
                member_ids=member_ids,
                data_type=AiOutboundAuthorization.DATA_FINANCIAL,
                family_financial_scope=family_financial_scope,
            )
            references = [
                {"kind": "portfolio_account", "account_id": item["account_id"]}
                for item in result["accounts"]
            ]
            data_type = AiOutboundAuthorization.DATA_FINANCIAL
        elif name == "portfolio_account_snapshot":
            result = portfolio_account_snapshot(actor, scope=scope, **arguments)
            _require_cloud_grants(
                provider,
                family_id=actor.family_id,
                member_ids={result["member_id"]},
                data_type=AiOutboundAuthorization.DATA_FINANCIAL,
                family_financial_scope=family_financial_scope,
            )
            references = [{
                "kind": "portfolio_snapshot",
                "account_id": result["account_id"],
                "snapshot_id": result["snapshot_id"],
            }]
            data_type = AiOutboundAuthorization.DATA_FINANCIAL
        elif name == "knowledge_search":
            result = _knowledge_for_cloud(
                provider,
                actor,
                knowledge_search(actor, query=arguments["query"], limit=arguments.get("limit", 5)),
            )
            references = [
                {
                    "kind": "knowledge",
                    "document_id": item["document_id"],
                    "revision_id": item["revision_id"],
                }
                for item in result["results"]
            ]
            data_type = AiOutboundAuthorization.DATA_KNOWLEDGE
        else:
            from .global_ai_services import confirmed_memory_context

            result = {"module": "memory", "items": confirmed_memory_context(actor)}
            member_ids = {item["created_by_id"] for item in result["items"]}
            _require_cloud_grants(
                provider,
                family_id=actor.family_id,
                member_ids=member_ids,
                data_type=AiOutboundAuthorization.DATA_MEMORY,
            )
            references = [
                {"kind": "memory", "memory_id": item["memory_id"], "version": item["version"]}
                for item in result["items"]
            ]
            data_type = AiOutboundAuthorization.DATA_MEMORY
    except GlobalAiReadError as exc:
        raise GlobalAiRuntimeError(str(exc)) from exc
    return {
        "tool_name": name,
        "data_type": data_type,
        "result": result,
        "evidence_refs": references,
    }
