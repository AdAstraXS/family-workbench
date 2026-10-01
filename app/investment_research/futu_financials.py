"""Optional, explicitly refreshed Futu OpenD financial reference data.

Futu's normalized statements are kept separate from cited SEC filing facts.
The SDK supplies floats; convert at the boundary and never calculate from them
with binary floating point arithmetic.
"""

import math
import re
import socket
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.utils import timezone

from .futu_field_labels import reviewed_label
from .models import FutuFinancialSnapshot
from .number_display import money as display_money, number


class FutuFinancialError(Exception):
    pass


STATEMENTS = ((1, "利润表"), (2, "资产负债表"), (3, "现金流量表"), (4, "主要指标"))
BREAKDOWN_TYPES = {1: "产品", 2: "行业", 4: "地区", 8: "业务"}
BREAKDOWN_TYPE_NAMES = {
    "PRODUCT": "产品", "INDUSTRY": "行业", "REGION": "地区", "BUSINESS": "业务",
}


def provider_code(security):
    market = (security.market or "").upper()
    if market == "US" and re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", security.symbol.upper()):
        return f"US.{security.symbol.upper()}"
    if market == "HK" and security.symbol.isdigit():
        return f"HK.{security.symbol.zfill(5)}"
    if market in {"CN", "CN_B"} and security.symbol.isdigit() and len(security.symbol) == 6:
        prefix = (security.exchange or "").upper()
        if prefix not in {"SH", "SZ"}:
            prefix = "SH" if security.symbol.startswith(("5", "6", "9")) else "SZ"
        return f"{prefix}.{security.symbol}"
    raise FutuFinancialError("这只证券尚无明确的富途股票代码，无法查询公司财务数据。")


def _decimal_string(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return str(number) if number.is_finite() else None


def _statement_data(payload, *, annual_only=True):
    if not isinstance(payload, dict):
        raise FutuFinancialError("富途返回的财务报表格式不正确。")
    names = {}
    for field in payload.get("structure_list") or []:
        if isinstance(field, dict) and field.get("field_id") is not None:
            names[str(field["field_id"])] = str(field.get("display_name") or "")[:120]
    reports = []
    for report in (payload.get("report_list") or [])[:3]:
        if not isinstance(report, dict):
            continue
        period = str(report.get("period_text") or "")[:32]
        if annual_only and not period.endswith("/FY"):
            continue
        items = []
        for item in (report.get("item_list") or [])[:250]:
            if not isinstance(item, dict):
                continue
            amount = _decimal_string(item.get("data"))
            if amount is None:
                continue
            items.append({
                "field_id": item.get("field_id"),
                "name": str(item.get("display_name") or
                            names.get(str(item.get("field_id"))) or "")[:120],
                "amount": amount,
                "yoy": _decimal_string(item.get("yoy")),
            })
        reports.append({
            "period": period, "fiscal_year": report.get("fiscal_year"),
            "period_end": str(report.get("date_time_str") or "")[:10],
            "currency": str(report.get("currency_code") or "")[:8].upper(),
            "standards": str(report.get("accounting_standards") or "")[:80],
            "auditor": str(report.get("auditor_report") or "")[:80],
            "items": items,
        })
    return sorted(reports, key=lambda item: item["period_end"], reverse=True)


def _breakdown_data(payload):
    if not isinstance(payload, dict):
        return None
    groups = []
    for group in (payload.get("breakdown_list") or [])[:8]:
        if not isinstance(group, dict):
            continue
        items = []
        for item in (group.get("item_list") or [])[:100]:
            if not isinstance(item, dict):
                continue
            items.append({"name": str(item.get("name") or "")[:150],
                          "amount": _decimal_string(item.get("main_oper_income")),
                          "ratio": _decimal_string(item.get("ratio"))})
        raw_type = group.get("type")
        label = BREAKDOWN_TYPES.get(raw_type)
        if label is None and isinstance(raw_type, str):
            label = BREAKDOWN_TYPE_NAMES.get(raw_type.rsplit("_", 1)[-1].upper())
        groups.append({"type": label or "未标注维度", "items": items})
    return {"period": str(payload.get("period") or "")[:32],
            "currency": str(payload.get("currency_code") or "")[:8].upper(),
            "groups": groups}


def fetch_futu_financials(code, *, context_factory=None):
    """Read OpenD once per explicit refresh. Return only annual original-currency data."""
    try:
        from futu import OpenQuoteContext, RET_OK
    except ImportError as exc:
        raise FutuFinancialError("Futu SDK 尚未安装。") from exc
    try:
        socket.create_connection((settings.FUTU_OPEND_HOST, settings.FUTU_OPEND_PORT), timeout=2).close()
    except OSError as exc:
        raise FutuFinancialError("无法连接 Futu OpenD，请先检查行情网关状态。") from exc
    context = (context_factory or OpenQuoteContext)(host=settings.FUTU_OPEND_HOST,
                                                    port=settings.FUTU_OPEND_PORT)
    try:
        statements = []
        for statement_type, title in STATEMENTS:
            ret, data = context.get_financials_statements(
                code, statement_type=statement_type, financial_type=7, num=3,
            )
            if ret != RET_OK:
                raise FutuFinancialError(f"富途{title}查询失败：{str(data)[:200]}")
            statements.append({"type": statement_type, "title": title,
                               "reports": _statement_data(data)})
        ret, data = context.get_financials_revenue_breakdown(code, financial_type=7)
        breakdown = _breakdown_data(data) if ret == RET_OK else None
        breakdown_error = "" if ret == RET_OK else str(data)[:200]
        if not any(entry["reports"] for entry in statements):
            raise FutuFinancialError("富途未返回这只股票的年度财务报表。")
        return {"statements": statements, "breakdown": breakdown,
                "breakdown_error": breakdown_error}
    except FutuFinancialError:
        raise
    except Exception as exc:
        raise FutuFinancialError("富途财务数据查询失败，请稍后重试。") from exc
    finally:
        context.close()


def refresh_futu_financials(security, *, fetcher=fetch_futu_financials):
    code = provider_code(security)
    try:
        data = fetcher(code)
    except FutuFinancialError as exc:
        FutuFinancialSnapshot.objects.filter(security=security).update(last_error=str(exc)[:500])
        raise
    snapshot, _ = FutuFinancialSnapshot.objects.update_or_create(
        security=security, defaults={"provider_code": code, "data": data,
                                     "fetched_at": timezone.now(), "last_error": ""},
    )
    return snapshot


_CURRENCY_NAMES = {"USD": "美元", "CNY": "元", "HKD": "港元"}


def _display_number(value, places=2):
    try:
        parsed = number(value)
        return f"{parsed:,.{places}f}" if parsed is not None else "—"
    except (InvalidOperation, TypeError, ValueError):
        return "—"


def _display_item(item, statement_type, currency):
    amount = item.get("amount")
    if amount is None:
        return "—"
    name = (item.get("name") or "").lower()
    if statement_type == 4 and name in {"毛利率", "营业利润率", "净利率"}:
        return f"{_display_number(amount)}%"
    if statement_type in {1, 2, 3} and name:
        if re.search(r"每股|per share|\beps\b", name):
            return display_money(amount, currency, per_share=True)
        if re.search(r"股份数|股数|shares? outstanding", name):
            shares = Decimal(str(amount)) / Decimal("100000000")
            return f"{_display_number(shares)} 亿股"
        return display_money(amount, currency)
    # Older snapshots lack structure_list names. Their unit cannot be inferred
    # safely, so retain the raw magnitude with separators until refreshed.
    return _display_number(amount)


def statement_tables(statements, provider_code=""):
    """Align a provider's named fields across annual reports for scanning."""
    tables = []
    for statement in statements:
        reports = [{**report, "standards_display":
                    "US GAAP" if report.get("standards") == "US_GAAP"
                    else report.get("standards") or ""}
                   for report in statement.get("reports") or []]
        field_ids = list(dict.fromkeys(
            str(item.get("field_id"))
            for report in reports for item in report.get("items") or []
            if item.get("field_id") is not None
        ))
        rows = []
        for field_id in field_ids:
            matches = [next((item for item in report.get("items") or []
                             if str(item.get("field_id")) == field_id), None)
                       for report in reports]
            name = next((item.get("name") for item in matches
                         if item and item.get("name")), "") or reviewed_label(
                             provider_code, statement["type"], field_id)
            cells = []
            for report, item in zip(reports, matches):
                cells.append({
                    "amount": _display_item({**item, "name": name}, statement["type"],
                                            report.get("currency") or "")
                    if item else "—",
                    "yoy": f"{_display_number(item['yoy'])}%"
                    if item and item.get("yoy") is not None else "",
                })
            rows.append({"field_id": field_id, "name": name, "cells": cells})
        tables.append({"title": statement["title"], "type": statement["type"],
                       "reports": reports, "rows": [row for row in rows if row["name"]],
                       "raw_rows": [row for row in rows if not row["name"]],
                       "missing_names": sum(not row["name"] for row in rows)})
    return tables


_HIGHLIGHTS = (
    (1, "营业收入", r"^(?:营业总收入|营业收入|total revenue|revenue)$"),
    (1, "毛利", r"^(?:毛利|gross profit|gross margin)$"),
    (1, "营业利润", r"^(?:营业利润|经营利润|operating income|operating profit)$"),
    (1, "净利润", r"^(?:净利润|net income)$"),
    (1, "稀释 EPS", r"^(?:稀释每股收益|稀释每股盈利|diluted earnings per share|diluted eps)$"),
    (3, "经营活动现金流", r"^(?:经营活动现金流量净额|经营活动现金流|operating cash flow)$"),
    (3, "资本开支", r"^(?:资本开支|购建固定资产支出|capital expenditure|capital expenditures)$"),
)


def highlight_rows(tables):
    result = []
    for statement_type, label, pattern in _HIGHLIGHTS:
        table = next((entry for entry in tables if entry["type"] == statement_type), None)
        if not table:
            continue
        matches = [row for row in table["rows"]
                   if row["name"] and re.fullmatch(pattern, row["name"].strip(), re.I)]
        if len(matches) == 1:
            result.append({"label": label, "provider_name": matches[0]["name"],
                           "cells": matches[0]["cells"], "reports": table["reports"]})
    return result


def breakdown_tables(breakdown):
    if not breakdown:
        return []
    currency = breakdown.get("currency") or ""
    groups = []
    for group in breakdown.get("groups") or []:
        rows = []
        for item in group.get("items") or []:
            amount = item.get("amount")
            rows.append({"name": item.get("name") or "未命名项目",
                         "amount": display_money(amount, currency),
                         "ratio": f"{_display_number(item['ratio'])}%"
                         if item.get("ratio") is not None else "—"})
        groups.append({"type": group.get("type") or "未标注维度", "rows": rows})
    return groups


def comparison_rows(snapshot, sec_periods, sec_rows):
    """Only align same currency and fiscal year; leave mismatches explicit."""
    if not snapshot or not sec_periods:
        return []
    by_code = {row["code"]: row for row in sec_rows}
    income = next((entry for entry in snapshot.data.get("statements", [])
                   if entry["type"] == 1), None)
    if not income:
        return []
    mapping = (("revenue", 5001), ("gross_profit", 5010),
               ("operating_income", 5034), ("net_income", 5051),
               ("diluted_eps", 5055))
    comparisons = []
    for report in income["reports"]:
        if report["currency"] != "USD":
            continue
        period = next((period for period in sec_periods
                       if period.isoformat() == report["period_end"]), None)
        if not period:
            continue
        index = sec_periods.index(period)
        for code, field_id in mapping:
            row = by_code.get(code)
            if not row:
                continue
            futu_item = next((item for item in report["items"]
                              if item["field_id"] == field_id), None)
            if not futu_item:
                continue
            sec_cell = row["cells"][index]
            futu_amount = Decimal(futu_item["amount"])
            if row["unit"] == "money":
                futu_amount /= Decimal("100000000")
            comparisons.append({"period": period, "label": row["label"],
                                "unit": row["unit"], "sec": sec_cell,
                                "futu": futu_amount, "futu_name": futu_item["name"]})
    return comparisons
