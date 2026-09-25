"""Read-only homepage summaries, from recorded snapshots rather than projections."""
from decimal import Decimal

from django.db.models import Sum

from ledger.models import AssetBalanceSnapshot
from knowledge.models import KnowledgeDocument
from knowledge.permissions import accessible_documents, accessible_search_entries

PALETTE = ("#5786c8", "#4eaa9c", "#e5af58", "#a18acb", "#d27d73", "#669ba9", "#92a75b", "#bb87a6")
ZERO = Decimal("0")


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
    snapshots = list(AssetBalanceSnapshot.objects.filter(
        family=family, is_draft=False, base_currency=latest_snapshot.base_currency,
        snapshot_date__lte=today,
    ).annotate(recorded_total=Sum("entries__base_amount")).exclude(
        recorded_total__isnull=True,
    ).order_by("-snapshot_date", "-created_at")[:12])
    snapshots.reverse()
    result["asset_trend"] = snapshots
    if len(snapshots) >= 2:
        low = min(s.recorded_total for s in snapshots)
        high = max(s.recorded_total for s in snapshots)
        span = high - low
        points = []
        for i, snapshot in enumerate(snapshots):
            x = Decimal(20) + Decimal(540) * i / (len(snapshots) - 1)
            y = Decimal(95) if not span else Decimal(165) - (snapshot.recorded_total - low) / span * 140
            points.append(f"{x:.2f},{y:.2f}")
        result.update(trend_points=" ".join(points), trend_low=low, trend_high=high,
                      trend_start=snapshots[0].snapshot_date, trend_end=snapshots[-1].snapshot_date)
    groups = list(latest_snapshot.entries.order_by().values("asset_category__name").annotate(amount=Sum("base_amount")).order_by("-amount", "asset_category__name"))
    total = sum((g["amount"] or ZERO for g in groups), ZERO)
    can_draw = total > 0 and all((g["amount"] or ZERO) >= 0 for g in groups)
    offset = ZERO
    for i, group in enumerate(groups):
        amount = group["amount"] or ZERO
        percent = amount / total * 100 if can_draw else ZERO
        result["asset_allocation"].append({
            "label": group["asset_category__name"] or "未分类", "amount": amount,
            "percent": f"{percent:.1f}", "dash": f"{percent:.5f}",
            "gap": f"{100-percent:.5f}", "offset": f"{-offset:.5f}",
            "color": PALETTE[i % len(PALETTE)],
        })
        offset += percent
    result["allocation_can_draw"] = can_draw
    return result
