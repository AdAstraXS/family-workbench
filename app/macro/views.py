from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import OuterRef, Subquery
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_safe

from .models import MacroImportRun, MacroObservation, MacroSourceMapping
from .registry import SERIES


@login_required
@require_safe
def index(request):
    country = "US" if request.GET.get("country") == "US" else "CN"
    latest = MacroObservation.objects.filter(mapping=OuterRef("pk")).order_by("-period_date", "geography")
    mappings = MacroSourceMapping.objects.filter(indicator__country=country, indicator__is_active=True).select_related("indicator").annotate(
        latest_date=Subquery(latest.values("period_date")[:1]),
        latest_value=Subquery(latest.values("value")[:1]),
        latest_geography=Subquery(latest.values("geography")[:1]),
    )
    existing = {m.indicator.code: m for m in mappings}
    rows = [{"spec": s, "mapping": existing.get(s.code)} for s in SERIES if s.country == country]
    return render(request, "macro/index.html", {"country": country, "rows": rows})


@login_required
@require_safe
def detail(request, pk):
    mapping = get_object_or_404(MacroSourceMapping.objects.select_related("indicator"), pk=pk, indicator__is_active=True)
    points = mapping.observations.all()
    geographies = list(points.order_by("geography").values_list("geography", flat=True).distinct())
    geography = request.GET.get("geography", geographies[0] if geographies else "全国")
    points = points.filter(geography=geography)
    page = Paginator(points, 60).get_page(request.GET.get("page"))
    return render(request, "macro/detail.html", {"mapping": mapping, "page": page, "geographies": geographies, "geography": geography})


@login_required
@require_safe
def revisions(request, pk):
    point = get_object_or_404(MacroObservation.objects.select_related("mapping__indicator"), pk=pk, mapping__indicator__is_active=True)
    page = Paginator(point.revisions.all(), 30).get_page(request.GET.get("page"))
    return render(request, "macro/revisions.html", {"point": point, "page": page})


@login_required
@require_safe
def status(request):
    return render(request, "macro/status.html", {"page": Paginator(MacroImportRun.objects.order_by("-started_at"), 30).get_page(request.GET.get("page"))})
