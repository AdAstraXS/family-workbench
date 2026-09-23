"""投研模块视图。

所有入口 login_required；每个请求自行验证有效成员（未绑定/停用 403，
即使超级用户被全局中间件放行也不能绕过）。写操作 CSRF + POST，
其余方法 405。写入只调用 services，不在视图中重复 ORM 写入。
异常顺序：ThesisRevisionConflict / DuplicateDossier 先于父类
ResearchValidationError；DossierNotFound 转 404。
"""
import logging
import traceback
from urllib.parse import urlencode

from ai_analysis.models import AiAnalysisRequest
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import F
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from .citations import locate_quote, resolve_quote
from .forms import CreateDossierForm, EditThesisForm, ExploreDossierForm, FirstThesisForm
from .models import (
    OfficialResearchContentVersion,
    OfficialResearchDocument,
    ResearchDossier,
    ResearchSourceState,
)
from .permissions import (
    accessible_dossiers,
    get_accessible_dossier_or_404,
    get_current_member,
    is_writer,
)
from .services import (
    DossierNotFound,
    DuplicateDossier,
    ResearchValidationError,
    ThesisRevisionConflict,
    create_dossier,
    create_exploration,
    save_first_thesis,
    save_thesis_revision,
)
from .providers.sec import SecClientError
from .research_ai import (
    MAX_DOCUMENT_CHARS, PROMPT_TEMPLATE_VERSION, ResearchAiError, available_research_providers,
    document_segments, generate_research_draft,
)
from .sec_content import fetch_sec_document_content
from .source_sync import sync_research_sources
from .tenk_chapters import tenk_chapter_coverage
from .tenk_financial_index import tenk_item8_index

PAGE_SIZE = 20
logger = logging.getLogger(__name__)


def _positive_id_or_404(raw):
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise Http404("无效的正文版本。") from exc
    if value <= 0:
        raise Http404("无效的正文版本。")
    return value


def _draft_selection_or_404(raw):
    try:
        version_raw, segment_raw = raw.split(":", 1)
        version_id = _positive_id_or_404(version_raw)
        segment_index = int(segment_raw)
    except (AttributeError, ValueError) as exc:
        raise Http404("无效的正文区段。") from exc
    if segment_index < 0 or str(segment_index) != segment_raw:
        raise Http404("无效的正文区段。")
    return version_id, segment_index


def _get_member_or_403(request):
    """每个请求自行验证有效成员；未绑定/停用 403。"""
    member = get_current_member(request)
    if member is None:
        return None
    return member


def _forbidden():
    return HttpResponseForbidden("当前账户尚未绑定有效家庭成员，或成员已停用。")


def _method(methods):
    """组合装饰器：方法白名单（405）→ 登录 → CSRF。"""

    def wrap(view):
        return require_http_methods(methods)(login_required(csrf_protect(view)))

    return wrap


@_method(["GET"])
def index(request):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossiers = accessible_dossiers(member).order_by("-updated_at", "-pk")
    paginator = Paginator(dossiers, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "investment_research/index.html",
        {"page": page, "can_write": is_writer(member)},
    )


@_method(["GET", "POST"])
def create(request):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    if not is_writer(member):
        return HttpResponseForbidden("查看者角色只能查看本人档案，不能创建或修改。")

    form = CreateDossierForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            dossier = create_dossier(
                actor=member,
                security=form.cleaned_data["security"],
                initial_thesis=form.cleaned_data["initial_thesis"],
                pillars=form.cleaned_data["pillars"],
                questions=form.cleaned_data["questions"],
            )
        except DuplicateDossier as exc:
            messages.info(request, "你已拥有该证券的研究档案，已为你打开。")
            return redirect(
                "investment_research:detail", pk=exc.dossier.pk
            )
        except ThesisRevisionConflict:
            # 创建路径理论上不触发；按统一顺序处理为 409。
            return render(
                request,
                "investment_research/create.html",
                {"form": form, "conflict": True},
                status=409,
            )
        except ResearchValidationError as exc:
            form = CreateDossierForm(request.POST)
            form.add_error(None, str(exc))
        else:
            messages.success(request, "研究档案已创建。")
            return redirect("investment_research:detail", pk=dossier.pk)
    return render(
        request, "investment_research/create.html", {"form": form}
    )


@_method(["GET", "POST"])
def explore(request):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    if not is_writer(member):
        return HttpResponseForbidden("查看者角色不能建立探索档案。")
    form = ExploreDossierForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            dossier = create_exploration(actor=member, security=form.cleaned_data["security"])
        except DuplicateDossier as exc:
            dossier = exc.dossier
            messages.info(request, "已有这家公司的研究档案，已为你打开。")
        else:
            messages.success(request, "已建立私密探索档案。可以先看资料，再形成自己的判断。")
        return redirect("investment_research:detail", pk=dossier.pk)
    return render(request, "investment_research/explore.html", {"form": form})


@_method(["GET", "POST"])
def first_thesis(request, pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    if not is_writer(member):
        return HttpResponseForbidden("查看者角色不能保存判断。")
    if dossier.current_revision_id and request.method == "GET":
        return redirect("investment_research:edit", pk=pk)
    form = FirstThesisForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            save_first_thesis(
                actor=member, dossier_id=pk, thesis=form.cleaned_data["thesis"],
                pillars=form.cleaned_data["pillars"], questions=form.cleaned_data["questions"],
            )
        except ThesisRevisionConflict:
            return render(request, "investment_research/first_thesis.html", {
                "dossier": dossier, "form": form, "conflict": True,
            }, status=409)
        except DossierNotFound:
            raise Http404
        except ResearchValidationError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "第一版正式判断已保存。")
            return redirect("investment_research:detail", pk=pk)
    return render(request, "investment_research/first_thesis.html", {"dossier": dossier, "form": form})


@_method(["GET"])
def detail(request, pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    documents = OfficialResearchDocument.objects.filter(security=dossier.security)
    source_states = list(
        ResearchSourceState.objects.filter(security=dossier.security).order_by("source")
    )
    successful_states = [state for state in source_states if state.last_success_at]
    latest_success_at = max(
        (state.last_success_at for state in successful_states), default=None
    )
    available_versions = list(
        OfficialResearchContentVersion.objects.filter(document__security=dossier.security, document__source="sec")
        .select_related("document").order_by("-fetched_at", "-pk")[:20]
    )
    completed_segments = set()
    if is_writer(member) and available_versions:
        for analysis in AiAnalysisRequest.objects.filter(
            member=member, family=member.family, module="investment_research",
            analysis_type="document_draft", status=AiAnalysisRequest.STATUS_SUCCESS,
        ).only("scope"):
            scope = analysis.scope or {}
            if scope.get("dossier_id") == dossier.pk and scope.get("version_id"):
                # 旧草稿只读取开头 16,000 字，没有 segment_index。
                completed_segments.add((scope["version_id"], scope.get("segment_index", 0)))
    research_groups = []
    for version in available_versions:
        segments = document_segments(version)
        chapters = tenk_chapter_coverage(
            version, (index for version_id, index in completed_segments if version_id == version.pk),
        )
        for segment in segments:
            segment["completed"] = (version.pk, segment["index"]) in completed_segments
            segment["chapter_codes"] = "、".join(
                chapter["code"] for chapter in chapters
                if chapter["located"] and chapter["start"] < segment["end"]
                and chapter["end"] > segment["start"]
            )
        research_groups.append({"version": version, "segments": segments})
    first_pending = next(
        (segment for group in research_groups for segment in group["segments"] if not segment["completed"]),
        None,
    )
    if first_pending is not None:
        first_pending["selected"] = True
    providers = available_research_providers() if is_writer(member) else []
    recent_drafts = [
        analysis for analysis in AiAnalysisRequest.objects.filter(
            member=member, module="investment_research", analysis_type="document_draft",
        ).select_related("provider").order_by("-created_at")[:30]
        if (analysis.scope or {}).get("dossier_id") == dossier.pk
    ][:5]
    return render(
        request,
        "investment_research/detail.html",
        {
            "dossier": dossier,
            "revision": dossier.current_revision,
            "can_write": is_writer(member),
            "document_count": documents.count(),
            "latest_source_success_at": latest_success_at,
            "source_error_count": sum(bool(state.last_error) for state in source_states),
            "available_versions": available_versions,
            "research_groups": research_groups,
            "research_providers": providers,
            "research_document_limit": MAX_DOCUMENT_CHARS,
            "recent_drafts": recent_drafts,
        },
    )


@_method(["POST"])
def sync_documents(request, pk):
    """手动同步当前档案证券的 SEC 与 Microsoft IR 官方资料。"""
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    if not is_writer(member):
        return HttpResponseForbidden("查看者角色只能查看本人档案，不能执行同步。")

    try:
        _, totals = sync_research_sources(
            symbols=[dossier.security.symbol],
        )
    except ValueError as exc:
        messages.error(request, f"官方资料同步未执行：{exc}")
    else:
        summary = (
            f"新增 {totals['created']} 份，更新 {totals['updated']} 份，"
            f"无变化 {totals['unchanged']} 份"
        )
        if totals["failed"]:
            messages.warning(
                request,
                f"官方资料同步完成，但有 {totals['failed']} 项失败；{summary}。"
                "请查看来源状态。",
            )
        elif totals["created"] or totals["updated"]:
            messages.success(request, f"官方资料已更新：{summary}。")
        else:
            messages.info(request, f"官方资料没有变化：{summary}。")
    return redirect("investment_research:documents", pk=dossier.pk)


@_method(["GET", "POST"])
def edit(request, pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    if not is_writer(member):
        return HttpResponseForbidden("查看者角色只能查看本人档案，不能创建或修改。")
    if dossier.current_revision_id is None:
        return redirect("investment_research:first_thesis", pk=pk)

    current = dossier.current_revision
    if request.method == "POST":
        form = EditThesisForm(request.POST)
        if form.is_valid():
            try:
                save_thesis_revision(
                    actor=member,
                    dossier_id=dossier.pk,
                    expected_revision_id=form.cleaned_data["expected_revision_id"],
                    thesis=form.cleaned_data["thesis"],
                    pillars=form.cleaned_data["pillars"],
                    questions=form.cleaned_data["questions"],
                    change_reason=form.cleaned_data["change_reason"],
                )
            except ThesisRevisionConflict:
                # 旧版本提交：保留用户输入与旧 expected_revision_id，
                # 不自动替换为最新版本；提示查看最新详情后重新编辑。
                return render(
                    request,
                    "investment_research/edit.html",
                    {
                        "dossier": dossier,
                        "form": form,
                        "conflict": True,
                    },
                    status=409,
                )
            except DuplicateDossier:
                # 编辑路径理论上不触发；按统一顺序在父类之前处理。
                form = EditThesisForm(request.POST)
                form.add_error(None, "你已拥有该证券的研究档案。")
            except DossierNotFound:
                # 并发删除等极端情况：统一按 404 处理。
                raise Http404
            except ResearchValidationError as exc:
                form = EditThesisForm(request.POST)
                form.add_error(None, str(exc))
            else:
                messages.success(request, "判断已保存为新版本。")
                return redirect("investment_research:detail", pk=dossier.pk)
    else:
        form = EditThesisForm(
            initial={
                "thesis": current.thesis if current else "",
                "pillars": "\n".join(current.pillars) if current else "",
                "questions": "\n".join(current.questions) if current else "",
                "change_reason": "",
                "expected_revision_id": current.pk if current else None,
            }
        )
    return render(
        request,
        "investment_research/edit.html",
        {"dossier": dossier, "form": form, "conflict": False},
    )


@_method(["GET"])
def history(request, pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    revisions = dossier.revisions.order_by("-revision_number", "-pk")
    paginator = Paginator(revisions, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "investment_research/history.html",
        {"dossier": dossier, "page": page},
    )


@_method(["GET"])
def documents(request, pk):
    """档案内官方资料列表；GET 只读，不触发任何来源同步。"""
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    queryset = OfficialResearchDocument.objects.filter(
        security=dossier.security
    ).order_by(F("published_at").desc(nulls_last=True), "-pk")
    page = Paginator(queryset, PAGE_SIZE).get_page(request.GET.get("page"))
    source_states = ResearchSourceState.objects.filter(
        security=dossier.security
    ).order_by("source")
    return render(
        request,
        "investment_research/documents.html",
        {
            "dossier": dossier,
            "page": page,
            "source_states": source_states,
            "can_write": is_writer(member),
        },
    )


@_method(["GET"])
def document_detail(request, pk, document_pk):
    """档案内官方资料正文；文档必须属于档案对应证券。"""
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    document = get_object_or_404(
        OfficialResearchDocument,
        pk=document_pk,
        security=dossier.security,
    )
    selected_version = None
    highlighted = None
    if "version" in request.GET:
        selected_version = get_object_or_404(
            OfficialResearchContentVersion,
            pk=_positive_id_or_404(request.GET["version"]), document=document,
        )
        citation_keys = ("start", "end", "hash")
        if any(key in request.GET for key in citation_keys):
            if not all(key in request.GET for key in citation_keys):
                raise Http404("引文参数不完整。")
            highlighted = resolve_quote(
                selected_version, request.GET["start"], request.GET["end"], request.GET["hash"],
            )
    elif any(key in request.GET for key in ("start", "end", "hash")):
        raise Http404("引用缺少正文版本。")
    current_version = document.content_versions.first() if document.source == "sec" else None
    chapter_version = selected_version or current_version
    chapter_coverage = []
    item8_statements = []
    item8_notes = []
    if chapter_version and document.document_type == "10-k":
        completed_indexes = []
        for analysis in AiAnalysisRequest.objects.filter(
            member=member, family=member.family, module="investment_research",
            analysis_type="document_draft", status=AiAnalysisRequest.STATUS_SUCCESS,
        ).only("scope"):
            scope = analysis.scope or {}
            if scope.get("dossier_id") == dossier.pk and scope.get("version_id") == chapter_version.pk:
                completed_indexes.append(scope.get("segment_index", 0))
        chapter_coverage = tenk_chapter_coverage(chapter_version, completed_indexes)
        item8_entries = tenk_item8_index(chapter_version, chapter_coverage)
        item8_statements = [entry for entry in item8_entries if entry["kind"] == "statement"]
        item8_notes = [entry for entry in item8_entries if entry["kind"] == "note"]
    return render(
        request,
        "investment_research/document_detail.html",
        {
            "dossier": dossier, "document": document,
            "can_write": is_writer(member),
            "can_fetch_sec": document.source == "sec" and document.document_type in {"10-k", "10-q", "8-k"},
            "current_content_version": current_version,
            "selected_version": selected_version,
            "highlighted": highlighted,
            "chapter_version": chapter_version,
            "chapter_coverage": chapter_coverage,
            "item8_statements": item8_statements,
            "item8_notes": item8_notes,
        },
    )


@_method(["POST"])
def create_citation(request, pk, document_pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    document = get_object_or_404(OfficialResearchDocument, pk=document_pk, security=dossier.security)
    version = get_object_or_404(
        OfficialResearchContentVersion,
        pk=_positive_id_or_404(request.POST.get("version")), document=document,
    )
    try:
        start, end, digest = locate_quote(version, request.POST.get("quote"))
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("investment_research:document_detail", pk=pk, document_pk=document_pk)
    url = reverse("investment_research:document_detail", args=[pk, document_pk])
    return redirect(f"{url}?{urlencode({'version': version.pk, 'start': start, 'end': end, 'hash': digest})}")


@_method(["POST"])
def fetch_sec_content(request, pk, document_pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    document = get_object_or_404(OfficialResearchDocument, pk=document_pk, security=dossier.security)
    if not is_writer(member):
        return HttpResponseForbidden("查看者角色不能提取正文。")
    try:
        _, created = fetch_sec_document_content(
            actor=member, dossier_id=dossier.pk, document_id=document.pk,
        )
    except (ResearchValidationError, SecClientError) as exc:
        messages.error(request, f"正文提取失败：{exc}")
    else:
        messages.success(request, "SEC 正文已保存。" if created else "SEC 正文没有变化。")
    return redirect("investment_research:document_detail", pk=pk, document_pk=document_pk)


@_method(["POST"])
def generate_draft(request, pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    if not is_writer(member):
        return HttpResponseForbidden("查看者角色不能发起 AI 分析。")
    try:
        version_id, segment_index = _draft_selection_or_404(request.POST.get("selection"))
        analysis = generate_research_draft(
            actor=member, dossier_id=dossier.pk,
            version_id=version_id, segment_index=segment_index,
            provider_id=_positive_id_or_404(request.POST.get("provider")),
            consent=request.POST.get("one_time_consent") == "yes",
        )
    except DossierNotFound:
        raise Http404
    except ResearchAiError as exc:
        messages.error(request, str(exc))
        return redirect("investment_research:detail", pk=pk)
    except Exception as exc:
        # 生产错误只记类型和调用位置，不把正文、判断、请求体或异常消息写入日志。
        cause = getattr(exc, "__cause__", None)
        diag = getattr(cause, "diag", None)
        locations = " -> ".join(
            f"{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno}:{frame.name}"
            for frame in traceback.extract_tb(exc.__traceback__)[-8:]
        )
        logger.error(
            "研究草稿意外失败：%s；SQLSTATE=%s；约束=%s；列=%s；位置：%s",
            type(exc).__name__, getattr(cause, "sqlstate", None),
            getattr(diag, "constraint_name", None), getattr(diag, "column_name", None), locations,
        )
        messages.error(request, "草稿生成失败，未自动重试。请稍后再试。")
        return redirect("investment_research:detail", pk=pk)
    return redirect("investment_research:draft_detail", pk=pk, request_pk=analysis.pk)


@_method(["GET"])
def draft_detail(request, pk, request_pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    analysis = get_object_or_404(
        AiAnalysisRequest.objects.select_related("provider"),
        pk=request_pk, member=member, family=member.family,
        module="investment_research", analysis_type="document_draft",
    )
    if (analysis.scope or {}).get("dossier_id") != dossier.pk:
        raise Http404
    fetched_at_raw = (analysis.scope or {}).get("content_fetched_at")
    fetched_at = parse_datetime(fetched_at_raw) if isinstance(fetched_at_raw, str) else None
    result = analysis.result if analysis.status == AiAnalysisRequest.STATUS_SUCCESS else None
    rendered = None
    if result:
        rendered = dict(result.result_json)
        for field in ("supports", "weakens"):
            rendered[field] = [dict(item) for item in rendered.get(field, [])]
            for item in rendered[field]:
                item["citations"] = [dict(citation) for citation in item.get("citations", [])]
                for citation in item["citations"]:
                    citation["url"] = (
                        reverse("investment_research:document_detail", args=[pk, analysis.scope["document_id"]])
                        + "?" + urlencode({"version": citation["version_id"], "start": citation["start"],
                                           "end": citation["end"], "hash": citation["hash"]})
                    )
    return render(request, "investment_research/draft_detail.html", {
        "dossier": dossier, "analysis": analysis, "draft": rendered,
        "content_fetched_at": fetched_at,
        "amount_guard_applied": (analysis.scope or {}).get("prompt_version") == PROMPT_TEMPLATE_VERSION,
    })
