"""从已保存的 10-K iXBRL 原件核对少量现金流与租赁指标。只读、失败即隐藏金额。"""
import gzip
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

from .citations import MAX_QUOTE_CHARS, quote_digest
from .tenk_chapters import tenk_chapter_coverage


METRICS = (
    ("operating_cash", "经营现金流", "us-gaap:NetCashProvidedByUsedInOperatingActivities", "duration",
     r"(?:cash (?:generated|provided) by operating activities|cash from operations|net cash provided by operating activities)"),
    ("ppe_cash", "固定资产现金支出", "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment", "duration",
     r"(?:payments for acquisition of property|purchases of property and equipment|additions to property and equipment)"),
    ("depreciation", "固定资产折旧", "us-gaap:Depreciation", "duration",
     r"depreciation expense"),
    ("finance_rou_add", "融资租赁新增使用权资产（非现金）",
     "us-gaap:RightOfUseAssetObtainedInExchangeForFinanceLeaseLiability", "duration",
     r"(?:leased assets obtained in exchange for finance lease liabilities|^finance leases?\s*\||finance lease.*(?:right.of.use|obtained))"),
    ("finance_principal", "融资租赁本金现金支付", "us-gaap:FinanceLeasePrincipalPayments", "duration",
     r"(?:principal payments on finance leases|finance lease.*principal|financing cash flows from finance leases)"),
    ("finance_liability", "融资租赁负债（期末）", "us-gaap:FinanceLeaseLiability", "instant",
     r"(?:total finance lease liabilities|finance lease liabilities)"),
    ("finance_rou_asset", "融资租赁资产净额（期末）", "us-gaap:FinanceLeaseRightOfUseAsset", "instant",
     r"(?:total finance lease assets|finance lease.*(?:right.of.use|assets|property, plant and equipment))"),
)

# 公司专属披露不能互相改名或汇总；每项同时限制公司、分类维度和原文行。
COMPANY_METRICS = {
    "MSFT": (
        ("finance_rou_asset", "融资租赁资产净额（期末）", "us-gaap:PropertyPlantAndEquipmentNet",
         "instant", r"^Property and equipment, net\s*\|", "us-gaap:LeaseContractualTermAxis",
         "msft:FinanceLeaseMember"),
        ("company_revenue", "Microsoft Cloud 收入", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
         "duration", r"^Our Microsoft Cloud revenue,", "msft:ProductsOrServicesSecondaryCategorizationAxis",
         "msft:MicrosoftCloudMember"),
        ("uncommenced_lease", "尚未开始的租赁（未来承诺）",
         "us-gaap:UnrecordedUnconditionalPurchaseObligationBalanceSheetAmount", "instant",
         r"additional leases.*had not yet commenced", "us-gaap:LeaseContractualTermAxis",
         "msft:OperatingLeaseMember"),
    ),
    "AAPL": (
        ("finance_liability", "融资租赁负债（期末）", "us-gaap:FinanceLeaseLiability",
         "instant", r"^Total lease liabilities\s*\|", None, None),
        ("company_revenue", "Services 收入", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
         "duration", r"^Services\s*\|", "srt:ProductOrServiceAxis", "us-gaap:ServiceMember"),
        ("uncommenced_lease", "尚未开始的租赁（未来承诺）",
         "us-gaap:UnrecordedUnconditionalPurchaseObligationBalanceSheetAmount", "instant",
         r"fixed payment obligations under additional leases.*had not yet commenced",
         "us-gaap:UnrecordedUnconditionalPurchaseObligationByCategoryOfItemPurchasedAxis",
         "us-gaap:OperatingLeaseLeaseNotYetCommencedMember"),
    ),
    "TSLA": (
        ("company_revenue", "Energy generation and storage 收入",
         "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "duration",
         r"^Energy generation and storage\s*\|", "srt:ProductOrServiceAxis",
         "tsla:EnergyGenerationAndStorageMember"),
    ),
}
COMPANY_CIK = {"MSFT": 789019, "AAPL": 320193, "TSLA": 1318605}
HISTORICAL_LEASE_CODES = frozenset({
    "finance_rou_add", "finance_principal", "finance_liability", "finance_rou_asset",
    "uncommenced_lease",
})


def _document_cik(document):
    value = str((getattr(document, "metadata", None) or {}).get("cik", "")).strip()
    return int(value) if value.isdigit() else None


class _IXBRL(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.contexts = {}
        self.units = {}
        self.facts = []
        self.context = None
        self.unit = None
        self.fact = None
        self.field = None
        self.hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "ix:hidden":
            self.hidden_depth += 1
        if tag == "xbrli:context":
            self.context = {"id": attrs.get("id"), "members": [], "dimensioned": False}
        elif tag == "xbrli:unit":
            self.unit = {"id": attrs.get("id"), "measure": ""}
        elif self.context is not None and tag in {"xbrldi:explicitmember", "xbrldi:typedmember"}:
            self.context["dimensioned"] = True
            if tag == "xbrldi:explicitmember":
                self.field = tag
                self.context["axis"] = attrs.get("dimension")
        elif self.context is not None and tag in {"xbrli:startdate", "xbrli:enddate", "xbrli:instant"}:
            self.field = tag
        elif self.unit is not None and tag == "xbrli:measure":
            self.field = tag
        elif tag == "ix:nonfraction" and not self.hidden_depth:
            self.fact = {"attrs": attrs, "text": ""}

    def handle_data(self, data):
        if self.field == "xbrli:measure" and self.unit is not None:
            self.unit["measure"] += data
        elif self.field and self.context is not None:
            key = self.field.split(":", 1)[1]
            if key == "explicitmember":
                self.context["members"].append(data.strip())
            else:
                self.context[key] = data.strip()
        if self.fact is not None:
            self.fact["text"] += data

    def handle_endtag(self, tag):
        if tag == "ix:nonfraction" and self.fact is not None:
            self.facts.append(self.fact)
            self.fact = None
        elif tag == "xbrli:context" and self.context is not None:
            self.contexts[self.context["id"]] = self.context
            self.context = None
        elif tag == "xbrli:unit" and self.unit is not None:
            self.units[self.unit["id"]] = self.unit["measure"].strip()
            self.unit = None
        elif tag == self.field:
            self.field = None
        if tag == "ix:hidden" and self.hidden_depth:
            self.hidden_depth -= 1


def _amount(fact):
    attrs = fact["attrs"]
    value = fact["text"].strip().replace(",", "").replace("$", "")
    if attrs.get("format", "").lower().endswith("fixed-zero") and value in {"—", "–", "-", ""}:
        return Decimal(0)
    try:
        number = Decimal(value.strip("() ")) * (Decimal(10) ** int(attrs.get("scale", 0)))
    except (InvalidOperation, ValueError):
        return None
    return -number if attrs.get("sign") == "-" or value.startswith("(") else number


def _citation(text, item8, fact, label_pattern):
    display = fact["text"].strip()
    # 选同一 Item 8 完整行中的原始呈现值，避免不同表格中同一数字误命中。
    if not display or len(display) > 60:
        return None
    candidates = []
    for match in re.finditer(r"(?m)^[^\n]+$", text[item8["start"]:item8["end"]]):
        line = match.group()
        if (len(line) <= MAX_QUOTE_CHARS and
                re.search(label_pattern, line, re.I) and
                re.search(r"(?<![\d,.])" + re.escape(display) + r"(?![\d,.])", line)):
            start = item8["start"] + match.start()
            candidates.append((start, start + len(line), line))
    # 多处出现同样的数字时，不能猜测哪一处是该事实。
    if len(candidates) != 1:
        return None
    start, end, quote = candidates[0]
    return {"start": start, "end": end, "hash": quote_digest(quote), "quote": quote}


def _full_year_end_dates(parser, report_end):
    """只用整公司经营现金流确定本文件实际列示的最多三个完整财年。"""
    candidates = set()
    for fact in parser.facts:
        attrs = fact["attrs"]
        if attrs.get("name") != "us-gaap:NetCashProvidedByUsedInOperatingActivities":
            continue
        ctx = parser.contexts.get(attrs.get("contextref")) or {}
        if ctx.get("dimensioned") or parser.units.get(attrs.get("unitref"), "").upper() != "ISO4217:USD":
            continue
        try:
            start, end = date.fromisoformat(ctx["startdate"]), date.fromisoformat(ctx["enddate"])
        except (KeyError, ValueError):
            continue
        if 350 <= (end - start).days <= 380 and 0 <= (report_end - end).days <= 900:
            candidates.add(end)
    if report_end not in candidates:
        return [report_end]
    years = set()
    selected = []
    for end in sorted(candidates, reverse=True):
        if end.year not in years:
            selected.append(end)
            years.add(end.year)
        if len(selected) == 3:
            break
    return selected


def _metric_cell(parser, version, item8, spec, end):
    code, label, concept, kind, line_pattern, *dimension = spec
    axis, member = dimension if dimension else (None, None)
    matches = []
    for fact in parser.facts:
        attrs = fact["attrs"]
        ctx = parser.contexts.get(attrs.get("contextref")) or {}
        if attrs.get("name") != concept:
            continue
        if axis:
            if ctx.get("axis") != axis or ctx.get("members") != [member]:
                continue
        elif ctx.get("dimensioned"):
            continue
        if parser.units.get(attrs.get("unitref"), "").upper() != "ISO4217:USD":
            continue
        if kind == "duration":
            try:
                days = (end - date.fromisoformat(ctx.get("startdate", ""))).days
            except ValueError:
                continue
            if ctx.get("enddate") != end.isoformat() or not 350 <= days <= 380:
                continue
        elif ctx.get("instant") != end.isoformat():
            continue
        matches.append(fact)
    cell = {"status": "本文件未见匹配的 XBRL 事实"}
    identities = {(str(_amount(f)), f["text"].strip(), f["attrs"].get("contextref")) for f in matches}
    if matches and len(identities) == 1:
        fact = matches[0]
        amount = _amount(fact)
        citation = _citation(version.content_text, item8, fact, line_pattern)
        fact_id = fact["attrs"].get("id")
        if amount is not None and citation and fact_id and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", fact_id):
            cell.update(status="已核对", amount=amount / Decimal("100000000"),
                        citation=citation, fact_id=fact_id, fact_ids=[fact_id],
                        source_document_id=getattr(version.document, "pk", None),
                        source_version_id=getattr(version, "pk", None),
                        source_version_number=getattr(version, "version_number", None),
                        source_report_year=version.document.period_end.year,
                        source_url=getattr(version, "source_url", None))
        else:
            cell["status"] = "原文引用待核对"
    elif len(matches) > 1:
        cell["status"] = "同期间存在多个事实，待核对"
    return cell


def _microsoft_uncommenced_cell(parser, version, item8, spec, end):
    if end.year == 2024 and version.document.period_end.year == 2024:
        spec = (*spec[:4], r"additional operating and finance leases.*had not yet commenced", *spec[5:])
    operating = _metric_cell(parser, version, item8, spec, end)
    finance_spec = (*spec[:-1], "msft:FinanceLeaseMember")
    finance = _metric_cell(parser, version, item8, finance_spec, end)
    if end.year == 2024 and version.document.period_end.year == 2024:
        if ("amount" not in operating or "amount" not in finance or
                operating["citation"] != finance["citation"]):
            return {"status": "经营与融资租赁两项未全部核对"}
        operating["amount"] += finance["amount"]
        operating["fact_ids"] += finance["fact_ids"]
        operating["status"] = "经营与融资租赁两项相加"
    elif "amount" in operating and "amount" in finance:
        if operating["amount"] != finance["amount"]:
            return {"status": "同年两个租赁成员金额不同，待核对"}
        operating["status"] = "同额重复披露，仅计一次"
    return operating


def tenk_metric_grid(version, historical_documents=()):
    """以当前 10-K 为基准；仅缺失的租赁历史列回查同年已保存原件。"""
    period_end = version.document.period_end
    if not period_end:
        return [], [], "这份资料缺少报告期截止日，无法核对完整财年。"
    try:
        raw = gzip.decompress(version.raw_gzip)
    except (OSError, EOFError):
        return [], [], "保存的 SEC 原件无法读取，暂不展示指标。"
    parser = _IXBRL()
    parser.feed(raw.decode("utf-8", errors="replace"))
    parser.close()
    if not parser.facts:
        return [], [], "这份原件没有可核对的 iXBRL 数字。"
    item8 = next((x for x in tenk_chapter_coverage(version) if x["code"] == "8" and x["located"]), None)
    if item8 is None:
        return [], [], "未定位到 Item 8 财务报表，暂不展示指标。"
    periods = _full_year_end_dates(parser, period_end)
    symbol = getattr(getattr(version.document, "security", None), "symbol", "").upper()
    cik = _document_cik(version.document)
    company_specs = COMPANY_METRICS.get(symbol, ()) if COMPANY_CIK.get(symbol) == cik else ()
    overrides = {metric[0]: metric for metric in company_specs}
    specs = [overrides.pop(metric[0], (*metric, None, None)) for metric in METRICS]
    specs.extend(overrides.values())
    rows = []
    for spec in specs:
        selector = _microsoft_uncommenced_cell if symbol == "MSFT" and spec[0] == "uncommenced_lease" else _metric_cell
        rows.append({"code": spec[0], "label": spec[1],
                     "cells": [selector(parser, version, item8, spec, end) for end in periods]})
    for index, end in enumerate(periods[1:], start=1):
        candidates = [document for document in historical_documents
                      if (cik is not None and document.period_end == end and
                          document.document_type == "10-k" and
                          getattr(document, "source", "sec") == "sec" and
                          getattr(document, "security_id", None) == getattr(version.document, "security_id", None) and
                          getattr(document.security, "symbol", None) == symbol and
                          _document_cik(document) == cik)]
        if len(candidates) != 1:
            message = "该年 10-K 尚未归档" if not candidates else "该年有多份 10-K，待核对"
            for row in rows:
                if row["code"] in HISTORICAL_LEASE_CODES and "amount" not in row["cells"][index]:
                    row["cells"][index]["status"] = message
            continue
        historical_document = candidates[0]
        old_version = next(iter(historical_document.content_versions.all()), None)
        if old_version is None:
            for row in rows:
                if row["code"] in HISTORICAL_LEASE_CODES and "amount" not in row["cells"][index]:
                    row["cells"][index] = {"status": "该年 10-K 正文未保存",
                                           "pending_document_id": historical_document.pk}
            continue
        old_periods, old_rows, old_problem = tenk_metric_grid(old_version)
        old_by_code = {row["code"]: row for row in old_rows}
        old_index = old_periods.index(end) if end in old_periods else None
        for row in rows:
            if row["code"] not in HISTORICAL_LEASE_CODES:
                continue
            current = row["cells"][index]
            source = (old_by_code[row["code"]]["cells"][old_index]
                      if not old_problem and old_index is not None and row["code"] in old_by_code
                      else {"status": "原年报无法核对该指标"})
            if "amount" in current and "amount" in source and current["amount"] != source["amount"]:
                row["cells"][index] = {"status": "两份 10-K 同期金额不一致，待核对"}
            elif "amount" not in current:
                if "amount" in source:
                    row["cells"][index] = {**source, "status": "由该年原始 10-K 补齐"}
                else:
                    row["cells"][index] = {"status": source["status"],
                                           "pending_document_id": historical_document.pk}
    operating, capital = rows[:2]
    free_cash_cells = []
    for cash, spending in zip(operating["cells"], capital["cells"]):
        if "amount" in cash and "amount" in spending and spending["amount"] >= 0:
            free_cash_cells.append({"status": "由上两行相减", "derived": True,
                                    "amount": cash["amount"] - spending["amount"]})
        else:
            free_cash_cells.append({"status": "基础金额未全部核对"})
    rows.insert(2, {"code": "simple_fcf", "label": "简化自由现金流（经营现金流－固定资产现金支出）",
                    "cells": free_cash_cells})
    return periods, rows, None
