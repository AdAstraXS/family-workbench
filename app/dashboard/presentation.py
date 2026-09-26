"""Read-only homepage summaries, from recorded snapshots rather than projections."""
from datetime import date
from decimal import Decimal

from django.db.models import Sum

from ledger.models import AssetBalanceSnapshot
from knowledge.models import KnowledgeDocument
from knowledge.permissions import accessible_documents, accessible_search_entries

PALETTE = ("#5786c8", "#4eaa9c", "#e5af58", "#a18acb", "#d27d73", "#669ba9", "#92a75b", "#bb87a6")
ZERO = Decimal("0")


def _month_start_offset(today, months_back):
    month_index = today.year * 12 + today.month - 1 - months_back
    return date(month_index // 12, month_index % 12 + 1, 1)


def homepage_details(family, member, latest_snapshot, today):
    result = {"asset_trend": [], "asset_allocation": [], "recent_knowledge": [], "pending_knowledge": 0}
    if member:
        result["recent_knowledge"] = list(accessible_documents(member).filter(
            owner=member, library_tier=KnowledgeDocument.LIBRARY_KNOWLEDGE,
            knowledge_status=KnowledgeDocument.KNOWLEDGE_INCLUDED,
        ).select_related(None).select_related("source")[:3])
        result["pending_knowledge"] = accessible_search_entries(member).filter(
            owner=member, knowledge_status=KnowledgeDocument.KNOWLEDGE_PENDING,
        ).count()
    if not latest_snapshot:
        return result
    first_month = _month_start_offset(today, 11)
    snapshots = AssetBalanceSnapshot.objects.filter(
        family=family, is_draft=False, base_currency=latest_snapshot.base_currency,
        snapshot_date__gte=first_month, snapshot_date__lte=today,
    ).annotate(recorded_total=Sum("entries__base_amount")).exclude(
        recorded_total__isnull=True,
    ).order_by("-snapshot_date", "-created_at")
    # Multiple snapshots in one calendar month represent revisions; use the latest.
    monthly = {}
    for snapshot in snapshots:
        monthly.setdefault((snapshot.snapshot_date.year, snapshot.snapshot_date.month), snapshot)
        if len(monthly) == 12:
            break
    snapshots = sorted(monthly.values(), key=lambda item: item.snapshot_date)
    result["asset_trend"] = snapshots
    result["trend_ticks"] = [
        {"date": _month_start_offset(today, 11 - index), "label": f"{_month_start_offset(today, 11 - index).month}月"}
        for index in range(12)
    ]
    result["trend_missing_months"] = 12 - len(snapshots)
    if snapshots:
        low = min(s.recorded_total for s in snapshots)
        high = max(s.recorded_total for s in snapshots)
        span = high - low
        points = []
        point_details = []
        first_month_index = first_month.year * 12 + first_month.month
        for index, snapshot in enumerate(snapshots):
            month_index = snapshot.snapshot_date.year * 12 + snapshot.snapshot_date.month - first_month_index
            x = Decimal(20) + Decimal(540) * month_index / 11
            y = Decimal(95) if not span else Decimal(165) - (snapshot.recorded_total - low) / span * 140
            points.append(f"{x:.2f},{y:.2f}")
            point_details.append({
                "x": f"{x:.2f}", "y": f"{y:.2f}",
                "label_y": f"{y - 11 if index % 2 == 0 else y + 19:.2f}",
                "wan": f"{snapshot.recorded_total / Decimal('10000'):.2f}",
                "snapshot": snapshot,
            })
        result.update(trend_points=" ".join(points), trend_chart_points=point_details,
                      trend_area=f"{point_details[0]['x']},185 " + " ".join(points) + f" {point_details[-1]['x']},185",
                      trend_low_wan=f"{low / Decimal('10000'):.2f}",
                      trend_high_wan=f"{high / Decimal('10000'):.2f}",
                      trend_mid_wan=f"{(low + high) / Decimal('20000'):.2f}",
                      trend_start=snapshots[0].snapshot_date, trend_end=snapshots[-1].snapshot_date)
    # Match ledger's family allocation: omit non-positive entries BEFORE grouping.
    # Net snapshot totals and historical trend values remain unchanged.
    groups = list(latest_snapshot.entries.filter(base_amount__gt=0).order_by().values("asset_category__name").annotate(amount=Sum("base_amount")).order_by("-amount", "asset_category__name"))
    total = sum((g["amount"] or ZERO for g in groups), ZERO)
    can_draw = total > 0
    result["allocation_total"] = total
    result["allocation_total_wan"] = total / Decimal("10000")
    result["allocation_liabilities"] = list(
        latest_snapshot.entries.filter(base_amount__lt=0).order_by()
        .values("asset_category__name").annotate(amount=Sum("base_amount"))
        .order_by("asset_category__name")
    )
    offset = ZERO
    for i, group in enumerate(groups):
        amount = group["amount"] or ZERO
        percent = amount / total * 100 if can_draw else ZERO
        # Demo's small gaps separate categories without hiding tiny slices.
        visible_arc = percent - min(Decimal("1.3"), percent / 4) if len(groups) > 1 else percent
        result["asset_allocation"].append({
            "label": group["asset_category__name"] or "未分类", "amount": amount,
            "percent": f"{percent:.1f}", "dash": f"{visible_arc:.5f}",
            "gap": f"{100-visible_arc:.5f}", "offset": f"{-offset:.5f}",
            "color": PALETTE[i % len(PALETTE)],
        })
        offset += percent
    result["allocation_can_draw"] = can_draw
    return result
