from datetime import date, timedelta
from calendar import monthrange, Calendar
from collections import defaultdict
from decimal import Decimal
import re
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import OuterRef, Subquery, Count, Min, Max
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_safe
from django.utils import timezone

from .guides import BASICS, GUIDES
from .models import MacroImportRun, MacroObservation, MacroSourceMapping, MacroIndicator
from .registry import SERIES
from .calendar import schedule, planned_events
from .housing import CITIES, HOUSING_COLUMNS
from .presentation import AUXILIARY, PRESENTATION_CODES, CPI_BASES, CN_PAIRS, profile, shifted, presentation, values


COUNTRIES = {"CN": "中国", "US": "美国"}
SPECS = {(s.country, s.code): s for s in SERIES}
THEMES = ["投资", "增长", "通胀", "景气", "就业", "房地产", "金融", "利率", "消费", "生产", "外贸", "财政"]
SOURCES = {"nbs": "国家统计局", "nbs_city": "国家统计局", "nbs_release": "国家统计局",
           "pbc": "中国人民银行", "mofcom": "商务部", "fred": "FRED", "akshare": "东方财富 · AKShare"}


def catalog(country=None, geography="北京市", with_presentation=False):
    # A national series can leave this parameter in a country URL; city series
    # have no national aggregate and always need an explicit city.
    if geography == "全国":
        geography = "北京市"
    disabled = set(MacroIndicator.objects.filter(is_active=False).values_list("country", "code"))
    mappings = MacroSourceMapping.objects.filter(indicator__is_active=True).select_related("indicator")
    if country:
        mappings = mappings.filter(indicator__country=country)
    latest = MacroObservation.objects.filter(mapping=OuterRef("pk")).order_by("-period_date", "geography")
    city_latest = MacroObservation.objects.filter(mapping=OuterRef("pk"), geography=geography).order_by("-period_date")
    mappings = mappings.annotate(
        latest_date=Subquery(latest.values("period_date")[:1]), latest_value=Subquery(latest.values("value")[:1]),
        city_date=Subquery(city_latest.values("period_date")[:1]), city_value=Subquery(city_latest.values("value")[:1]),
    )
    existing = {(m.indicator.country, m.indicator.code): m for m in mappings}
    rows = []
    for spec in SERIES:
        key = (spec.country, spec.code)
        if (country and spec.country != country) or key in disabled:
            continue
        mapping = existing.get(key)
        city = spec.provider == "nbs_city"
        rows.append({"spec": spec, "mapping": mapping, "country_name": COUNTRIES[spec.country],
                     "source_name": SOURCES[spec.provider], "guide": GUIDES[key],
                     "latest_date": (mapping.city_date if city else mapping.latest_date) if mapping else None,
                     "latest_value": (mapping.city_value if city else mapping.latest_value) if mapping else None,
                     "geography": geography if city else "全国",
                     "url": query_url(reverse("macro:indicator", args=key), geography=geography) if city else reverse("macro:indicator", args=key),
                     "guide_url": query_url(reverse("macro:guide", args=key), geography=geography) if city else reverse("macro:guide", args=key)})
    if with_presentation:
        periods = set()
        for row in rows:
            if row["latest_date"]:
                d = row["latest_date"]
                periods.update([d, shifted(d, -1), shifted(d, -3), shifted(d, -12)])
        history = observation_history(country, geography, periods=periods)
        for row in rows:
            row["presentation"] = presentation(row["spec"], row["latest_date"], row["latest_value"], history)
    return rows


def observation_history(country, geography="北京市", periods=None, codes=None):
    points = MacroObservation.objects.filter(mapping__indicator__is_active=True,
        mapping__indicator__code__in=codes if codes is not None else PRESENTATION_CODES,
        geography__in=["全国", geography])
    if country:
        points = points.filter(mapping__indicator__country=country)
    if periods is not None:
        points = points.filter(period_date__in=periods)
    return {(country, code, period): value for country, code, period, value in points.order_by().values_list(
        "mapping__indicator__country", "mapping__indicator__code", "period_date", "value")}


def actual_releases(rows, observations):
    active = {(r["spec"].country, r["spec"].code): r for r in rows}
    groups = {}
    for point in observations.select_related("mapping__indicator").order_by("-release_date", "-period_date", "geography").iterator():
        indicator = point.mapping.indicator
        row = active.get((indicator.country, indicator.code))
        if not row:
            continue
        group = "nbs_house" if point.mapping.group.startswith("nbs_house_") else point.mapping.group
        key = (indicator.country, group, point.release_date, point.period_date)
        if key not in groups:
            title = {"nbs_house": "70城住宅价格", "pbc": "金融统计与社会融资报告", "mofcom": "全国吸收外资报告"}.get(group, row["spec"].name)
            groups[key] = {"country": indicator.country, "country_name": COUNTRIES[indicator.country], "day": point.release_date,
                "kind": "actual", "title": title,
                "period_date": point.period_date, "period": period_label(point.period_date, row["spec"].frequency),
                "indicators": [], "source_name": SOURCES[row["spec"].provider]}
        if row not in groups[key]["indicators"]:
            groups[key]["indicators"].append(row)
    return sorted(groups.values(), key=lambda e: (e["day"], e["period_date"], e["country"], e["title"]), reverse=True)


def query_url(path, **params):
    return path + "?" + urlencode({k: v for k, v in params.items() if v not in (None, "")})


def period_label(d, frequency):
    if frequency == "月度":
        return f"{d.year}年{d.month}月"
    if frequency == "季度":
        return f"{d.year}年 第{(d.month-1)//3+1}季度"
    if frequency == "年度":
        return f"{d.year}年"
    return d.isoformat()


def series_context(request, spec, mapping):
    geographies = list(mapping.observations.order_by("geography").values_list("geography", flat=True).distinct()) if mapping else []
    default = "北京市" if "北京市" in geographies else (geographies[0] if geographies else "全国")
    geography = request.GET.get("geography", default)
    if geographies and geography not in geographies:
        raise Http404("没有该地区的数据")
    span = request.GET.get("range", "3")
    if span not in {"1", "3", "5", "all"}:
        span = "3"
    points = mapping.observations.filter(geography=geography) if mapping else MacroObservation.objects.none()
    latest = points.order_by("-period_date").first()
    if latest and span != "all":
        d = latest.period_date
        target_year = d.year - int(span)
        cutoff = date(target_year, d.month, min(d.day, monthrange(target_year, d.month)[1]))
        points = points.filter(period_date__gte=cutoff)
    chart = [{"date": p.period_date.isoformat(), "label": period_label(p.period_date, spec.frequency),
              "value": str(p.value) if p.value is not None else None} for p in points.order_by("period_date")]
    page = Paginator(points.order_by("-period_date"), 30).get_page(request.GET.get("page"))
    definition = mapping.definition if mapping else spec.definition()
    growth_profile = profile(spec)
    growth = presentation(spec, latest.period_date if latest else None, latest.value if latest else None, {})
    measures = []
    measure = "level"
    if growth_profile:
        # Companion values include observations before the chart cutoff. A short
        # chart must still calculate its first point from its exact prior year.
        history = observation_history(spec.country, geography, codes={spec.code, CPI_BASES.get(spec.code, spec.code), CN_PAIRS.get(spec.code, spec.code)})
        growth = presentation(spec, latest.period_date if latest else None, latest.value if latest else None, history)
        allowed = {m.key: m for m in growth_profile["modes"]}
        requested = request.GET.get("measure", growth_profile["default"])
        measure = requested if requested in allowed or requested == "level" else growth_profile["default"]
        measures = [{"key": m.key, "label": m.label, "url": query_url(request.path, **{**request.GET.dict(), "measure": m.key, "page": None})} for m in growth_profile["modes"]]
        measures.append({"key": "level", "label": "原始值", "url": query_url(request.path, **{**request.GET.dict(), "measure": "level", "page": None})})
        for point in page:
            point.presentation = presentation(spec, point.period_date, point.value, history)
        if measure != "level":
            chart = [{**p, "value": str(v) if (v := values(spec, date.fromisoformat(p["date"]),
                None if p["value"] is None else Decimal(p["value"]), history).get(measure)) is not None else None} for p in chart]
    reference = "50" if spec.code.startswith("PMI_") else ("100" if spec.code.startswith("HOUSE_") else None)
    chart_unit = spec.unit
    chart_label = "原始值"
    if growth_profile and measure != "level":
        mode = next(m for m in growth_profile["modes"] if m.key == measure)
        chart_unit, chart_label = mode.unit, mode.label
        reference = "0" if mode.unit == "%" else None
    urls = [{"label": label, "value": value,
             "url": query_url(request.path, **{**request.GET.dict(), "range": value, "page": None, "geography": geography})}
            for value, label in [("1", "1年"), ("3", "3年"), ("5", "5年"), ("all", "全部")]]
    return {"spec": spec, "mapping": mapping, "definition": definition, "source_name": SOURCES[spec.provider],
            "geographies": geographies, "geography": geography, "range": span, "ranges": urls,
            "page": page, "latest": latest, "growth": growth, "measures": measures, "measure": measure,
            "chart_unit": chart_unit, "chart_label": chart_label,
            "chart_data": {"points": chart, "unit": chart_unit, "name": spec.name + " · " + chart_label, "reference": reference,
                           "missing_label": "暂无可比数据" if growth_profile and measure != "level" else "来源缺值"},
            "chart_count": len(chart), "chart_start": chart[0]["label"] if chart else "", "chart_end": chart[-1]["label"] if chart else "",
            "guide_url": query_url(reverse("macro:guide", args=[spec.country, spec.code]), geography=geography) if spec.provider == "nbs_city" else reverse("macro:guide", args=[spec.country, spec.code]),
            "previous_url": query_url(request.path, **{**request.GET.dict(), "page": page.previous_page_number()}) if page.has_previous() else None,
            "next_url": query_url(request.path, **{**request.GET.dict(), "page": page.next_page_number()}) if page.has_next() else None}


@login_required
@require_safe
def index(request):
    if request.GET.get("country") in COUNTRIES:
        return country(request, request.GET["country"])
    rows = catalog()
    cards = [{"code": c, "name": n, "count": sum(r["spec"].country == c and r["spec"].code not in AUXILIARY for r in rows)} for c, n in COUNTRIES.items()]
    mapped = [r["mapping"].pk for r in rows if r["mapping"]]
    recent_dates = list(MacroObservation.objects.filter(mapping_id__in=mapped, release_date__isnull=False)
        .order_by("-release_date").values_list("release_date", flat=True).distinct()[:6])
    recent = actual_releases(rows, MacroObservation.objects.filter(release_date__in=recent_dates, mapping_id__in=mapped))[:6]
    today = timezone.localdate()
    return render(request, "macro/overview.html", {"section": "overview", "countries": cards, "recent": recent,
        "upcoming": planned_events(today, today + timedelta(days=7))[:5]})


@login_required
@require_safe
def country(request, country):
    if country not in COUNTRIES:
        raise Http404
    all_rows = [r for r in catalog(country, request.GET.get("geography", "北京市"), with_presentation=True) if r["spec"].code not in AUXILIARY]
    available = {r["spec"].category for r in all_rows}
    theme = request.GET.get("theme", "投资" if country == "CN" else "通胀")
    if theme not in available and theme != "全部":
        theme = "全部"
    rows = [r for r in all_rows if theme == "全部" or r["spec"].category == theme]
    if country == "CN" and theme == "投资":
        rows.sort(key=lambda r: 0 if r["spec"].code == "FAI_CUM_YOY" else 1)
    selected = next((r for r in rows if r["spec"].code == request.GET.get("series")), rows[0] if rows else None)
    context = {"country": country, "country_name": COUNTRIES[country], "section": country,
               "rows": rows, "theme": theme, "themes": ["全部"] + [t for t in THEMES if t in available],
               "selected": selected, "summary_rows": rows[:3]}
    if country == "CN" and theme == "投资":
        context["summary_rows"] = [r for code in ["FAI_CUM_YOY", "MANUFACTURING_FAI_CUM_YOY", "FDI_CUM"] for r in rows if r["spec"].code == code]
    if selected:
        context.update(series_context(request, selected["spec"], selected["mapping"]))
    return render(request, "macro/index.html", context)


def indicator_page(request, spec, mapping):
    context = series_context(request, spec, mapping)
    context.update({"country": spec.country, "country_name": COUNTRIES[spec.country], "section": spec.country,
                    "tab": request.GET.get("tab", "history"), "guide": GUIDES[(spec.country, spec.code)]})
    if context["tab"] not in {"history", "definition"}:
        context["tab"] = "history"
    return render(request, "macro/detail.html", context)


@login_required
@require_safe
def indicator(request, country, code):
    if (country, code) not in SPECS or MacroIndicator.objects.filter(country=country, code=code, is_active=False).exists():
        raise Http404
    mapping = MacroSourceMapping.objects.filter(indicator__country=country, indicator__code=code, indicator__is_active=True).select_related("indicator").first()
    return indicator_page(request, SPECS[(country, code)], mapping)


@login_required
@require_safe
def detail(request, pk):
    mapping = get_object_or_404(MacroSourceMapping.objects.select_related("indicator"), pk=pk, indicator__is_active=True)
    spec = SPECS.get((mapping.indicator.country, mapping.indicator.code))
    if not spec:
        raise Http404
    return indicator_page(request, spec, mapping)


@login_required
@require_safe
def encyclopedia(request):
    q = request.GET.get("q", "").strip()[:200]
    country = request.GET.get("country", "")
    country = country if country in COUNTRIES else ""
    theme = request.GET.get("theme", "全部")
    rows = catalog(country)
    themes = ["全部"] + [t for t in THEMES if any(r["spec"].category == t for r in rows)]
    if theme not in themes:
        theme = "全部"
    rows = [r for r in rows if (theme == "全部" or r["spec"].category == theme) and
            (not q or q.casefold() in f'{r["spec"].name} {r["spec"].code} {r["guide"]["aliases"]} {r["guide"]["lead"]}'.casefold())]
    page = Paginator(rows, 12).get_page(request.GET.get("page"))
    params = {"q": q, "country": country, "theme": theme}
    return render(request, "macro/encyclopedia.html", {"section": "learn", "rows": page, "page": page, "result_count": len(rows), "q": q,
        "country": country, "theme": theme, "themes": themes, "basics": BASICS,
        "previous_url": query_url(request.path, **params, page=page.previous_page_number()) if page.has_previous() else None,
        "next_url": query_url(request.path, **params, page=page.next_page_number()) if page.has_next() else None})


@login_required
@require_safe
def guide(request, country, code):
    if country not in COUNTRIES:
        raise Http404
    rows = catalog(country, request.GET.get("geography", "北京市"))
    row = next((r for r in rows if r["spec"].code == code), None)
    if not row:
        raise Http404
    related = [r for r in rows if r != row and f'{r["spec"].country}:{r["spec"].code}' in row["guide"]["related"]]
    return render(request, "macro/guide.html", {"section": "learn", "row": row, "guide": row["guide"], "related": related})


@login_required
@require_safe
def revisions(request, pk):
    point = get_object_or_404(MacroObservation.objects.select_related("mapping__indicator"), pk=pk, mapping__indicator__is_active=True)
    return render(request, "macro/revisions.html", {"section": point.mapping.indicator.country, "point": point,
                                                   "page": Paginator(point.revisions.all(), 30).get_page(request.GET.get("page"))})


@login_required
@require_safe
def status(request):
    return render(request, "macro/status.html", {"section": "sources", "page": Paginator(MacroImportRun.objects.order_by("-started_at"), 30).get_page(request.GET.get("page"))})


SOURCE_METHODS = {
    "nbs": ("国家统计局", "https://data.stats.gov.cn/", "国家数据历史表，经 AKShare 读取；按指标、时期和城市映射。", "返回的统计期决定实际历史范围，发布库与新闻稿更新时间可能不同。"),
    "pbc": ("中国人民银行", "https://www.pbc.gov.cn/", "读取并核对指定官方报告页面。", "社融、信贷按报告逐期积累；当前尚未自动发现新报告。"),
    "mofcom": ("商务部", "https://www.mofcom.gov.cn/", "读取并核对指定吸收外资发布稿。", "累计金额、同比、行业及企业数量分别记录；当前尚未自动发现新报告。"),
    "fred": ("FRED · 圣路易斯联储", "https://fred.stlouisfed.org/", "公开 CSV 序列；原始发布机构见指标百科及 FRED 系列说明。", "FRED 是数据分发平台；来源缺值保留，统计期不是官方发布日期。"),
    "akshare": ("东方财富 · AKShare", "https://data.eastmoney.com/cjsj/", "AKShare 读取东方财富的宏观历史表。", "第三方转发数据；原统计机构和方法参考见指标百科，不能把采集日当作发布日期。"),
}


@login_required
@require_safe
def sources(request):
    country = request.GET.get("country", "")
    country = country if country in COUNTRIES else ""
    q = request.GET.get("q", "").strip()[:200]
    rows = catalog(country)
    stats = {s["mapping_id"]: s for s in MacroObservation.objects.filter(mapping__indicator__is_active=True)
             .order_by().values("mapping_id").annotate(total=Count("id"), valid=Count("value"), cities=Count("geography", distinct=True),
                                                      first=Min("period_date"), last=Max("period_date"), seen=Max("last_seen_at"))}
    groups = {key: {"key": key, "name": info[0], "url": info[1], "method": info[2], "note": info[3], "count": 0, "loaded": 0}
              for key, info in SOURCE_METHODS.items()}
    for row in rows:
        provider = "nbs" if row["spec"].provider.startswith("nbs") else row["spec"].provider
        row["source_group"] = groups[provider]
        row["stats"] = stats.get(row["mapping"].pk, {}) if row["mapping"] else {}
        row["missing"] = row["stats"].get("total", 0) - row["stats"].get("valid", 0)
        groups[provider]["count"] += 1
        groups[provider]["loaded"] += bool(row["stats"])
    matches = [r for r in rows if not q or q.casefold() in f'{r["spec"].name} {r["spec"].code} {r["source_group"]["name"]}'.casefold()]
    return render(request, "macro/sources.html", {"section": "sources", "groups": [g for g in groups.values() if g["count"]],
        "rows": matches, "country": country, "q": q, "count": len(matches)})


def selected_month(request, default):
    raw = request.GET.get("month", default.strftime("%Y-%m"))
    if not re.fullmatch(r"\d{4}-\d{2}", raw):
        raise Http404("月份格式应为 YYYY-MM")
    try:
        result = date.fromisoformat(raw + "-01")
        if not 1900 <= result.year <= 2100:
            raise ValueError
        return result
    except ValueError:
        raise Http404("无效月份")


@login_required
@require_safe
def housing_cities(request):
    codes = [c[0] for c in HOUSING_COLUMNS]
    points = MacroObservation.objects.filter(mapping__indicator__country="CN", mapping__indicator__code__in=codes,
                                             mapping__indicator__is_active=True)
    months = list(points.order_by("-period_date").values_list("period_date", flat=True).distinct())
    month = selected_month(request, months[0] if months else timezone.localdate().replace(day=1))
    values = {(p.geography, p.mapping.indicator.code): p for p in points.filter(period_date=month).select_related("mapping__indicator")}
    disabled = set(MacroIndicator.objects.filter(country="CN", code__in=codes, is_active=False).values_list("code", flat=True))
    rows, valid, missing, absent = [], 0, 0, 0
    for city in CITIES:
        cells = []
        for code, _, _ in HOUSING_COLUMNS:
            point = values.get((city, code))
            state = "disabled" if code in disabled else "present" if point else "absent"
            if point and point.value is not None: valid += 1
            elif point: missing += 1
            elif state != "disabled": absent += 1
            cells.append({"point": point, "state": state, "change": point.value - 100 if point and point.value is not None else None,
                          "url": query_url(reverse("macro:indicator", args=["CN", code]), geography=city) if point else ""})
        rows.append({"city": city, "cells": cells})
    return render(request, "macro/housing_cities.html", {"section": "CN", "rows": rows, "columns": HOUSING_COLUMNS, "month": month,
        "months": sorted(set(months + [month]), reverse=True), "valid": valid, "missing": missing, "absent": absent,
        "disabled_count": len(disabled) * len(CITIES)})


@login_required
@require_safe
def release_calendar(request):
    month = selected_month(request, timezone.localdate().replace(day=1))
    end = month.replace(day=monthrange(month.year, month.month)[1])
    country = request.GET.get("country", "")
    country = country if country in COUNTRIES else ""
    kind = request.GET.get("kind", "all")
    kind = kind if kind in {"all", "planned", "actual"} else "all"
    active = {(r["spec"].country, r["spec"].code): r for r in catalog(country)}
    plans = planned_events(month, end, country)
    for event in plans:
        event["indicators"] = [active[(event["country"], code)] for code in event["codes"] if (event["country"], code) in active]
        event["elapsed"] = event["when"] < timezone.now()
    plans = [e for e in plans if e["indicators"]]
    observations = MacroObservation.objects.filter(mapping__indicator__is_active=True, release_date__range=(month, end))
    if country:
        observations = observations.filter(mapping__indicator__country=country)
    actual = actual_releases(list(active.values()), observations)
    events = (plans if kind != "actual" else []) + (actual if kind != "planned" else [])
    by_day = defaultdict(list)
    for e in events:
        by_day[e["day"]].append(e)
    for day_events in by_day.values():
        day_events.sort(key=lambda e: (e["kind"] != "planned", e.get("when", timezone.now()), e["title"]))
    weeks = [[{"date": d, "in_month": d.month == month.month, "events": by_day[d]} for d in week]
             for week in Calendar(firstweekday=0).monthdatescalendar(month.year, month.month)]
    prev = (month - timedelta(days=1)).replace(day=1)
    following = (end + timedelta(days=1))
    return render(request, "macro/calendar.html", {"section": "calendar", "month": month, "country": country, "kind": kind,
        "weeks": weeks, "days": [{"date": d, "events": es} for d, es in sorted(by_day.items()) if es and month <= d <= end],
        "planned_count": len(plans), "actual_count": len(actual), "snapshot": schedule(), "verified_year": month.year == schedule()["year"],
        "previous_url": query_url(request.path, month=prev.strftime("%Y-%m"), country=country, kind=kind),
        "next_url": query_url(request.path, month=following.strftime("%Y-%m"), country=country, kind=kind)})
