"""投研模块视图。

所有入口 login_required；每个请求自行验证有效成员（未绑定/停用 403，
即使超级用户被全局中间件放行也不能绕过）。写操作 CSRF + POST，
其余方法 405。写入只调用 services，不在视图中重复 ORM 写入。
异常顺序：ThesisRevisionConflict / DuplicateDossier 先于父类
ResearchValidationError；DossierNotFound 转 404。
"""
import logging
import traceback
from datetime import timedelta
from urllib.parse import urlencode

from ai_analysis.models import AiAnalysisRequest
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import F, Q, OuterRef, Subquery
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from .citations import locate_quote, resolve_quote
from .official_ir import company_security, documents_for_security, ir_coverage
from .providers.ir_registry import BY_KEY, company_for_security
from .forms import (
    CreateDossierForm, EditThesisForm, ExploreDossierForm, FilingReviewForm, FirstThesisForm,
)
from .filing_review import FilingReviewConflict, reviewable_filings, save_filing_review
from .models import (
    OfficialResearchContentVersion,
    OfficialResearchDocument,
    ResearchDossier,
    ResearchFilingReview,
    ResearchReviewPlan,
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
from .tenk_history import fill_tenk_history
from .tenk_metrics import BUSINESS_CALC_CODES, HISTORICAL_LEASE_CODES, tenk_metric_grid
from .metric_focus import CORE_CODES, generate_metric_suggestions, save_metric_focus
from .review_plan import (
    confirm_review_plan, generate_review_plan, latest_plan_source, plan_context,
)

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
    for dossier in page.object_list:
        _, review_items = reviewable_filings(dossier)
        dossier.pending_review_count = sum(item["pending"] for item in review_items)
        dossier.needs_revision_count = sum(item["needs_revision"] for item in review_items)
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
            security = (company_security(BY_KEY[form.cleaned_data['company']])
                        if form.cleaned_data.get('company') else form.cleaned_data['security'])
            dossier = create_exploration(actor=member, security=security)
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
    documents = documents_for_security(dossier.security)
    source_states = list(
        ResearchSourceState.objects.filter(security=dossier.security).order_by("source")
    )
    successful_states = [state for state in source_states if state.last_success_at]
    latest_success_at = max(
        (state.last_success_at for state in successful_states), default=None
    )
    available_versions = list(
        OfficialResearchContentVersion.objects.filter(document__in=documents)
        .filter(pk=Subquery(OfficialResearchContentVersion.objects.filter(document_id=OuterRef('document_id')).order_by('-version_number').values('pk')[:1]))
        .exclude(content_text='')
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
    review_start_date, review_items = reviewable_filings(dossier)
    current_plan = (ResearchReviewPlan.objects.filter(
        dossier=dossier, thesis_revision_id=dossier.current_revision_id,
    ).first() if dossier.current_revision_id else None)
    return render(
        request,
        "investment_research/detail.html",
        {
            "dossier": dossier,
            "revision": dossier.current_revision,
            "can_write": is_writer(member),
            "document_count": documents.count(),
            "ir_company": company_for_security(dossier.security),
            "latest_source_success_at": latest_success_at,
            "source_error_count": sum(bool(state.last_error) for state in source_states),
            "available_versions": available_versions,
            "research_groups": research_groups,
            "research_providers": providers,
            "research_document_limit": MAX_DOCUMENT_CHARS,
            "recent_drafts": recent_drafts,
            "selected_metric_count": len(dossier.selected_metric_codes or []),
            "review_start_date": review_start_date,
            "review_items": [item for item in review_items
                             if item["pending"] or item["needs_revision"]][:5],
            "pending_review_count": sum(item["pending"] for item in review_items),
            "needs_revision_count": sum(item["needs_revision"] for item in review_items),
            "current_plan": current_plan,
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
            sources=['sec'],
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
def filing_reviews(request, pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    start_date, items = reviewable_filings(dossier)
    return render(request, "investment_research/filing_reviews.html", {
        "dossier": dossier, "start_date": start_date, "items": items,
        "can_write": is_writer(member),
    })


@_method(["GET", "POST"])
def review_plan(request, pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    if request.method == "POST" and not is_writer(member):
        return HttpResponseForbidden("查看者角色不能生成或确认复核计划。")
    version = latest_plan_source(dossier)
    if request.method == "POST":
        try:
            if request.POST.get("action") == "generate":
                if version is None:
                    raise ResearchValidationError("请先保存最新 10-K 正文。")
                generate_review_plan(
                    actor=member, dossier_id=dossier.pk, version_id=version.pk,
                    provider_id=_positive_id_or_404(request.POST.get("provider")),
                    consent=request.POST.get("one_time_consent") == "yes",
                )
                messages.success(request, "AI 复核计划草稿已生成；请逐项核对后再确认。")
            elif request.POST.get("action") == "confirm":
                try:
                    indexes = [int(value) for value in request.POST.getlist("selected_indexes")]
                except ValueError as exc:
                    raise ResearchValidationError("复核计划条目编号无效。") from exc
                confirm_review_plan(
                    actor=member, dossier_id=dossier.pk,
                    analysis_id=_positive_id_or_404(request.POST.get("analysis_id")),
                    selected_indexes=indexes,
                )
                messages.success(request, "已确认的复核计划保存到当前判断版本。")
            else:
                raise Http404
        except DossierNotFound:
            raise Http404
        except (ResearchValidationError, ResearchAiError) as exc:
            messages.error(request, str(exc))
        return redirect("investment_research:review_plan", pk=pk)
    revision = dossier.current_revision
    confirmed = (ResearchReviewPlan.objects.filter(
        dossier=dossier, thesis_revision=revision,
    ).select_related("source_analysis").first() if revision else None)
    latest = next((analysis for analysis in AiAnalysisRequest.objects.filter(
        member=member, family=member.family, module="investment_research",
        analysis_type="next_filing_plan", status=AiAnalysisRequest.STATUS_SUCCESS,
    ).order_by("-created_at", "-pk")[:30]
        if (analysis.scope or {}).get("dossier_id") == dossier.pk and
        (analysis.scope or {}).get("thesis_revision_id") == getattr(revision, "pk", None) and
        (analysis.scope or {}).get("version_id") == getattr(version, "pk", None) and
        (analysis.scope or {}).get("selected_metric_codes") == (dossier.selected_metric_codes or [])), None)

    def linked_items(items, document_id):
        rendered = []
        for index, item in enumerate(items):
            citations = [{**citation, "url": (
                reverse("investment_research:document_detail", args=[pk, document_id])
                + "?" + urlencode({"version": citation["version_id"],
                                   "start": citation["start"], "end": citation["end"],
                                   "hash": citation["hash"]}) + "#research-citation")}
                for citation in item.get("citations", [])]
            rendered.append({**item, "item_index": index, "citations": citations})
        return rendered

    suggestions = (linked_items(latest.result.result_json.get("items", []),
                                latest.scope["document_id"]) if latest else [])
    confirmed_items = (linked_items(confirmed.items,
                                    confirmed.source_analysis.scope["document_id"])
                       if confirmed else [])
    metrics, evidence, source_problem = {}, [], ""
    if version and revision:
        try:
            metrics, evidence = plan_context(dossier, version)
        except ResearchAiError as exc:
            source_problem = str(exc)
    return render(request, "investment_research/review_plan.html", {
        "dossier": dossier, "revision": revision, "version": version,
        "metrics": metrics, "evidence": evidence, "source_problem": source_problem,
        "latest": latest, "suggestions": suggestions,
        "confirmed": confirmed, "confirmed_items": confirmed_items,
        "can_write": is_writer(member),
        "providers": available_research_providers() if is_writer(member) else [],
    })


@_method(["GET", "POST"])
def filing_review(request, pk, document_pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    if dossier.current_revision_id is None:
        return redirect("investment_research:first_thesis", pk=pk)
    _, items = reviewable_filings(dossier)
    item = next((value for value in items if value["document"].pk == document_pk), None)
    if item is None:
        raise Http404("这份资料不在当前档案的新财报清单中。")
    document = item["document"]
    version = item["version"]
    if request.method == "POST" and not is_writer(member):
        return HttpResponseForbidden("查看者角色不能保存财报复核。")
    if request.method == "POST" and version is None:
        raise Http404("请先保存这份财报的正文。")
    revision = dossier.current_revision
    current_plan = ResearchReviewPlan.objects.filter(
        dossier=dossier, thesis_revision=revision,
    ).first()
    form = None
    if version and is_writer(member):
        form = FilingReviewForm(
            revision, request.POST if request.method == "POST" else None,
            initial={"expected_revision_id": revision.pk, "version_id": version.pk},
        )
        if request.method == "POST" and form.is_valid():
            try:
                save_filing_review(
                    actor=member, dossier_id=dossier.pk, document_id=document.pk,
                    version_id=form.cleaned_data["version_id"],
                    expected_revision_id=form.cleaned_data["expected_revision_id"],
                    assessments=form.cleaned_assessments(),
                    outcome=form.cleaned_data["outcome"], action=form.cleaned_data["action"],
                    summary=form.cleaned_data["summary"],
                    follow_up=form.cleaned_data["follow_up"], quote=form.cleaned_data["quote"],
                )
            except FilingReviewConflict as exc:
                form.add_error(None, str(exc))
                return render(request, "investment_research/filing_review.html", {
                    "dossier": dossier, "document": document, "version": version,
                    "revision": revision, "form": form,
                    "assessment_fields": [{"text": text, "status": form[status_name],
                                           "note": form[note_name]}
                                          for _, text, status_name, note_name in form.assessment_fields],
                    "prior_reviews": [], "can_write": True,
                    "needs_revision": item["needs_revision"],
                    "current_plan": current_plan,
                }, status=409)
            except DossierNotFound:
                raise Http404
            except ResearchValidationError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, "本次财报复核已保存。旧记录和原文引用会保留。")
                return redirect("investment_research:filing_review", pk=pk,
                                document_pk=document.pk)
    prior_reviews = ResearchFilingReview.objects.filter(
        dossier=dossier, document=document,
    ).select_related("thesis_revision", "content_version").order_by("-created_at", "-pk")
    return render(request, "investment_research/filing_review.html", {
        "dossier": dossier, "document": document, "version": version,
        "revision": revision, "form": form,
        "assessment_fields": ([{"text": text, "status": form[status_name],
                                "note": form[note_name]}
                               for _, text, status_name, note_name in form.assessment_fields]
                              if form else []),
        "prior_reviews": prior_reviews, "can_write": is_writer(member),
        "needs_revision": item["needs_revision"],
        "current_plan": current_plan,
    })


@_method(["GET"])
def documents(request, pk):
    """档案内官方资料列表；GET 只读，不触发任何来源同步。"""
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    queryset = documents_for_security(dossier.security).order_by(F("published_at").desc(nulls_last=True), "-pk")
    selected_source = request.GET.get('source', '')
    if selected_source in ('sec', 'official_ir'):
        queryset = queryset.filter(source__in=['official_ir', 'microsoft_ir'] if selected_source == 'official_ir' else ['sec'])
    selected_period = request.GET.get('period', '')
    if len(selected_period) == 6 and selected_period[:4].isdigit() and selected_period[4] == '-' and selected_period[5] in '1234':
        queryset = queryset.filter(metadata__fiscal_year=int(selected_period[:4]), metadata__quarter=int(selected_period[5]))
    else:
        selected_period = ''
    if selected_source == 'official_ir' or selected_period:
        queryset = queryset.order_by(F('metadata__fiscal_year').desc(nulls_last=True),
                                     F('metadata__quarter').desc(nulls_last=True), '-pk')
    company, coverage, warnings = ir_coverage(dossier.security)
    page = Paginator(queryset, PAGE_SIZE).get_page(request.GET.get("page"))
    source_query = Q(security=dossier.security)
    if company:
        source_query |= Q(source='official_ir', external_company_id=company.key)
    source_states = ResearchSourceState.objects.filter(source_query).order_by('source')
    return render(
        request,
        "investment_research/documents.html",
        {
            "dossier": dossier,
            "page": page,
            "source_states": source_states,
            "ir_company": company, "ir_coverage": coverage, "ir_warnings": warnings,
            "selected_source": selected_source,
            "selected_period": selected_period,
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
    document = get_object_or_404(documents_for_security(dossier.security), pk=document_pk)
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
    current_version = document.content_versions.first()
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
            "can_fetch_ir": document.source in ('official_ir', 'microsoft_ir') and bool(company_for_security(document.security)),
            "current_content_version": current_version,
            "selected_version": selected_version,
            "highlighted": highlighted,
            "chapter_version": chapter_version,
            "chapter_coverage": chapter_coverage,
            "item8_statements": item8_statements,
            "item8_notes": item8_notes,
        },
    )


@_method(["GET"])
def document_metrics(request, pk, document_pk):
    """按已保存的 10-K 正文版本核对 iXBRL，不触发 SEC 请求或写库。"""
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    document = get_object_or_404(
        OfficialResearchDocument, pk=document_pk, security=dossier.security,
        source="sec", document_type="10-k",
    )
    if "version" in request.GET:
        version = get_object_or_404(
            OfficialResearchContentVersion,
            pk=_positive_id_or_404(request.GET["version"]), document=document,
        )
    else:
        version = document.content_versions.first()
    historical_documents = (
        OfficialResearchDocument.objects.filter(
            security=dossier.security, source="sec", document_type="10-k",
            period_end__lt=document.period_end,
            period_end__gte=document.period_end - timedelta(days=900),
        ).exclude(pk=document.pk).prefetch_related("content_versions")
        if version and document.period_end else ()
    )
    periods, rows, problem = (
        tenk_metric_grid(version, historical_documents)
        if version else ([], [], "请先提取这份 10-K 的正文。")
    )
    selected_codes = set(dossier.selected_metric_codes or [])
    visible_codes = CORE_CODES | selected_codes
    if "company_revenue" in selected_codes:
        visible_codes |= BUSINESS_CALC_CODES
    rows = [row for row in rows if row["code"] in visible_codes]
    return render(request, "investment_research/document_metrics.html", {
        "dossier": dossier, "document": document, "version": version,
        "periods": periods, "rows": rows, "problem": problem,
        "selected_metric_count": len(selected_codes),
        "can_fill_history": is_writer(member) and any(
            row["code"] in HISTORICAL_LEASE_CODES and any(
                cell["status"] in {"该年 10-K 尚未归档", "该年 10-K 正文未保存"}
                for cell in row["cells"][1:]
            ) for row in rows
        ),
    })


@_method(["GET", "POST"])
def metric_focus(request, pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    if request.method == "POST" and not is_writer(member):
        return HttpResponseForbidden("查看者角色不能修改追踪指标。")
    version = (OfficialResearchContentVersion.objects.filter(
        document__security=dossier.security, document__source="sec",
        document__document_type="10-k",
    ).select_related("document", "document__security")
               .order_by("-document__period_end", "-fetched_at", "-pk").first())
    if request.method == "POST":
        if version is None:
            raise Http404
        try:
            if request.POST.get("action") == "save":
                save_metric_focus(actor=member, dossier_id=dossier.pk,
                                  version_id=version.pk, codes=request.POST.getlist("codes"))
                messages.success(request, "追踪指标已保存。")
            elif request.POST.get("action") == "generate":
                generate_metric_suggestions(
                    actor=member, dossier_id=dossier.pk, version_id=version.pk,
                    provider_id=_positive_id_or_404(request.POST.get("provider")),
                    consent=request.POST.get("one_time_consent") == "yes",
                )
                messages.success(request, "AI 候选指标已生成；请核对原文后手动保存。")
            else:
                raise Http404
        except DossierNotFound:
            raise Http404
        except (ResearchValidationError, ResearchAiError) as exc:
            messages.error(request, str(exc))
        return redirect("investment_research:metric_focus", pk=pk)
    choices = []
    if version:
        _, rows, problem = tenk_metric_grid(version)
        if not problem:
            selected = set(dossier.selected_metric_codes or [])
            choices = [{"code": row["code"], "label": row["label"],
                        "checked": row["code"] in selected,
                        "available": bool(row["cells"][0].get("citation")),
                        "includes_margin": row["code"] == "company_revenue" and
                        any(item["code"] == "company_cost" for item in rows)}
                       for row in rows if row["code"] not in CORE_CODES | BUSINESS_CALC_CODES]
        else:
            messages.warning(request, problem)
    latest = next((analysis for analysis in AiAnalysisRequest.objects.filter(
        member=member, family=member.family, module="investment_research",
        analysis_type="metric_focus", status=AiAnalysisRequest.STATUS_SUCCESS,
    ).order_by("-created_at")[:30]
        if (analysis.scope or {}).get("dossier_id") == dossier.pk and
        (analysis.scope or {}).get("version_id") == getattr(version, "pk", None)), None)
    suggestions = []
    if latest:
        for item in latest.result.result_json.get("suggestions", []):
            rendered = dict(item)
            rendered["citations"] = [{**citation, "url": (
                reverse("investment_research:document_detail", args=[pk, version.document_id])
                + "?" + urlencode({"version": citation["version_id"],
                                   "start": citation["start"], "end": citation["end"],
                                   "hash": citation["hash"]}) + "#research-citation")}
                for citation in item.get("citations", [])]
            suggestions.append(rendered)
    return render(request, "investment_research/metric_focus.html", {
        "dossier": dossier, "version": version, "choices": choices,
        "suggestions": suggestions, "can_write": is_writer(member),
        "providers": available_research_providers() if is_writer(member) else [],
    })


@_method(["POST"])
def fill_document_metrics_history(request, pk, document_pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    document = get_object_or_404(
        OfficialResearchDocument, pk=document_pk, security=dossier.security,
        source="sec", document_type="10-k",
    )
    version = get_object_or_404(
        OfficialResearchContentVersion,
        pk=_positive_id_or_404(request.POST.get("version")), document=document,
    )
    try:
        outcome = fill_tenk_history(actor=member, dossier=dossier, version=version)
    except (ResearchValidationError, SecClientError, ValueError) as exc:
        messages.error(request, f"历史年报补齐失败：{exc}")
    else:
        message = (f"历史年报：新增归档 {outcome['archived']} 份，保存正文 "
                   f"{outcome['saved']} 份。")
        if outcome["missing"]:
            messages.warning(request, message + "仍缺 FY" +
                             "、FY".join(map(str, outcome["missing"])) + "。")
        else:
            messages.success(request, message)
    return redirect(f"{reverse('investment_research:document_metrics', args=[pk, document_pk])}"
                    f"?{urlencode({'version': version.pk})}")


@_method(["POST"])
def create_citation(request, pk, document_pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    document = get_object_or_404(documents_for_security(dossier.security), pk=document_pk)
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
    return redirect(f"{url}?{urlencode({'version': version.pk, 'start': start, 'end': end, 'hash': digest})}#research-citation")


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
