import json
import re
import uuid
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, F
from django.http import JsonResponse, Http404
from django.shortcuts import render, redirect
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from investment_research.permissions import (
    get_current_member,
    accessible_dossiers,
    is_writer,
)
from family_core.models import FamilyMember
from .catalogue import TOPICS, CATEGORIES
from .models import (
    NewsSource,
    NewsMaterial,
    MaterialVersion,
    InvestmentEvent,
    ResearchCandidate,
    WatchRule,
    WatchConsent,
    WatchRun,
    MemberAnnotation,
    BodySnapshot,
    BodyAttempt,
    ScreeningBatch,
)
from .services import (
    WatchError,
    Conflict,
    digest,
    writer,
    public_versions,
    filter_news,
    dossier_for,
    version_for,
    associate,
    idempotent,
    candidate_stale,
    current_evidence,
    save_rule,
    recall,
    check_revision,
    annotate,
    review,
    organize_event,
)


def wants_json(request):
    return request.GET.get(
        "format"
    ) == "json" or "application/json" in request.headers.get("Accept", "")


def endpoint(methods):
    def decorate(fn):
        @login_required
        @require_http_methods(methods)
        @wraps(fn)
        def wrapped(request, *args, **kwargs):
            member = get_current_member(request)
            if not member:
                raise PermissionDenied("请先绑定有效家庭成员。")
            request.watch_member = member
            try:
                return fn(request, *args, **kwargs)
            except WatchError as exc:
                if wants_json(request):
                    return JsonResponse({"error": str(exc)}, status=exc.status)
                return render(
                    request,
                    "investment_watch/error.html",
                    {"error": str(exc)},
                    status=exc.status,
                )

        return wrapped

    return decorate


def payload(request, allowed):
    if request.content_type == "application/json":
        try:
            values = json.loads(request.body)
        except (ValueError, UnicodeError):
            raise WatchError("请求 JSON 无效。")
        if not isinstance(values, dict) or set(values) - set(allowed):
            raise WatchError("存在不允许的字段。")
        return values
    return {key: request.POST[key] for key in allowed if key in request.POST}


def integer(value, default=None):
    if value is None and default is not None:
        return default
    try:
        if isinstance(value, (bool, float)):
            raise ValueError
        return int(value)
    except (ValueError, TypeError):
        raise WatchError("缺少有效的版本或对象编号。")


def boolean(value):
    if value in (True, "true", "on", "1"):
        return True
    if value in (False, None, "false", "", "0"):
        return False
    raise WatchError("布尔值无效。")


def context(request, **values):
    from django.conf import settings

    return {
        "can_write": is_writer(request.watch_member),
        "operation_key": uuid.uuid4().hex,
        "local_preview": getattr(settings, "WATCH_LOCAL_PREVIEW", False),
        **values,
    }


def paginate(request, items):
    filters = {
        k: v for k, v in request.GET.items() if k not in {"cursor", "page", "format"}
    }
    scope = digest([request.watch_member.pk, request.path, filters])
    token = request.GET.get("cursor")
    page_number = 1
    if token:
        try:
            data = signing.loads(token, salt="investment-watch-page", max_age=86400)
            if (
                data["scope"] != scope
                or type(data["page"]) is not int
                or data["page"] < 1
            ):
                raise ValueError
            page_number = data["page"]
        except (signing.BadSignature, KeyError, TypeError, ValueError):
            raise WatchError("分页链接已失效，请从第一页重新浏览。")
    page = Paginator(items, 30).get_page(page_number)

    def cursor(number):
        return signing.dumps(
            {"scope": scope, "page": number}, salt="investment-watch-page"
        )

    page.next_cursor = cursor(page.next_page_number()) if page.has_next() else None
    page.previous_cursor = (
        cursor(page.previous_page_number()) if page.has_previous() else None
    )
    return page


def filter_query(request):
    query = request.GET.copy()
    for name in ("cursor", "page", "format"):
        query.pop(name, None)
    return query.urlencode()


def version_json(version):
    return {
        "id": version.material_id,
        "material_version": version.pk,
        "version_number": version.number,
        "title": version.title,
        "summary": version.summary,
        "url": version.url,
        "published_at": version.published_at,
        "published_precision": version.published_precision,
        "found_at": version.found_at,
        "occurred_at": version.occurred_at,
        "status": version.status,
        "market": version.market,
        "category": version.category,
        "topics": version.topics,
        "source": version.material.source.name,
        "event_id": version.material.event_id,
        "original_chain": version.original_chain,
    }


@endpoint(["GET"])
def news(request):
    member = request.watch_member
    query = filter_news(member, request.GET)
    company_name = ""
    if request.GET.get("dossier"):
        from .workspace import reading_rule
        from .catalogue import match_rule

        dossier = dossier_for(member, integer(request.GET["dossier"]))
        rule = reading_rule(dossier)
        company_name = dossier.security.name
        query = [v for v in query if match_rule(rule, v)[0]]
    # One exact-content event in the feed; each source/version remains available in details.
    seen = set()
    items = []
    for item in query:
        event_id = item.material.event.merged_into_id or item.material.event_id
        if event_id not in seen:
            seen.add(event_id)
            items.append(item)
    page = paginate(request, items)
    if wants_json(request):
        return JsonResponse(
            {
                "items": [version_json(v) for v in page],
                "page": page.number,
                "total": len(items),
                "next_cursor": page.next_cursor,
            }
        )
    return render(
        request,
        "investment_watch/news.html",
        context(
            request,
            items=page,
            sources=NewsSource.objects.filter(family=member.family),
            categories=CATEGORIES,
            filters=request.GET,
            topic_name=next(
                (t[1] for t in TOPICS if t[0] == request.GET.get("topic")), company_name
            ),
            filter_query=filter_query(request),
        ),
    )


@endpoint(["GET"])
def topics(request):
    from .workspace import reading_rule
    from .catalogue import match_rule

    versions = list(public_versions(request.watch_member).exclude(status="withdrawn"))
    result = [
        {
            "id": key,
            "name": name,
            "group": group,
            "description": description,
            "total": len(
                {
                    v.material.event.merged_into_id or v.material.event_id
                    for v in versions
                    if key in v.topics
                }
            ),
        }
        for key, name, group, description, _ in TOPICS
    ]
    if wants_json(request):
        return JsonResponse({"topics": result})
    companies = []
    for dossier in accessible_dossiers(request.watch_member):
        rule = reading_rule(dossier)
        companies.append(
            {
                "dossier": dossier,
                "total": len(
                    {
                        v.material.event.merged_into_id or v.material.event_id
                        for v in versions
                        if match_rule(rule, v)[0]
                    }
                ),
            }
        )
    return render(
        request,
        "investment_watch/topics.html",
        context(request, topics=result, companies=companies),
    )


@endpoint(["GET"])
def news_detail(request, pk):
    from .events import latest_relation, suggestions
    from .models import MaterialRelation

    member = request.watch_member
    material = (
        NewsMaterial.objects.filter(pk=pk, source__family=member.family)
        .select_related("current_version")
        .first()
    )
    if not material:
        raise Http404
    version = version_for(
        member, integer(request.GET.get("version"), material.current_version_id)
    )
    if version.material_id != material.pk:
        raise Http404
    if wants_json(request):
        return JsonResponse(version_json(version))
    root = version.material.event.merged_into_id or version.material.event_id
    return render(
        request,
        "investment_watch/detail.html",
        context(
            request,
            version=version,
            body_snapshot=BodySnapshot.objects.filter(material_version=version).first(),
            dossiers=accessible_dossiers(member),
            annotation=MemberAnnotation.objects.filter(
                member=member, event=material.event
            ).first(),
            versions=material.versions.order_by("-number"),
            sources=MaterialVersion.objects.filter(
                Q(material__event_id=root) | Q(material__event__merged_into_id=root),
                material__current_version_id=F("pk"),
            ).select_related("material__source"),
            is_admin=member.role == FamilyMember.ROLE_ADMIN,
            events=InvestmentEvent.objects.filter(family=member.family)
            .exclude(pk=material.event_id)
            .order_by("-pk")[:100],
            stale=material.current_version_id != version.pk,
            relation=latest_relation(version),
            developments=[
                r
                for r in MaterialRelation.objects.filter(
                    target=version,
                    source__material__current_version_id=F("source_id"),
                    kind__in=["followup", "conflict"],
                )
                .select_related("source__material")
                .order_by("created_at")[:50]
                if latest_relation(r.source).pk == r.pk
            ],
            suggestions=suggestions(member, version),
            earlier_versions=public_versions(member)
            .exclude(status="withdrawn")
            .exclude(pk=version.pk)
            .order_by("-pk")[:100],
        ),
    )


@endpoint(["POST"])
def news_associate(request, pk):
    body = payload(
        request,
        [
            "dossier_id",
            "material_version",
            "expected_revision",
            "idempotency_key",
            "selection",
        ],
    )
    if body.get("selection"):
        parts = str(body.pop("selection")).split(":")
        if len(parts) != 2:
            raise WatchError("请选择公司研究档案。")
        body["dossier_id"], body["expected_revision"] = map(integer, parts)
    member = request.watch_member
    v = version_for(member, integer(body.get("material_version")))
    if v.material_id != pk:
        raise Http404

    def action():
        candidate = associate(
            member,
            integer(body.get("dossier_id")),
            v.pk,
            integer(body.get("expected_revision")),
        )
        return {
            "id": candidate.pk,
            "status": candidate.status,
            "direction": "unknown",
            "material_version": candidate.material_version_id,
        }

    result = idempotent(
        member,
        "associate",
        request.headers.get("Idempotency-Key") or body.pop("idempotency_key", ""),
        body,
        action,
    )
    if wants_json(request):
        return JsonResponse(result, status=201)
    messages.success(request, "已加入待分析候选；正式判断未修改。")
    return redirect("investment_watch:item", pk=result["id"])


@endpoint(["GET"])
def items(request):
    from .analysis import targets

    member = request.watch_member
    query = (
        ResearchCandidate.objects.filter(
            dossier__owner=member, dossier__family=member.family
        )
        .select_related(
            "dossier__security",
            "dossier__current_revision",
            "material_version__material__source",
            "material_version__material__event",
        )
        .prefetch_related("evidence__reviews", "screenings__batch")
        .order_by("-created_at", "-pk")
    )
    if request.GET.get("dossier"):
        dossier = dossier_for(member, integer(request.GET["dossier"]))
        query = query.filter(dossier=dossier)
    else:
        dossier = None
    if request.GET.get("saved") in {"true", "1"}:
        query = query.filter(
            material_version__material__event_id__in=MemberAnnotation.objects.filter(
                member=member, saved=True
            ).values("event_id")
        )
    entries = []
    from .presentation import reading_state, state_cache
    query = list(query)
    reading_cache = state_cache(query)
    scope = request.GET.get("scope") or ("all" if wants_json(request) or request.GET.get("direction") or request.GET.get("assumption") or request.GET.get("saved") else "important")
    if scope not in {"important", "all"}:
        raise WatchError("未知阅读范围。")
    for candidate in query:
        candidate.stale = candidate_stale(candidate)
        candidate.current_evidence = current_evidence(candidate)
        for evidence in candidate.current_evidence:
            evidence.assumption_label = targets(candidate.dossier.current_revision).get(
                evidence.assumption_key, evidence.assumption_key
            )
            reviews = list(evidence.reviews.all())
            latest = max(reviews, key=lambda r: r.pk) if reviews else None
            if latest:
                evidence.direction = latest.direction
                evidence.explanation = "成员复核：" + latest.reason
        direction = request.GET.get("direction")
        assumption = request.GET.get("assumption")
        if direction or assumption:
            if candidate.stale:
                continue
            if not any(
                (not direction or e.direction == direction)
                and (not assumption or e.assumption_key == assumption)
                for e in candidate.current_evidence
            ):
                if not (
                    direction == "unknown"
                    and not assumption
                    and not candidate.current_evidence
                ):
                    continue
        candidate.highlight_evidence = [e for e in candidate.current_evidence if e.direction != "unknown"][:2]
        candidate.more_evidence = [e for e in candidate.current_evidence if e not in candidate.highlight_evidence]
        candidate.unknown_count = sum(e.direction == "unknown" for e in candidate.more_evidence)
        reading_state(candidate, reading_cache)
        if scope == "important" and (candidate.stale or not candidate.reading_important or not candidate.reading_recent):
            continue
        entries.append(candidate)
    page = paginate(request, entries)
    if wants_json(request):
        return JsonResponse(
            {
                "items": [
                    {
                        "id": c.pk,
                        "dossier_id": c.dossier_id,
                        "stale": c.stale,
                        "material": version_json(c.material_version),
                        "reason": c.reason,
                        "reading_status": c.reading_label,
                        "relevance": c.relevance_label,
                        "evidence": [
                            {
                                "id": e.pk,
                                "assumption_key": e.assumption_key,
                                "direction": e.direction,
                                "explanation": e.explanation,
                            }
                            for e in c.current_evidence
                        ],
                    }
                    for c in page
                ],
                "next_cursor": page.next_cursor,
            }
        )
    return render(
        request,
        "investment_watch/items.html",
        context(
            request,
            items=page,
            dossier=dossier,
            dossiers=accessible_dossiers(member),
            assumptions=targets(dossier.current_revision) if dossier else {},
            filters=request.GET,
            reading_scope=scope,
            filter_query=filter_query(request),
        ),
    )


@endpoint(["POST"])
def event_organize(request, pk):
    organize_event(
        request.watch_member,
        pk,
        integer(request.POST.get("target"), 0),
        request.POST.get("action"),
        request.POST.get("reason"),
        request.POST.get("expected_updated"),
    )
    messages.success(request, "事件关系已保存，来源版本、引文和个人收藏仍保留。")
    return redirect("investment_watch:news")


@endpoint(["GET"])
def item(request, pk):
    member = request.watch_member
    candidate = (
        ResearchCandidate.objects.filter(
            pk=pk, dossier__owner=member, dossier__family=member.family
        )
        .select_related(
            "dossier__security",
            "dossier__current_revision",
            "material_version__material__source",
        )
        .first()
    )
    if not candidate:
        raise Http404
    from .presentation import reading_state
    reading_state(candidate)
    rows = list(candidate.evidence.select_related("revision", "input_relation__target", "input_body").prefetch_related("reviews").order_by("-pk"))
    current_ids = {e.pk for e in current_evidence(candidate, rows)}
    for evidence in rows:
        evidence.is_current = evidence.pk in current_ids
    if wants_json(request):
        return JsonResponse(
            {
                "id": candidate.pk,
                "stale": candidate_stale(candidate),
                "reason": candidate.reason,
                "material": version_json(candidate.material_version),
                "dossier_id": candidate.dossier_id,
                "evidence": [
                    {
                        "id": e.pk,
                        "revision_id": e.revision_id,
                        "is_current": e.is_current,
                        "assumption_key": e.assumption_key,
                        "direction": e.direction,
                        "explanation": e.explanation,
                        "source_claim": e.source_claim,
                        "author_opinion": e.author_opinion,
                        "input_relation_id": e.input_relation_id,
                        "input_body_id": e.input_body_id,
                        "quote": e.quote,
                        "locator": e.locator,
                        "conditions": e.conditions,
                        "gaps": e.gaps,
                        "reviews": list(
                            e.reviews.order_by("pk").values(
                                "id", "direction", "reason", "created_at"
                            )
                        ),
                    }
                    for e in rows
                ],
            }
        )
    from .analysis import targets
    from .events import canonical_version

    canonical = canonical_version(candidate.material_version)
    original_candidate = (
        ResearchCandidate.objects.filter(
            dossier=candidate.dossier,
            revision=candidate.revision,
            material_version=canonical,
        ).first()
        if canonical.pk != candidate.material_version_id
        else None
    )

    for evidence in rows:
        evidence.assumption_label = targets(evidence.revision).get(
            evidence.assumption_key, evidence.assumption_key
        )
    return render(
        request,
        "investment_watch/item.html",
        context(
            request,
            candidate=candidate,
            evidence=rows,
            stale=candidate_stale(candidate),
            version=candidate.material_version,
            original_candidate=original_candidate,
            body_snapshot=BodySnapshot.objects.filter(material_version=candidate.material_version).first(),
            screening=candidate.latest_screening,
            body_attempt=BodyAttempt.objects.filter(family=member.family, security=candidate.dossier.security,
                material_version=candidate.material_version).first(),
            body_references=BodySnapshot.objects.filter(pk__in={e.input_body_id for e in rows if e.input_body_id}).select_related("material_version"),
        ),
    )


@endpoint(["GET", "POST"])
def rules(request):
    member = request.watch_member
    if request.method == "POST":
        body = payload(
            request,
            [
                "dossier_id",
                "aliases",
                "products",
                "official_domains",
                "business_context",
                "topics",
                "include",
                "exclude",
                "enabled",
                "expected_version",
            ],
        )
        if request.content_type != "application/json":
            for key in ("aliases", "products", "official_domains", "topics", "include", "exclude"):
                body[key] = [
                    w.strip()
                    for w in re.split("[,，\n]", body.get(key, ""))
                    if w.strip()
                ]
        if request.POST.get("action") == "preview":
            from .workspace import preview_rule

            writer(member)
            dossier = dossier_for(member, integer(body.get("dossier_id")))
            submitted = {k: request.POST[k] for k in body if k in request.POST}
            return render(
                request,
                "investment_watch/rule_preview.html",
                context(
                    request,
                    dossier=dossier,
                    preview=preview_rule(member, body),
                    submitted=submitted,
                ),
            )
        rule = save_rule(
            member,
            integer(body.get("dossier_id")),
            {**body, "enabled": boolean(body.get("enabled"))},
            integer(body.get("expected_version")),
        )
        if wants_json(request):
            return JsonResponse({"id": rule.pk, "version": rule.version})
        recall(rule.dossier)
        messages.success(request, "关注已保存，已匹配现有材料。")
        return redirect("investment_watch:rules")
    dossiers = list(accessible_dossiers(member))
    for d in dossiers:
        d.rule = WatchRule.objects.filter(dossier=d).first()
    if wants_json(request):
        return JsonResponse(
            {
                "rules": [
                    {
                        "dossier_id": d.pk,
                        "version": d.rule.version if d.rule else 0,
                        **{
                            k: getattr(d.rule, k)
                            if d.rule
                            else (False if k == "enabled" else "" if k == "business_context" else [])
                            for k in (
                                "aliases",
                                "products",
                                "official_domains",
                                "business_context",
                                "topics",
                                "include",
                                "exclude",
                                "enabled",
                            )
                        },
                    }
                    for d in dossiers
                ]
            }
        )
    from investment_research.research_ai import available_research_providers

    for d in dossiers:
        d.consent = (
            WatchConsent.objects.filter(dossier=d).select_related("provider").first()
        )
    return render(
        request,
        "investment_watch/rules.html",
        context(
            request,
            dossiers=dossiers,
            providers=available_research_providers() if is_writer(member) else [],
        ),
    )


@endpoint(["GET", "POST"])
def company_add(request):
    from django import forms
    from .workspace import CompanyForm, add_company, search_companies

    writer(request.watch_member)
    form = CompanyForm(request.POST if request.method == "POST" else None)
    query = request.GET.get("q", "").strip()[:100]
    if query and request.method == "GET":
        form.fields["security"].queryset = search_companies(query)
    if request.method == "POST" and form.is_valid():
        try:
            dossier = add_company(request.watch_member, form.cleaned_data)
        except forms.ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(
                request, f"{dossier.security.name} 已加入。请预览规则，再启用关注。"
            )
            return redirect("investment_watch:rules")
    return render(
        request,
        "investment_watch/company_add.html",
        context(request, form=form, query=query),
    )


@endpoint(["GET"])
def company(request, pk):
    dossier = dossier_for(request.watch_member, pk)
    return redirect("investment_research:company_research", pk=dossier.pk)


@endpoint(["POST"])
def consent(request, pk):
    from ai_analysis.models import AiProvider
    from .analysis import set_consent
    from investment_research.research_ai import ResearchAiError

    member = request.watch_member
    provider = AiProvider.objects.filter(
        pk=integer(request.POST.get("provider"))
    ).first()
    if not provider:
        raise WatchError("模型不存在。")
    try:
        set_consent(
            member, pk, provider, boolean(request.POST.get("allow_personal_thesis"))
        )
    except ResearchAiError as exc:
        raise WatchError(str(exc))
    messages.success(request, "个人假设分析授权已更新。")
    return redirect("investment_watch:rules")


@endpoint(["POST"])
def runs(request):
    body = payload(request, ["dossier_id", "expected_revision", "idempotency_key"])
    member = request.watch_member

    def action():
        dossier = dossier_for(member, integer(body.get("dossier_id")), lock=True)
        check_revision(dossier, integer(body.get("expected_revision")))
        from .worker import queue_run

        run = queue_run(dossier)
        return {"id": run.pk, "status": run.status}

    result = idempotent(
        member,
        "run",
        request.headers.get("Idempotency-Key") or body.pop("idempotency_key", ""),
        body,
        action,
    )
    if wants_json(request):
        return JsonResponse(result, status=202)
    messages.success(request, "检查任务已加入队列，由后台处理；不代表分析已完成。")
    return redirect("investment_watch:coverage")


@endpoint(["GET"])
def research_context(request):
    from ai_analysis.models import AiAnalysisRequest

    dossier = dossier_for(request.watch_member, integer(request.GET.get("dossier_id")))
    latest = (
        AiAnalysisRequest.objects.filter(
            member=request.watch_member,
            module="investment_research",
            analysis_type="thesis_synthesis",
            scope__dossier_id=dossier.pk,
        )
        .order_by("-created_at")
        .first()
    )
    return JsonResponse(
        {
            "dossier_id": dossier.pk,
            "revision_id": dossier.current_revision_id,
            "research_url": reverse("investment_watch:company", args=[dossier.pk]),
            "latest_analysis_url": reverse(
                "investment_research:thesis_analysis_detail",
                args=[dossier.pk, latest.pk],
            )
            if latest
            else None,
            "new_candidate_count": dossier.news_candidates.filter(
                created_at__gt=latest.created_at
            ).count()
            if latest
            else dossier.news_candidates.count(),
        }
    )


@endpoint(["POST"])
def annotation(request, pk):
    body = payload(request, ["note", "saved", "read", "expected_version"])
    record = annotate(
        request.watch_member,
        pk,
        {
            "note": body.get("note", ""),
            "saved": boolean(body.get("saved")),
            "read": boolean(body.get("read")),
        },
        integer(body.get("expected_version")),
    )
    if wants_json(request):
        return JsonResponse({"id": record.pk, "version": record.version})
    messages.success(request, "收藏与备注已保存。")
    return redirect("investment_watch:saved")


@endpoint(["GET"])
def saved(request):
    notes = MemberAnnotation.objects.filter(
        member=request.watch_member,
        event__family=request.watch_member.family,
        saved=True,
    ).select_related("event")
    for note in notes:
        note.material = note.event.materials.select_related("current_version").first()
    return render(request, "investment_watch/saved.html", context(request, notes=notes))


@endpoint(["POST"])
def evidence_review(request, pk):
    body = payload(request, ["direction", "reason", "idempotency_key"])
    result = idempotent(
        request.watch_member,
        "review",
        request.headers.get("Idempotency-Key") or body.pop("idempotency_key", ""),
        body,
        lambda: {
            "id": review(
                request.watch_member, pk, body.get("direction"), body.get("reason")
            ).pk
        },
    )
    if wants_json(request):
        return JsonResponse(result, status=201)
    messages.success(request, "复核已追加保存；正式判断没有改变。")
    return redirect("investment_watch:items")


@endpoint(["POST"])
def request_reading(request, pk):
    member = request.watch_member
    body = payload(request, ["reason", "expected_revision", "idempotency_key"])
    reason = body.get("reason", "")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise WatchError("请填写不超过 500 字的重要性理由。")

    def action():
        candidate = ResearchCandidate.objects.select_for_update(of=("self",)).select_related("dossier", "material_version__material").filter(
            pk=pk, dossier__owner=member, dossier__family=member.family).first()
        if not candidate:
            raise Http404
        check_revision(candidate.dossier, integer(body.get("expected_revision")))
        if candidate_stale(candidate):
            raise Conflict("材料已更新，请查看最新版本。")
        candidate.reading_requested = True
        candidate.reading_reason = reason.strip()
        candidate.manual = True
        candidate.save(update_fields=["reading_requested", "reading_reason", "manual", "updated_at"])
        from .worker import queue_run
        queue_run(candidate.dossier)
        return {"id": candidate.pk, "requested": True}

    result = idempotent(member, "request-reading", request.headers.get("Idempotency-Key") or body.get("idempotency_key", ""), body, action)
    if wants_json(request):
        return JsonResponse(result, status=201)
    messages.success(request, "已加入优先阅读候选；由定时任务处理，仍遵守授权、预算与每日三次抓取上限。")
    return redirect("investment_watch:item", pk=pk)


@endpoint(["POST"])
def select_research(request, pk):
    member = request.watch_member
    writer(member)
    with transaction.atomic():
        candidate = (
            ResearchCandidate.objects.select_for_update(of=("self",))
            .select_related("dossier", "material_version__material")
            .filter(pk=pk, dossier__owner=member, dossier__family=member.family)
            .first()
        )
        if not candidate:
            raise Http404
        if candidate_stale(candidate):
            raise Conflict("材料或判断已更新，请重新关联。")
        candidate.selected_for_research = boolean(request.POST.get("selected"))
        candidate.save(update_fields=["selected_for_research", "updated_at"])
    messages.success(request, "已更新下一次研究的材料选择；旧分析保持原样。")
    if request.POST.get("return_to") == "company":
        return redirect(reverse("investment_research:company_research", kwargs={"pk": candidate.dossier_id}) + "?view=changes")
    return redirect("investment_watch:item", pk=pk)


@endpoint(["GET", "POST"])
def coverage(request):
    member = request.watch_member
    if request.method == "POST":
        writer(member)
        if member.role != FamilyMember.ROLE_ADMIN:
            raise PermissionDenied("仅家庭管理员可修改信源。")
        source = NewsSource.objects.filter(
            pk=integer(request.POST.get("source_id")), family=member.family
        ).first()
        if not source:
            raise Http404
        source.enabled = boolean(request.POST.get("enabled"))
        source.save(update_fields=["enabled", "updated_at"])
        return redirect("investment_watch:coverage")
    from .budget import budget_status

    sources = NewsSource.objects.filter(family=member.family).order_by("name")
    budget = budget_status(member.family)
    from django.conf import settings
    from .body_capture import china_day, firecrawl_key
    body_enabled = getattr(settings, "INVESTMENT_WATCH_BODY_ENABLED", False)
    body_configured = bool(firecrawl_key())
    from .capture_account import cached_usage
    from datetime import timedelta
    from django.utils import timezone
    capture_usage = cached_usage() if member.role == FamilyMember.ROLE_ADMIN else None
    source_rows = list(sources)
    for source in source_rows:
        source.next_check = source.last_checked_at + timedelta(minutes=max(15, source.interval_minutes)) if source.last_checked_at else None
        source.due = source.enabled and (not source.next_check or source.next_check <= timezone.now())
    body_attempts = BodyAttempt.objects.filter(family=member.family, candidate__dossier__owner=member).select_related(
        "security", "material_version").order_by("-created_at")[:15]
    screenings = ScreeningBatch.objects.filter(dossier__owner=member, dossier__family=member.family).select_related("dossier__security").order_by("-created_at")[:10]
    body_usage = [{"company": d.security.name, "used": BodyAttempt.objects.filter(family=member.family,
        security=d.security, day=china_day()).count()} for d in accessible_dossiers(member).select_related("security")]
    runs = WatchRun.objects.filter(
        dossier__owner=member, dossier__family=member.family
    ).order_by("-created_at")[:20]
    if wants_json(request):
        return JsonResponse(
            {
                "budget": budget,
                "body_pipeline": {"enabled": body_enabled, "configured": body_configured, "daily_limit": 3, "usage": body_usage},
                "sources": list(
                    sources.values(
                        "id",
                        "key",
                        "name",
                        "enabled",
                        "last_checked_at",
                        "last_success_at",
                        "last_error",
                    )
                ),
                "runs": list(
                    runs.values(
                        "id", "dossier_id", "status", "message", "count", "updated_at"
                    )
                ),
                "limitations": [
                    "公开标题与摘录，不代表全文覆盖",
                    "仅检查每个来源最新有限条目，不能保证不漏选",
                    "主题与候选由规则召回，需人工核查",
                ],
            }
        )
    return render(
        request,
        "investment_watch/coverage.html",
        context(
            request,
            sources=source_rows,
            capture_usage=capture_usage,
            budget=budget,
            runs=runs,
            body_enabled=body_enabled,
            body_configured=body_configured,
            body_usage=body_usage,
            body_attempts=body_attempts,
            screenings=screenings,
            is_admin=member.role == FamilyMember.ROLE_ADMIN,
        ),
    )


@endpoint(["GET", "POST"])
def source_edit(request, pk=None):
    from intelligence.http_client import fetch_public_url, SafeHttpError
    from .source_templates import SourceForm, parse_source

    member = request.watch_member
    writer(member)
    if member.role != FamilyMember.ROLE_ADMIN:
        raise PermissionDenied("仅家庭管理员可配置共享信源。")
    source = NewsSource(family=member.family, key="custom-" + uuid.uuid4().hex)
    if pk:
        source = (
            NewsSource.objects.filter(pk=pk, family=member.family)
            .exclude(adapter="official")
            .first()
        )
        if not source:
            raise Http404
    keys = [
        "name",
        "url",
        "adapter",
        "market",
        "interval_minutes",
        "max_items",
        "config",
    ]
    form = SourceForm(
        request.POST if request.method == "POST" else None,
        initial={key: getattr(source, key) for key in keys},
    )
    rows = None
    token = ""
    if request.method == "POST" and form.is_valid():
        for key in keys:
            setattr(source, key, form.cleaned_data[key])
        signature = digest([member.pk, pk, source.updated_at, form.cleaned_data])
        if request.POST.get("action") == "save":
            try:
                tested = signing.loads(
                    request.POST.get("tested", ""),
                    salt="watch-source-test",
                    max_age=1800,
                )
                if tested != signature:
                    raise signing.BadSignature
            except signing.BadSignature:
                form.add_error(None, "配置已改变或测试已过期，请先测试读取。")
            else:
                source.enabled = False
                source.cursor = {}
                source.last_checked_at = None
                source.last_success_at = None
                source.last_error = ""
                with transaction.atomic():
                    if source.pk:
                        current = NewsSource.objects.select_for_update().get(pk=source.pk)
                        if current.updated_at != source.updated_at:
                            raise Conflict("来源已被其他操作更新，请重新测试后保存。")
                    source.save()
                messages.success(
                    request, "来源已保存为暂停状态，核对后可在来源与运行启用。"
                )
                return redirect("investment_watch:coverage")
        else:
            try:
                response = fetch_public_url(source.url)
                rows = parse_source(response.body, source)
                if not rows:
                    raise WatchError("没有读取到文章，请检查订阅地址。")
                token = signing.dumps(signature, salt="watch-source-test")
            except (SafeHttpError, WatchError) as exc:
                form.add_error(None, getattr(exc, "safe_message", str(exc)))
    return render(
        request,
        "investment_watch/source_edit.html",
        context(request, form=form, rows=rows, tested=token),
    )


@endpoint(["POST"])
def material_relation(request, pk):
    from .events import relate

    relation = relate(
        request.watch_member,
        pk,
        integer(request.POST.get("target"), 0),
        request.POST.get("kind", ""),
        request.POST.get("reason", ""),
        integer(request.POST.get("expected"), 0),
    )
    messages.success(
        request,
        "关系已记录。仅确认无新增信息的重复材料共用分析；进展和矛盾仍保留独立分析。",
    )
    return redirect("investment_watch:news_detail", pk=relation.source.material_id)
