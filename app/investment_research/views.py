"""投研模块视图。

所有入口 login_required；每个请求自行验证有效成员（未绑定/停用 403，
即使超级用户被全局中间件放行也不能绕过）。写操作 CSRF + POST，
其余方法 405。写入只调用 services，不在视图中重复 ORM 写入。
异常顺序：ThesisRevisionConflict / DuplicateDossier 先于父类
ResearchValidationError；DossierNotFound 转 404。
"""
import logging
import traceback
from hashlib import sha256
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlencode

from ai_analysis.models import AiAnalysisRequest
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import F, Q
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
    FutuFinancialSnapshot,
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
    PROMPT_TEMPLATE_VERSION, ResearchAiError, available_research_providers,
    generate_research_draft,
)
from .sec_content import fetch_sec_document_content
from .source_sync import sync_research_sources
from .tenk_chapters import tenk_chapter_coverage
from .tenk_financial_index import tenk_item8_index
from .tenk_history import fill_tenk_history
from .tenk_metrics import BUSINESS_CALC_CODES, HISTORICAL_LEASE_CODES, tenk_metric_grid
from .financial_overview import build_financial_overview
from .futu_financials import (
    FutuFinancialError, breakdown_tables, provider_code, refresh_futu_financials,
    highlight_rows, statement_tables,
)
from .analysis_materials import source_preview
from .thesis_analysis import generate_thesis_analysis, enforce_market_expectation_boundary
from .valuation_trial import build_valuation_trial
from .next_day_digest import (active_consent, latest_digest, latest_manual_analysis,
                              display_digest_result, pending_sources, set_auto_digest_consent,
                              generate_next_day_digest)
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
    preparation = dossier.preparations.last()
    initial = {"pillars": "\n".join(h["claim"] for h in preparation.hypotheses),
               "questions": "\n".join(preparation.questions)} if preparation else {}
    form = FirstThesisForm(request.POST if request.method == "POST" else None, initial=initial)
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
    latest_analysis = latest_manual_analysis(dossier)
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
            "latest_analysis": latest_analysis,
            "selected_metric_count": len(dossier.selected_metric_codes or []),
            "review_start_date": review_start_date,
            "review_items": [item for item in review_items
                             if item["pending"] or item["needs_revision"]][:5],
            "pending_review_count": sum(item["pending"] for item in review_items),
            "needs_revision_count": sum(item["needs_revision"] for item in review_items),
            "current_plan": current_plan,
        },
    )


@_method(["GET"])
def financials(request, pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    versions = list(OfficialResearchContentVersion.objects.filter(
        document__security=dossier.security, document__source="sec",
        document__document_type="10-k",
    ).select_related("document", "document__security").defer("raw_gzip", "content_text")
        .order_by("-document__period_end", "-fetched_at", "-pk")[:8])
    version = versions[0] if versions else None
    if request.GET.get("version"):
        version = next((item for item in versions
                        if str(item.pk) == request.GET["version"]), None)
        if version is None:
            raise Http404("所选年报正文版本不存在。")
    historical_versions = (list(OfficialResearchContentVersion.objects.filter(
        document__security=dossier.security, document__source="sec",
        document__document_type="10-k",
        document__period_end__lt=version.document.period_end,
        document__period_end__gte=version.document.period_end - timedelta(days=900),
    ).select_related("document", "document__security").defer(
        "raw_gzip", "content_text").order_by("-version_number", "-pk"))
        if version and version.document.period_end else [])
    cache_key = None
    if version and not settings.DEBUG and version.raw_sha256 and version.content_sha256:
        identity = [(item.pk, item.raw_sha256, item.content_sha256)
                    for item in historical_versions]
        cache_key = "research-financial-v3:" + sha256(repr((
            version.pk, version.raw_sha256, version.content_sha256, identity,
        )).encode()).hexdigest()
    result = cache.get(cache_key) if cache_key else None
    if result is None:
        if version is not None:
            version = OfficialResearchContentVersion.objects.select_related(
                "document", "document__security").get(pk=version.pk)
        result = build_financial_overview(version, historical_versions)
        if cache_key:
            cache.set(cache_key, result, timeout=3600)
    periods, rows, problem = result
    by_code = {row["code"]: row for row in rows}
    groups = []
    for title, codes in (
        ("增长从哪里来", ("revenue", "revenue_growth", "company_revenue", "iphone_revenue", "automotive_revenue")),
        ("增长有没有带来利润", ("gross_profit", "gross_margin", "operating_income",
                         "operating_margin", "net_income", "net_income_growth", "net_margin",
                         "diluted_eps", "eps_growth")),
        ("利润是否变成现金", ("operating_cash", "capex", "simple_fcf")),
        ("扩张与财务承受力", ("cash", "total_debt", "net_cash", "diluted_shares")),
    ):
        groups.append({"title": title, "rows": [by_code[code] for code in codes if code in by_code]})
    charts = []
    for title, codes, fixed_max in (
        ("收入规模", ("revenue",), None),
        ("利润率", ("gross_margin", "operating_margin", "net_margin"), Decimal(100)),
        ("现金流与投入", ("operating_cash", "capex", "simple_fcf"), None),
    ):
        series = [by_code[code] for code in codes if code in by_code]
        amounts = [abs(cell["amount"]) for row in series for cell in row["cells"]
                   if "amount" in cell]
        scale = fixed_max or (max(amounts) if amounts else Decimal(1))
        entries = []
        for row in series:
            points = []
            for period, cell in zip(periods, row["cells"]):
                amount = cell.get("amount")
                points.append({"period": period, "amount": amount,
                               "width": max(2, min(100, int(abs(amount) / scale * 100)))
                               if amount is not None and scale else 0,
                               "negative": amount is not None and amount < 0})
            entries.append({"label": row["label"], "unit": row["unit"], "points": points})
        charts.append({"title": title, "series": entries})
    return render(request, "investment_research/financials.html", {
        "dossier": dossier, "versions": versions, "version": version,
        "periods": periods, "groups": groups, "charts": charts, "problem": problem,
    })


@_method(["GET", "POST"])
def futu_financials(request, pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    try:
        code = provider_code(dossier.security)
        code_problem = ""
    except FutuFinancialError as exc:
        code, code_problem = "", str(exc)
    if request.method == "POST":
        if not is_writer(member):
            return HttpResponseForbidden("查看者角色不能更新富途财务资料。")
        if code_problem:
            messages.error(request, code_problem)
        else:
            try:
                from .company_jobs import enqueue
                enqueue(member, dossier, ["financials"])
            except (FutuFinancialError, ResearchValidationError) as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "正在获取富途财务资料并核对字段名称，请在公司资料页查看进度。")
        return redirect("investment_research:materials", pk=pk)
    snapshot = FutuFinancialSnapshot.objects.filter(security=dossier.security).first()
    statements = snapshot.data.get("statements", []) if snapshot else []
    breakdown = snapshot.data.get("breakdown") if snapshot else None
    tables = statement_tables(statements, snapshot.provider_code if snapshot else "")
    return render(request, "investment_research/futu_financials.html", {
        "dossier": dossier, "snapshot": snapshot, "code": code,
        "code_problem": code_problem, "tables": tables,
        "has_missing_names": any(table["missing_names"] for table in tables),
        "highlights": highlight_rows(tables),
        "breakdown": breakdown, "breakdown_groups": breakdown_tables(breakdown),
        "can_write": is_writer(member),
    })


@_method(["GET", "POST"])
def thesis_analysis(request, pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    can_write = is_writer(member)
    if request.method == "POST":
        if not can_write:
            return HttpResponseForbidden("查看者角色不能发起 AI 分析。")
        try:
            provider_id = int(request.POST.get("provider", ""))
        except ValueError:
            provider_id = 0
        try:
            analysis = generate_thesis_analysis(
                actor=member, dossier_id=dossier.pk,
                provider_id=provider_id,
                consent=request.POST.get("one_time_consent") == "yes",
                include_news=request.POST.get("include_news") == "yes",
                review_mode=request.POST.get("review_mode", "full"),
            )
        except (ResearchAiError, ResearchValidationError) as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "公司研究简报已生成；关键结论可展开核对原文。")
            return redirect("investment_research:thesis_analysis_detail", pk=pk,
                            analysis_pk=analysis.pk)
        return redirect("investment_research:thesis_analysis", pk=pk)
    sources = source_preview(dossier)
    from investment_watch.research_bridge import selected_candidates
    selected_news = list(selected_candidates(dossier).order_by("-pk")[:10])
    from .company_workspace import research_history
    baseline = research_history(dossier).filter(
        status=AiAnalysisRequest.STATUS_SUCCESS,
        scope__thesis_revision_id=dossier.current_revision_id,
    ).first()
    histories = [analysis for analysis in AiAnalysisRequest.objects.filter(
        member=member, family=member.family, module="investment_research",
        analysis_type="thesis_synthesis",
    ).select_related("provider").order_by("-created_at")[:40]
        if (analysis.scope or {}).get("dossier_id") == dossier.pk][:10]
    return render(request, "investment_research/thesis_analysis.html", {
        "dossier": dossier, "revision": dossier.current_revision,
        "sources": sources, "providers": available_research_providers() if can_write else [],
        "can_write": can_write, "histories": histories,
        "selected_news": selected_news, "has_analysis_material": bool(sources or selected_news),
        "baseline_analysis": baseline,
        "review_mode": request.GET.get("mode", "full") if baseline else "full",
    })


@_method(["GET"])
def thesis_analysis_detail(request, pk, analysis_pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    analysis = get_object_or_404(
        AiAnalysisRequest.objects.select_related("provider"),
        pk=analysis_pk, member=member, family=member.family,
        module="investment_research", analysis_type="thesis_synthesis",
    )
    if (analysis.scope or {}).get("dossier_id") != dossier.pk:
        raise Http404("分析记录不属于此档案。")
    result = analysis.result.result_json if analysis.status == AiAnalysisRequest.STATUS_SUCCESS else None
    if result:
        from .report_sections import source_sections
        result = source_sections(result, analysis.scope or {})
    valuation = build_valuation_trial(dossier.security, analysis.scope, request.GET)
    return render(request, "investment_research/thesis_analysis_brief.html", {
        "dossier": dossier, "analysis": analysis, "result": result,
        "valuation": valuation,
        "new_candidate_count": dossier.news_candidates.filter(created_at__gt=analysis.created_at).count(),
        "is_current_revision": (analysis.scope or {}).get("thesis_revision_id") == dossier.current_revision_id,
    })


@_method(["GET"])
def company_research(request, pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    from .company_workspace import workspace_context, research_history
    report = research_history(dossier).filter(status=AiAnalysisRequest.STATUS_SUCCESS).first()
    view = request.GET.get("view", "conclusion")
    if view not in {"changes", "evidence"} and report:
        return thesis_analysis_detail(request, pk, report.pk)
    context = workspace_context(dossier, request.GET)
    return render(request, "investment_research/company_materials.html", {
        "dossier": dossier, "revision": dossier.current_revision,
        "can_write": is_writer(member), **context,
    })


@_method(["GET"])
def next_day_tracking(request, pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    digest = latest_digest(dossier)
    pending = pending_sources(dossier)
    latest_analysis = latest_manual_analysis(dossier)
    consent = active_consent(dossier)
    consent_active = bool(consent and latest_analysis and
                          consent.provider_id == latest_analysis.provider_id)
    checks = (latest_analysis.result.result_json or {}).get("next_checks", []) if latest_analysis else []
    quote = build_valuation_trial(dossier.security, {}, {})
    return render(request, "investment_research/next_day_tracking.html", {
        "dossier": dossier, "digest": digest,
        "result": display_digest_result(digest.result.result_json) if digest else None,
        "pending": pending, "checks": checks,
        "latest_analysis": latest_analysis, "quote": quote,
        "consent": consent, "consent_active": consent_active,
        "can_write": is_writer(member),
    })


@_method(["POST"])
def next_day_consent(request, pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    if not is_writer(member):
        return HttpResponseForbidden("查看者角色不能修改自动分析授权。")
    action = request.POST.get("action")
    if action not in {"enable", "disable"}:
        messages.error(request, "请选择开启或关闭自动对照。")
    else:
        try:
            set_auto_digest_consent(actor=member, dossier_id=dossier.pk,
                                    enabled=action == "enable")
        except ResearchValidationError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "已开启新资料的个人判断自动对照。" if action == "enable"
                             else "已关闭个人判断自动对照；公开资料事件简报仍可生成。")
    return redirect("investment_research:next_day_tracking", pk=pk)


@_method(["POST"])
def next_day_generate(request, pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    if not is_writer(member):
        return HttpResponseForbidden("查看者角色不能生成自动事件简报。")
    try:
        digest = generate_next_day_digest(dossier.pk)
    except ResearchAiError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "新资料事件简报已生成，可展开核对原文。" if digest
                         else "当前没有待处理的官方正文，或尚未生成公司研究简报。")
    return redirect("investment_research:next_day_tracking", pk=pk)


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
    analysis = latest_manual_analysis(dossier)
    digest = latest_digest(dossier)
    reference = {
        "analysis": analysis,
        "suggestion": (analysis.result.result_json or {}).get("suggested_revision") if analysis else "",
        "suggestion_is_current": bool(analysis and
            (analysis.scope or {}).get("thesis_revision_id") == current.pk),
        "digest": digest,
        "digest_result": display_digest_result(digest.result.result_json) if digest else None,
        "digest_is_current": bool(digest and
            (digest.scope or {}).get("thesis_revision_id") == current.pk),
    }
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
                        **reference,
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
        {"dossier": dossier, "form": form, "conflict": False, **reference},
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
    queryset = documents_for_security(dossier.security).order_by(
        F("published_at").desc(nulls_last=True), F('metadata__fiscal_year').desc(nulls_last=True),
        F('metadata__quarter').desc(nulls_last=True), "-pk")
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
            "can_fetch_sec": document.source == "sec" and document.document_type in {"10-k", "10-q", "8-k", "20-f", "40-f", "6-k"},
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
