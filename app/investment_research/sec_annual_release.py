"""Conservative annual earnings bridge, derived only from archived source tables.

An untagged table must have explicit dates, annual column scope, units and at
least two exact comparative matches to filed facts establishing currency and
accounting basis. Ambiguous documents stay readable without invented numbers.
"""
import gzip
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from bs4 import BeautifulSoup, NavigableString
from .material_reading import CORE_FACTS, fact_rows

LABELS = {
    "revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet", "Revenue"),
    "revenues": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenue"),
    "net sales": ("SalesRevenueNet", "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "Revenue"),
    "gross profit": ("GrossProfit",), "gross margin": ("GrossProfit",),
    "operating income": ("OperatingIncomeLoss", "ProfitLossFromOperatingActivities"),
    "operating income (loss)": ("OperatingIncomeLoss", "ProfitLossFromOperatingActivities"),
    "net income": ("NetIncomeLoss", "ProfitLoss"), "net income (loss)": ("NetIncomeLoss", "ProfitLoss"),
    "total assets": ("Assets",), "total liabilities": ("Liabilities",),
    "total equity": ("StockholdersEquity", "Equity"),
    "total stockholders’ equity": ("StockholdersEquity", "Equity"),
    "total stockholders' equity": ("StockholdersEquity", "Equity"),
    "net cash provided by operating activities": ("NetCashProvidedByUsedInOperatingActivities", "CashFlowsFromUsedInOperatingActivities"),
    "net cash provided by (used in) operating activities": ("NetCashProvidedByUsedInOperatingActivities", "CashFlowsFromUsedInOperatingActivities"),
}
STATEMENT = re.compile(r"(?:consolidated\s+)?(?:statements? of (?:operations|income|earnings|cash flows)|balance sheets)", re.I)


def _date(text):
    text = re.sub(r"\s+", " ", text).strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _amount(text):
    text = re.sub(r"\s+", "", text).replace(",", "").replace("$", "")
    if text in {"—", "–", "-", "", "N/A"}:
        return None
    if not re.fullmatch(r"-?\d+(?:\.\d+)?|\(\d+(?:\.\d+)?\)", text):
        return None
    return -Decimal(text[1:-1]) if text.startswith("(") else Decimal(text)


def _grid(table):
    rows = []
    for tr in table.find_all("tr"):
        if tr.find_parent("table") != table:
            continue
        cells, col = [], 0
        for td in tr.find_all(["td", "th"], recursive=False):
            # Rowspans require a different alignment strategy; do not guess.
            try:
                rowspan, span = int(td.get("rowspan", 1)), int(td.get("colspan", 1))
            except (ValueError, TypeError):
                return []
            if rowspan != 1 or not 1 <= span <= 256:
                return []
            cells.append((col, col + span, re.sub(r"\s+", " ", td.get_text(" ", strip=True))))
            col += span
        rows.append(cells)
    return rows


def _context(table):
    parts = []
    for element in table.previous_elements:
        if isinstance(element, NavigableString) and element.strip():
            parts.append(str(element).strip())
        if sum(map(len, parts)) > 1000:
            break
    before = " ".join(reversed(parts))
    matches = list(STATEMENT.finditer(before))
    return before[matches[-1].start():] if matches else ""


def _start(end, data):
    candidates = set()
    for taxonomy in data.get("facts", {}).values():
        for fact in taxonomy.values():
            for values in fact.get("units", {}).values():
                for value in values:
                    start = value.get("start")
                    if value.get("form") not in {"10-Q", "10-Q/A"} or not start:
                        continue
                    try:
                        days = (date.fromisoformat(end) - date.fromisoformat(start)).days
                    except ValueError:
                        continue
                    if 330 <= days <= 380 and value.get("end", "") <= end:
                        candidates.add(start)
    if len(candidates) == 1:
        return candidates.pop(), "起始日来自同期季报，截止日来自全年公告"
    previous = sorted({r["end"] for r in fact_rows(data) if r["start"] and r["end"] < end}, reverse=True)
    if previous:
        start = (date.fromisoformat(previous[0]) + timedelta(days=1)).isoformat()
        if 330 <= (date.fromisoformat(end) - date.fromisoformat(start)).days <= 380:
            return start, "起始日按上个已披露财年结束次日推算，待正式年报核对"
    return "", "财年起始日尚未确认"


def release_rows(version, data):
    """Extract only the latest annual period; official facts remain authoritative."""
    result = {"rows": [], "warnings": [], "periods": []}
    if version.media_type != "text/html":
        result["warnings"].append("公告不是可解析的 HTML 表格，请阅读原文核对全年数据。")
        return result
    try:
        raw = gzip.decompress(bytes(version.raw_gzip))
        soup = BeautifulSoup(raw, "html.parser")
    except (OSError, EOFError, ValueError):
        result["warnings"].append("公告表格尚未整理成功，请核对原文。")
        return result
    filed = fact_rows(data)
    latest_end = max((r["end"] for r in filed if r["start"]), default="")
    for table in soup.find_all("table"):
        context = _context(table)
        if not context or re.search(r"non.gaap|adjusted|reconciliation|outlook|guidance", context, re.I):
            continue
        units = re.search(r"in\s+(millions|thousands|billions)\b", context, re.I)
        if not units:
            continue
        scale = {"millions": Decimal(10)**6, "thousands": Decimal(10)**3, "billions": Decimal(10)**9}[units[1].lower()]
        rows = _grid(table)
        dates, annual_ranges, date_index = [], [], -1
        balance = bool(re.search(r"balance sheets", context, re.I))
        for i, row in enumerate(rows[:12]):
            for a, b, value in row:
                if re.fullmatch(r"(?:for the )?(?:fiscal )?years? ended", value, re.I):
                    annual_ranges.append((a, b))
            found = [(a, b, _date(value)) for a, b, value in row if _date(value)]
            if found:
                dates, date_index = found, i
                break
        if len(dates) < 2:
            continue
        if any(re.search(r"non.gaap|adjusted|outlook|guidance", v, re.I) for row in rows[:date_index + 1] for _, _, v in row):
            continue
        whole_annual = bool(annual_ranges and annual_ranges[-1][1] <= dates[0][0])
        annual_dates = dates if balance or whole_annual else [d for d in dates if any(a <= d[0] and d[1] <= b for a, b in annual_ranges)]
        if len(annual_dates) < 2:
            continue
        end = max(d[2] for d in annual_dates)
        if end <= latest_end:
            continue
        candidates, eps_section = [], False
        for row in rows[date_index + 1:]:
            nonempty = [(a, b, v) for a, b, v in row if v]
            if not nonempty:
                continue
            label = nonempty[0][2].strip().lower().rstrip(":")
            if "earnings per share" in label:
                eps_section = True
            elif "number of shares" in label or "shares used" in label:
                eps_section = False
            per_share = label in {"diluted earnings per share", "earnings per share - diluted", "earnings per share (diluted)"} or (eps_section and label == "diluted")
            codes = ("EarningsPerShareDiluted", "DilutedEarningsLossPerShare") if per_share else LABELS.get(label)
            if not codes:
                continue
            values = {}
            for a, b, period in annual_dates:
                pieces = [v for x, y, v in row if a <= x and y <= b and v and v not in {"$"}]
                value = _amount("".join(pieces))
                if value is not None:
                    values[period] = value if per_share else value * scale
            if end in values:
                candidates.append((nonempty[0][2], codes, values))
        # Exact comparative matches establish currency, standard AND concept.
        matches = []
        for label, codes, values in candidates:
            confirmed = {(r["currency"], r["standard"], r["code"]) for r in filed
                         if r["code"] in codes and r["end"] in values and r["end"] != end
                         and r["value"] == values[r["end"]] and bool(r["start"]) != balance}
            if len(confirmed) == 1:
                matches.append((label, values, next(iter(confirmed))))
        bases = {(unit.split("/")[0], standard) for _, _, (unit, standard, _) in matches}
        if len(matches) < 2 or len(bases) != 1:
            continue
        start, basis = _start(end, data)
        if not start:
            continue
        result["periods"].append({"start": start, "end": end, "basis": basis})
        for label, values, (unit, standard, code) in matches:
            result["rows"].append({"label": CORE_FACTS[code], "source_label": label, "code": code,
                "start": "" if balance else start, "end": end, "value": values[end], "currency": unit,
                "standard": standard, "period": end if balance else f"{start} — {end}",
                "filed": version.data.get("filing_date", ""), "accession": version.data.get("accession", ""),
                "source_kind": "release", "version_id": version.pk, "source_url": version.source_url,
                "audit": "未经审计（原文标注）" if re.search(r"\bunaudited\b", context, re.I) else "审计状态见原文",
                "period_basis": basis})
    # Two independently parsed tables must agree; conflicting values are omitted.
    annual_ends = {r["end"] for r in result["rows"] if r["start"]}
    # A balance sheet alone does not prove that a quarterly release is annual.
    result["rows"] = [r for r in result["rows"] if r["end"] in annual_ends]
    grouped = {}
    for row in result["rows"]:
        key = (row["code"], row["currency"], row["standard"], row["start"], row["end"])
        grouped.setdefault(key, []).append(row)
    result["rows"] = [rows[0] for rows in grouped.values() if len({r["value"] for r in rows}) == 1]
    if not result["rows"]:
        result["warnings"].append("这份公告未取得可核对的新增全年指标；日期、币种、口径或表格对应关系需人工核对，原文仍可阅读。")
    return result


def annual_reading(data, overview):
    official = fact_rows(data)
    extra, notices = [], []
    for report in overview["releases"]:
        if report["period"] <= max((r["end"] for r in official if r["start"]), default=""):
            continue
        parsed = release_rows(report["version"], data)
        extra.extend(parsed["rows"])
        if parsed["rows"] or report["title"] == "全年业绩公告":
            notices.append({"report": report, "warnings": parsed["warnings"], "count": len(parsed["rows"])})
    # Official filings win for a matching metric/period. Newer releases win otherwise.
    merged = {}
    for row in sorted(extra, key=lambda r: r["filed"] or "") + official:
        merged[(row["code"], row["currency"], row["standard"], row["start"], row["end"])] = row
    rows = list(merged.values())
    periods = sorted([r for r in rows if r["start"]], key=lambda r: (r["end"], r["start"]), reverse=True)
    latest = periods[0] if periods else None
    fiscal = {"start": latest["start"], "end": latest["end"],
              "basis": latest.get("period_basis", "起止日期来自正式年报"),
              "days": (date.fromisoformat(latest["end"]) - date.fromisoformat(latest["start"])).days + 1} if latest else None
    return rows, fiscal, notices


def fiscal_calendar(data, overview):
    rows = sorted([r for r in fact_rows(data) if r["start"]], key=lambda r: r["end"], reverse=True)
    latest = rows[0] if rows else None
    end = latest["end"] if latest else ""
    start = latest["start"] if latest else ""
    basis = "起止日期来自正式年报"
    for report in overview["releases"]:
        candidate = report["period"]
        if report["title"] == "全年业绩公告" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate) and candidate > end:
            end = candidate
            start, basis = _start(end, data)
    if not end:
        return None
    return {"start": start, "end": end, "basis": basis,
            "days": (date.fromisoformat(end) - date.fromisoformat(start)).days + 1 if start else None}
