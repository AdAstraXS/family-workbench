"""Bounded fixed-host public data reads; remote JavaScript is never executed."""
import json
import re
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from django.db import connection, transaction
from django.utils import timezone
from .fund_models import FundReference, FundNavRecord, FundDisclosureSnapshot

CHINA = ZoneInfo("Asia/Shanghai")
TARGETS = {"050025": ("513500", "https://pdf.dfcfw.com/pdf/H2_AN202307211592525255_1.pdf")}
PROFILE_KEYS = ["fd_full_name", "found_date", "type_desc", "keeper_name", "manager_name",
                "trup_name", "invest_target", "invest_orientation", "performance_bench_mark"]


class FundDataError(ValueError):
    pass


def fetch_text(url):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://fund.eastmoney.com/"})
    with urlopen(request, timeout=15) as response:
        raw = response.read(8_000_001)
        if len(raw) > 8_000_000:
            raise FundDataError("来源响应超过大小限制")
        return raw.decode(response.headers.get_content_charset() or "utf-8", errors="strict")


def js_literal(text, name):
    match = re.search(r"(?:var\s+)?" + re.escape(name) + r"\s*=\s*", text)
    if not match:
        raise FundDataError("缺少预期数据字段")
    value, end = json.JSONDecoder(parse_float=Decimal).raw_decode(text[match.end():])
    if not text[match.end() + end:].lstrip().startswith(";"):
        raise FundDataError("来源字段不是JSON常量")
    return value


def positive_nav(value):
    number = Decimal(str(value))
    if not number.is_finite() or not Decimal("0") < number < Decimal("1000000000000"):
        raise FundDataError("净值无效")
    return number


def parse_nav(text):
    name = js_literal(text, "fS_name")
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise FundDataError("基金名称无效")
    data = js_literal(text, "Data_netWorthTrend")
    if not isinstance(data, list) or not data or len(data) > 20000:
        raise FundDataError("净值历史为空或格式变化")
    rows = {}
    for item in data:
        day = datetime.fromtimestamp(int(item["x"]) / 1000, CHINA).date()
        if not date(1990, 1, 1) <= day <= timezone.localdate():
            raise FundDataError("净值日期无效")
        rows[day] = positive_nav(item["y"])
    return name, sorted(rows.items())


def parse_allocations(data):
    items = data.get("data", {}).get("chart_list", [])
    if not items:
        raise FundDataError("资产分布暂未披露")
    result = []
    for row in items:
        value = Decimal(str(row["percent"]))
        if not value.is_finite() or abs(value) > 10000:
            raise FundDataError("资产比例无效")
        result.append({"name": str(row["type_desc"])[:100], "percentage": str(value)})
    return result


def parse_holdings(text):
    match = re.search(r'content:\s*"(.*?)",\s*arryear', text, re.S)
    if not match:
        raise FundDataError("持仓来源格式变化")
    soup = BeautifulSoup(match.group(1).replace(r'\"', '"'), "html.parser")
    snapshots = []
    for box in soup.select(".box"):
        title, table = box.select_one("h4"), box.select_one("table")
        if not title or not table:
            continue
        day_match = re.search(r"\d{4}-\d{2}-\d{2}", title.get_text())
        if not day_match:
            continue
        day = date.fromisoformat(day_match.group())
        if day > timezone.localdate():
            raise FundDataError("披露日期无效")
        headers = [re.sub(r"\s+", "", cell.get_text()) for cell in table.select("thead th")]
        if not headers:
            headers = [re.sub(r"\s+", "", cell.get_text()) for cell in table.select("tr")[0].select("th")]
        required = ["股票代码", "股票名称", "占净值比例"]
        if not all(key in headers for key in required):
            raise FundDataError("持仓列发生变化")
        positions = []
        for row in table.select("tbody tr")[:10]:
            cells = [cell.get_text(" ", strip=True) for cell in row.select("td")]
            if len(cells) != len(headers):
                raise FundDataError("持仓列数变化")
            values = dict(zip(headers, cells))
            percentage = Decimal(values["占净值比例"].replace("%", "").replace(",", ""))
            if not percentage.is_finite() or not 0 <= percentage <= 100:
                raise FundDataError("持仓比例无效")
            positions.append({"code": values["股票代码"][:30], "name": values["股票名称"][:200],
                              "percentage": str(percentage)})
        snapshots.append((day, positions))
    return snapshots  # No direct stocks is legitimate for an ETF feeder.


def fallback_profile(code):
    url = f"https://fundf10.eastmoney.com/jbgk_{code}.html"
    soup = BeautifulSoup(fetch_text(url), "html.parser")
    fields = {}
    for row in soup.select("table.info tr"):
        cells = row.select("th,td")
        for index in range(0, len(cells) - 1, 2):
            fields[cells[index].get_text(strip=True)] = cells[index + 1].get_text(" ", strip=True)
    if not fields.get("基金代码", "").startswith(code):
        raise FundDataError("备用资料代码不匹配")
    mapping = {"基金全称": "fd_full_name", "基金类型": "type_desc", "基金管理人": "keeper_name",
               "基金托管人": "trup_name", "基金经理人": "manager_name", "业绩比较基准": "performance_bench_mark"}
    profile = {key: "" for key in PROFILE_KEYS}
    for label, key in mapping.items():
        profile[key] = fields.get(label, "")
    profile["found_date"] = fields.get("成立日期/规模", "").split("/")[0].strip()
    for heading in soup.select("h4"):
        key = {"投资目标": "invest_target", "投资策略": "invest_orientation"}.get(heading.get_text(strip=True))
        if key:
            content = heading.parent.select_one(".txt_in") or heading.parent.select_one("p")
            if content:
                profile[key] = content.get_text(" ", strip=True)
    return profile, url


def refresh_fund(fund_id):
    # Cross-worker and daily-command exclusion for each public fund.
    lock = False
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [1464222283, int(fund_id)])
            lock = cursor.fetchone()[0]
        if not lock:
            return {"fund_id": fund_id, "status": "busy", "message": "该基金正在更新"}
    try:
        return _refresh_fund(fund_id)
    finally:
        if lock:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [1464222283, int(fund_id)])


def _refresh_fund(fund_id):
    fund = FundReference.objects.get(pk=fund_id)
    code = fund.code
    if not re.fullmatch(r"[0-9]{6}", code):
        raise FundDataError("基金代码无效")
    now, errors, successes = timezone.now(), {}, []
    nav_url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
    report_date = None
    try:
        text = fetch_text(nav_url)
        if js_literal(text, "fS_code") != code:
            raise FundDataError("净值来源基金代码不匹配")
        name, records = parse_nav(text)
        with transaction.atomic():
            locked = FundReference.objects.select_for_update().get(pk=fund.pk)
            FundNavRecord.objects.bulk_create([
                FundNavRecord(fund=fund, nav_date=day, unit_nav=value, source_url=nav_url, fetched_at=now)
                for day, value in records], update_conflicts=True, update_fields=["unit_nav", "source_url", "fetched_at"],
                unique_fields=["fund", "nav_date"])
            if locked.nav_date is None or records[-1][0] >= locked.nav_date:
                locked.name, locked.nav_date, locked.latest_nav = name, records[-1][0], records[-1][1]
                locked.nav_source, locked.nav_fetched_at = nav_url, now
                locked.save(update_fields=["name", "nav_date", "latest_nav", "nav_source", "nav_fetched_at", "updated_at"])
        successes.append("净值")
        try:
            allocation = js_literal(text, "Data_assetAllocation")
            dates = [date.fromisoformat(value) for value in allocation.get("categories", [])]
            report_date = max(day for day in dates if day <= timezone.localdate())
        except (ValueError, TypeError, KeyError):
            errors["资产分布"] = "来源未提供有效报告日期"
    except Exception:
        errors["净值"] = "抓取失败或来源格式变化，保留上次成功数据"
    profile_url = f"https://danjuanfunds.com/djapi/fund/{code}"
    try:
        data = json.loads(fetch_text(profile_url), parse_float=Decimal).get("data")
        if not isinstance(data, dict) or str(data.get("fd_code")) != code:
            raise FundDataError("资料的基金代码不匹配")
        profile = {key: str(data.get(key) or "") for key in PROFILE_KEYS}
        FundReference.objects.filter(pk=fund.pk).update(profile=profile, profile_source=profile_url,
                                                       profile_fetched_at=now)
        successes.append("资料")
    except Exception:
        try:
            profile, source = fallback_profile(code)
            FundReference.objects.filter(pk=fund.pk).update(profile=profile, profile_source=source,
                                                           profile_fetched_at=now)
            successes.append("资料")
        except Exception:
            errors["资料"] = "资料暂不可用，保留上次成功数据"
    if report_date:
        asset_url = "https://danjuanfunds.com/djapi/fundx/base/fund/record/asset/percent?" + urlencode(
            {"fund_code": code, "report_date": report_date.isoformat()})
        try:
            allocations = parse_allocations(json.loads(fetch_text(asset_url), parse_float=Decimal))
            FundDisclosureSnapshot.objects.update_or_create(fund=fund, report_date=report_date,
                defaults={"allocations": allocations, "allocation_source": asset_url, "allocation_fetched_at": now})
            successes.append("资产分布")
        except Exception:
            errors["资产分布"] = "抓取失败或暂无披露，保留上次成功数据"
    # Query this and last calendar year so January still shows the latest disclosure.
    for year in [timezone.localdate().year, timezone.localdate().year - 1]:
        url = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx?" + urlencode(
            {"type": "jjcc", "code": code, "topline": 10, "year": year, "month": ""})
        try:
            holdings = parse_holdings(fetch_text(url))
            for day, rows in holdings:
                FundDisclosureSnapshot.objects.update_or_create(fund=fund, report_date=day,
                    defaults={"holdings": rows, "holdings_source": url, "holdings_fetched_at": now})
            if holdings:
                successes.append("持仓")
                break
        except Exception:
            errors["持仓"] = "抓取失败或来源格式变化，保留上次成功数据"
            break
    if code in TARGETS:
        target_code, source = TARGETS[code]
        target, _ = FundReference.objects.get_or_create(code=target_code)
        FundReference.objects.filter(pk=fund.pk).update(target_fund=target, target_source=source)
    FundReference.objects.filter(pk=fund.pk).update(last_attempt_at=now, refresh_errors=errors)
    return {"fund_id": fund.pk, "code": code, "status": "partial" if errors and successes else ("failed" if errors else "saved"),
            "message": "；".join([f"已更新：{'、'.join(dict.fromkeys(successes))}"] if successes else ["未取得新数据"]),
            "errors": errors}
