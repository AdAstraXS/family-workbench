"""Read-only, source-labelled valuation scenarios for a research draft.

The model may discuss assumptions, but all displayed arithmetic uses Decimal.
Missing or incompatible price/EPS data never becomes a made-up starting point.
"""

from decimal import Decimal, InvalidOperation

from django.utils import timezone

from portfolio.models import SecurityMarketSnapshot


GROWTH_RATES = (Decimal("0.10"), Decimal("0.15"), Decimal("0.20"))
YEARS = (1, 3, 5)


def _decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _money(value):
    return value.quantize(Decimal("0.01"))


def build_valuation_trial(security, scope, query):
    """Return labelled inputs and scenarios without fetching a live quote."""
    snapshot = SecurityMarketSnapshot.objects.filter(security=security).first()
    if not snapshot or not snapshot.last_price or snapshot.last_price <= 0 or not snapshot.price_as_of:
        return {"available": False, "problem": "尚无带时点的已保存股价。请先到自选股的“行情与估值”更新行情。"}
    price = snapshot.last_price
    quote = {"price": _money(price), "currency": security.currency,
             "price_as_of": timezone.localtime(snapshot.price_as_of),
             "price_source": snapshot.get_price_source_display(),
             "pricing_status": snapshot.get_pricing_status_display(),
             "is_delayed": snapshot.is_delayed,
             "provider_pe_ttm": snapshot.pe_ttm_ratio}
    annual = (scope or {}).get("valuation_basis") or {}
    eps = _decimal(annual.get("eps"))
    annual_ok = (security.currency == "USD" and eps is not None and eps > 0
                 and annual.get("period_end") and annual.get("citation"))
    if annual_ok:
        basis_label = "SEC 年报 GAAP 稀释 EPS"
        basis_period = annual["period_end"]
        basis_citation = annual["citation"]
        basis_note = "年报 EPS 与当前行情日期不同；含可能的非经常性收益，未作正常化调整。"
    else:
        provider_pe = snapshot.pe_ttm_ratio
        if provider_pe is None or provider_pe <= 0:
            return {**quote, "available": False,
                    "problem": "已有股价，但缺少同一口径的 EPS 或正值 TTM PE，暂不能试算。"}
        eps = price / provider_pe
        basis_label = "行情源 TTM PE 反推的每股收益"
        basis_period = "行情快照时点"
        basis_citation = None
        basis_note = "这是价格 ÷ 行情源 TTM PE 的反推值，不等于 SEC 年报 GAAP EPS；行情源盈利调整口径待核对。"
    starting_pe = price / eps
    try:
        years = int(query.get("years", 5))
    except (TypeError, ValueError):
        years = 5
    if years not in YEARS:
        years = 5
    exit_pe = _decimal(query.get("exit_pe"))
    if exit_pe is None or not Decimal("1") <= exit_pe <= Decimal("200"):
        exit_pe = starting_pe.quantize(Decimal("0.01"))
    rows = []
    for rate in GROWTH_RATES:
        terminal_eps = eps * (Decimal(1) + rate) ** years
        terminal_price = terminal_eps * exit_pe
        annual_return = ((terminal_price / price) ** (Decimal(1) / Decimal(years))
                         - Decimal(1)) * Decimal(100)
        rows.append({"growth": int(rate * 100), "eps": _money(terminal_eps),
                     "price": _money(terminal_price),
                     "annual_return": annual_return.quantize(Decimal("0.01"))})
    return {
        **quote, "available": True,
        "eps": _money(eps), "basis_label": basis_label,
        "basis_period": basis_period, "basis_citation": basis_citation,
        "basis_note": basis_note, "starting_pe": starting_pe.quantize(Decimal("0.01")),
        "years": years, "exit_pe": exit_pe, "rows": rows,
    }
