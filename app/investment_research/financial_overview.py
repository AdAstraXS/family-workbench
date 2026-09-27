"""Read-only financial overview from an archived, cited SEC 10-K.

Every displayed fact has a unique matching line in the saved Item 8 text.  The
view never fetches data or saves derived values; changing the archived version
cannot silently move a citation to a different source.
"""

import gzip
from datetime import date
from decimal import Decimal

from .tenk_chapters import tenk_chapter_coverage
from .tenk_metrics import _IXBRL, _amount, _citation, _full_year_end_dates, tenk_metric_grid


# The first matching tag is preferred. A different tag is used only when the
# preferred one is absent for that year. Dimensions are deliberately excluded.
SPECS = (
    ("revenue", "营业收入", "money", "duration",
     ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"),
     r"(?:^|\|\s*)(?:Total (?:revenue|revenues|net sales)|Revenue|Net sales)\s*\|"),
    ("gross_profit", "毛利", "money", "duration", ("GrossProfit",),
     r"(?:^|\|\s*)Gross (?:profit|margin)\s*\|"),
    ("operating_income", "营业利润", "money", "duration", ("OperatingIncomeLoss",),
     r"(?:^|\|\s*)(?:Operating income|Income from operations|Operating profit)\s*\|"),
    ("net_income", "净利润", "money", "duration",
     ("NetIncomeLoss", "ProfitLoss"),
     r"(?:^|\|\s*)Net (?:income|earnings|loss)(?: \([^|]*\))?\s*\|"),
    ("diluted_eps", "稀释每股收益", "per_share", "duration", ("EarningsPerShareDiluted",),
     r"(?:^|\|\s*)(?:Diluted(?: earnings per share)?|Earnings per share.*diluted)\s*\|"),
    ("diluted_shares", "稀释后加权平均股数", "shares", "duration",
     ("WeightedAverageNumberOfDilutedSharesOutstanding",),
     r"(?:^|\|\s*)(?:Diluted|Diluted weighted.average shares|Shares used in computing.*diluted)\s*\|"),
    ("operating_cash", "经营活动现金流", "money", "duration",
     ("NetCashProvidedByUsedInOperatingActivities",),
     r"(?:cash (?:generated|provided) by operating activities|cash from operations|net cash provided by operating activities)"),
    ("capex", "固定资产现金支出", "money", "duration",
     ("PaymentsToAcquirePropertyPlantAndEquipment",),
     r"(?:payments for acquisition of property|purchases of property and equipment|additions to property and equipment)"),
    ("cash", "现金及现金等价物", "money", "instant",
     ("CashAndCashEquivalentsAtCarryingValue",),
     r"(?:^|\|\s*)Cash and cash equivalents\s*\|"),
    ("debt_current", "一年内到期有息债务", "money", "instant",
     ("LongTermDebtCurrent",),
     r"(?:^|\|\s*)(?:Current (?:portion of )?long.term debt|Current maturities of long.term debt)\s*\|"),
    ("debt_noncurrent", "长期有息债务", "money", "instant",
     ("LongTermDebtNoncurrent",),
     r"(?:^|\|\s*)(?:Long.term debt|Long.term debt, excluding current portion)\s*\|"),
)


def _unit_matches(unit, kind):
    normalized = unit.upper().replace(" ", "")
    if kind == "money":
        return normalized == "ISO4217:USD"
    if kind == "per_share":
        return "ISO4217:USD" in normalized and "SHARES" in normalized
    return normalized == "XBRLI:SHARES"


def _fact_cell(parser, version, item8, period, spec):
    code, _, kind, period_kind, tags, label_pattern = spec
    for tag in tags:
        matches = []
        for fact in parser.facts:
            attrs = fact["attrs"]
            if attrs.get("name") != f"us-gaap:{tag}":
                continue
            context = parser.contexts.get(attrs.get("contextref")) or {}
            if context.get("dimensioned") or not _unit_matches(
                parser.units.get(attrs.get("unitref"), ""), kind
            ):
                continue
            if period_kind == "duration":
                try:
                    days = (period - date.fromisoformat(context.get("startdate", ""))).days
                except ValueError:
                    continue
                if context.get("enddate") != period.isoformat() or not 350 <= days <= 380:
                    continue
            elif context.get("instant") != period.isoformat():
                continue
            amount = _amount(fact)
            if amount is not None:
                matches.append((amount, fact))
        if not matches:
            continue
        amounts = {amount for amount, _ in matches}
        if len(amounts) != 1:
            return {"status": "同期间事实冲突，待核对"}
        amount, fact = matches[0]
        citation = _citation(version.content_text, item8, fact, label_pattern)
        if not citation:
            return {"status": "原文位置待核对"}
        scale = Decimal("100000000") if kind in {"money", "shares"} else Decimal(1)
        return {
            "amount": amount / scale, "citation": citation, "status": "已核对",
            "document_id": version.document_id, "version_id": version.pk,
            "version_number": version.version_number, "source_url": version.source_url,
            "fact_id": fact["attrs"].get("id", ""),
        }
    return {"status": "年报未核对到该项"}


def _derived(label, cells, calculate, *, unit="money"):
    values = [cell.get("amount") for cell in cells]
    if any(value is None for value in values):
        return {"status": "所需基础数字未全部核对"}
    try:
        amount = calculate(*values)
    except (ArithmeticError, ValueError):
        return {"status": "无法按相同口径计算"}
    if amount is None:
        return {"status": "分母为零或口径不适用"}
    return {"amount": amount, "derived": True, "components": [
        {"label": name, "cell": cell} for name, cell in zip(label, cells)
    ], "status": "由已核对数字计算", "unit": unit}


def _safe_ratio(numerator, denominator):
    return numerator / denominator * Decimal(100) if denominator > 0 else None


def build_financial_overview(version, historical_versions=()):
    """Return three full fiscal years, audited cells, and an explicit problem."""
    if version is None or version.document.source != "sec" or version.document.document_type != "10-k":
        return [], [], "请先保存 SEC 10-K 年报正文。"
    if not version.document.period_end:
        return [], [], "年报缺少报告期截止日。"
    try:
        raw = gzip.decompress(version.raw_gzip)
    except (OSError, EOFError):
        return [], [], "已保存的年报原件无法读取。"
    parser = _IXBRL()
    parser.feed(raw.decode("utf-8", errors="replace"))
    parser.close()
    if not parser.facts:
        return [], [], "年报原件没有可核对的 iXBRL 数字。"
    item8 = next((item for item in tenk_chapter_coverage(version)
                  if item["code"] == "8" and item["located"]), None)
    if item8 is None:
        return [], [], "尚未定位年报财务报表章节。"
    periods = list(reversed(_full_year_end_dates(parser, version.document.period_end)))
    rows = [{"code": spec[0], "label": spec[1], "unit": spec[2],
             "cells": [_fact_cell(parser, version, item8, period, spec) for period in periods]}
            for spec in SPECS]
    by_code = {row["code"]: row for row in rows}
    identity = (getattr(version.document, "security_id", None),
                str((version.document.metadata or {}).get("cik", "")))
    for index, period in enumerate(periods[:-1]):
        if not identity[0] or not identity[1]:
            break
        candidates = [old for old in historical_versions
                      if old.document.source == "sec" and old.document.document_type == "10-k"
                      and old.document.period_end == period
                      and (getattr(old.document, "security_id", None),
                           str((old.document.metadata or {}).get("cik", ""))) == identity]
        if len({old.document_id for old in candidates}) != 1 or not candidates:
            continue
        old = max(candidates, key=lambda item: (item.version_number, item.pk))
        old_periods, old_rows, old_problem = build_financial_overview(old)
        if old_problem or period not in old_periods:
            continue
        old_by_code = {row["code"]: row for row in old_rows}
        old_index = old_periods.index(period)
        for code in ("cash", "debt_current", "debt_noncurrent"):
            current_cell = by_code[code]["cells"][index]
            old_cell = old_by_code[code]["cells"][old_index]
            if "amount" not in old_cell:
                continue
            if "amount" in current_cell:
                if current_cell["amount"] != old_cell["amount"]:
                    by_code[code]["cells"][index] = {"status": "两份年报同期金额不同，待核对"}
            else:
                by_code[code]["cells"][index] = {
                    **old_cell, "status": "由该年原始 10-K 补齐",
                }

    def append(code, label, sources, calculate, *, unit="money"):
        cells = [_derived([by_code[source]["label"] for source in sources],
                          [by_code[source]["cells"][index] for source in sources],
                          calculate, unit=unit) for index in range(len(periods))]
        row = {"code": code, "label": label, "unit": unit, "cells": cells}
        rows.append(row)
        by_code[code] = row

    append("gross_margin", "毛利率", ("gross_profit", "revenue"), _safe_ratio, unit="percent")
    append("operating_margin", "营业利润率", ("operating_income", "revenue"),
           _safe_ratio, unit="percent")
    append("net_margin", "净利率", ("net_income", "revenue"), _safe_ratio, unit="percent")
    append("simple_fcf", "简化自由现金流", ("operating_cash", "capex"),
           lambda cash, capex: cash - capex if capex >= 0 else None)
    append("total_debt", "已核对有息债务", ("debt_current", "debt_noncurrent"),
           lambda current, noncurrent: current + noncurrent)
    append("net_cash", "净现金／（净负债）", ("cash", "total_debt"),
           lambda cash, debt: cash - debt)
    for source, code, label in (
        ("revenue", "revenue_growth", "营业收入同比增长"),
        ("net_income", "net_income_growth", "净利润同比增长"),
        ("diluted_eps", "eps_growth", "稀释 EPS 同比增长"),
    ):
        source_cells = by_code[source]["cells"]
        growth = [{"status": "需上一完整财年作比较"}]
        for prior, current in zip(source_cells, source_cells[1:]):
            growth.append(_derived(
                (f"上年{by_code[source]['label']}", f"本年{by_code[source]['label']}"),
                (prior, current),
                lambda old, new: (new - old) / old * Decimal(100) if old > 0 else None,
                unit="percent",
            ))
        row = {"code": code, "label": label, "unit": "percent", "cells": growth}
        rows.append(row)
        by_code[code] = row
    for row in rows:
        row["coverage"] = sum("amount" in cell for cell in row["cells"])
    # Company-specific figures are shown as separate lines: they may overlap,
    # so their sum is never presented as a decomposition of total revenue.
    _, company_rows, _ = tenk_metric_grid(version)
    for row in company_rows:
        if row["code"] not in {"company_revenue", "iphone_revenue", "automotive_revenue"}:
            continue
        period_cells = {period: cell for period, cell in zip(
            reversed(_full_year_end_dates(parser, version.document.period_end)),
            reversed(row["cells"]),
        )}
        cells = []
        for period in periods:
            cell = period_cells.get(period, {"status": "年报未核对到该项"}).copy()
            if "amount" in cell:
                cell["document_id"] = cell.get("source_document_id")
                cell["version_id"] = cell.get("source_version_id")
            cells.append(cell)
        rows.append({"code": row["code"], "label": row["label"], "unit": "money",
                     "cells": cells, "coverage": sum("amount" in cell for cell in cells),
                     "company_specific": True})
    return periods, rows, None
