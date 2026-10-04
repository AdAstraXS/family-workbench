from datetime import date
from calendar import monthrange
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import OuterRef, Subquery
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_safe

from .guides import BASICS, GUIDES
from .models import MacroImportRun, MacroObservation, MacroSourceMapping, MacroIndicator
from .registry import SERIES


COUNTRIES = {"CN": "中国", "US": "美国"}
SPECS = {(s.country, s.code): s for s in SERIES}
THEMES = ["投资", "增长", "通胀", "景气", "就业", "房地产", "金融", "利率", "消费", "生产", "外贸", "财政"]
SOURCES = {"nbs": "国家统计局", "nbs_city": "国家统计局", "nbs_release": "国家统计局",
           "pbc": "中国人民银行", "mofcom": "商务部", "fred": "FRED", "akshare": "东方财富 · AKShare"}


def catalog(country=None, geography="北京市"):
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
    return rows


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
    reference = "50" if spec.code.startswith("PMI_") else ("100" if spec.code.startswith("HOUSE_") else None)
    urls = [{"label": label, "value": value,
             "url": query_url(request.path, **{**request.GET.dict(), "range": value, "page": None, "geography": geography})}
            for value, label in [("1", "1年"), ("3", "3年"), ("5", "5年"), ("all", "全部")]]
    return {"spec": spec, "mapping": mapping, "definition": definition, "source_name": SOURCES[spec.provider],
            "geographies": geographies, "geography": geography, "range": span, "ranges": urls,
            "page": page, "latest": latest, "chart_data": {"points": chart, "unit": spec.unit, "name": spec.name, "reference": reference},
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
    cards = [{"code": c, "name": n, "count": sum(r["spec"].country == c for r in rows)} for c, n in COUNTRIES.items()]
    recent = sorted([r for r in rows if r["latest_date"]], key=lambda r: r["latest_date"], reverse=True)[:6]
    return render(request, "macro/overview.html", {"section": "overview", "countries": cards, "recent": recent})


@login_required
@require_safe
def country(request, country):
    if country not in COUNTRIES:
        raise Http404
    all_rows = catalog(country, request.GET.get("geography", "北京市"))
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
