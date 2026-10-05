"""Versioned official schedules. A plan is never an observation's actual release date."""
import re
from datetime import date, datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from django.utils import timezone

from .adapters import SourceError, digest
from .calendar import schedule
from .fetch_worker import download
from .models import MacroCalendarSnapshot
from .registry import SERIES

CALENDARS = {
    "nbs": "https://www.stats.gov.cn/xxgk/sjfb/fbrcb/",
    "bea": "https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics",
    "census": "https://www.census.gov/economic-indicators/calendar-listview.html",
    "bls": "https://www.bls.gov/schedule/news_release/bls.ics",
}


def event(agency, title, when, codes, period):
    local = when.astimezone(ZoneInfo("Asia/Shanghai" if agency == "nbs" else "America/New_York"))
    return {"agency": agency, "country": "CN" if agency == "nbs" else "US", "title": title,
            "date": local.date().isoformat(), "time": local.strftime("%H:%M"), "timezone": str(local.tzinfo),
            "codes": codes, "period": period}


def parse_ics(text, agency):
    if "BEGIN:VCALENDAR" not in text or "END:VCALENDAR" not in text:
        raise SourceError("官方日历不是完整 ICS")
    # Some official feeds use blank lines between properties, including folded lines.
    lines = [line for line in text.replace("\r", "").split("\n") if line]
    unfolded = []
    for line in lines:
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    events = []
    for block in "\n".join(unfolded).split("BEGIN:VEVENT")[1:]:
        if "END:VEVENT" not in block:
            raise SourceError("日历事件不完整")
        fields = {key: value for key, value in (line.split(":", 1) for line in block.split("END:VEVENT")[0].splitlines() if ":" in line)}
        title = fields.get("SUMMARY", "").replace("\\,", ",").replace("\\n", " ")
        codes, label = [], title
        if agency == "bea":
            if title.startswith("Personal Income and Outlays"):
                codes, label = ["PCEPI", "PCEPILFE", "PCEC96", "DSPIC96", "PSAVERT"], "个人收入与支出（含 PCE）"
            elif title.startswith(("Gross Domestic Product", "GDP (")):
                codes, label = ["GDPC1", "GDPDEF"], "GDP · " + title
        elif agency == "bls":
            if title.startswith("Employment Situation"):
                codes, label = ["PAYEMS", "UNRATE", "CIVPART", "CES0500000003"], "就业报告"
            elif title.startswith("Consumer Price Index"):
                codes, label = ["CPIAUCSL", "CPILFESL"], "居民消费价格 CPI"
            elif title.startswith("Producer Price Index"):
                codes, label = ["PPIFIS", "PPIFID"], "生产者价格 PPI"
        if not codes:
            continue
        starts = [(key, value) for key, value in fields.items() if key.startswith("DTSTART")]
        if len(starts) != 1:
            raise SourceError("日历事件发布时间缺失")
        key, value = starts[0]
        try:
            when = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
            tz = "UTC" if value.endswith("Z") else re.search(r"TZID=([^;:]+)", key)[1]
            when = when.replace(tzinfo=ZoneInfo(tz))
        except (ValueError, TypeError, KeyError) as exc:
            raise SourceError("日历发布时间或时区格式改变") from exc
        events.append(event(agency, label, when, codes, title))
    return events


def parse_census(text):
    soup = BeautifulSoup(text, "html.parser")
    events = []
    definitions = {
        "New Residential Construction": (["HOUST", "PERMIT"], "新屋开工与营建许可"),
        "New Residential Sales": (["HSN1F"], "新建住宅销售"),
        "Advance Monthly Sales for Retail and Food Services": (["RSAFS"], "零售与餐饮销售"),
        "Advance Report on Durable Goods--Manufacturers' Shipments, Inventories, and Orders": (["DGORDER"], "耐用品订单"),
    }
    for row in soup.find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all("td")]
        if len(cells) != 6 or cells[0] not in definitions:
            continue
        codes, title = definitions[cells[0]]
        try:
            when = datetime.strptime(cells[4], "A%Y%m%d%H%M").replace(tzinfo=ZoneInfo("America/New_York"))
        except ValueError as exc:
            raise SourceError("Census 日历日期格式改变") from exc
        events.append(event("census", title, when, codes, cells[3]))
    return events


def parse_nbs(text):
    soup = BeautifulSoup(text, "html.parser")
    heading = soup.find("meta", attrs={"name": "ArticleTitle"})
    title = heading.get("content", "") if heading else soup.get_text(" ", strip=True)[:1500]
    year = re.search(r"(20\d{2})年.*(?:主要统计信息|数据).*发布", title)
    if not year:
        raise SourceError("统计局年度日历年份未核验")
    year = int(year[1])
    table = soup.find("table")
    if not table:
        raise SourceError("统计局日历表缺失")
    rows = table.find_all("tr")
    definitions = {
        1: ["INDUSTRY_YOY", "UNEMPLOYMENT", "FAI_CUM_YOY", "PROPERTY_INVESTMENT_CUM_YOY", "RETAIL_YOY"],
        4: ["PMI_MANUFACTURING", "PMI_NONMANUFACTURING"], 5: ["CPI_YOY", "CPI_MOM"], 6: ["PPI_YOY"],
        14: ["HOUSE_NEW_MOM", "HOUSE_NEW_YOY", "HOUSE_USED_MOM", "HOUSE_USED_YOY"],
        15: ["INDUSTRIAL_PROFIT_CUM", "INDUSTRIAL_PROFIT_CUM_YOY"],
    }
    events = []
    for index, row in enumerate(rows):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"])]
        if not cells or not cells[0].isdigit() or int(cells[0]) not in definitions:
            continue
        if len(cells) != 14 or index + 1 >= len(rows):
            raise SourceError("统计局年度日历列数改变")
        number = int(cells[0])
        clocks = [cell.get_text(" ", strip=True) for cell in rows[index + 1].find_all("td")]
        active_months = sum(bool(re.findall(r"(\d{1,2})/", cell)) for cell in cells[2:])
        if len(clocks) != active_months:
            raise SourceError("统计局发布时间行格式改变")
        clocks = iter(clocks)
        for month, cell in enumerate(cells[2:], 1):
            days = re.findall(r"(\d{1,2})/", cell)
            if not days:
                continue
            times = re.findall(r"\d{1,2}:\d{2}", next(clocks))
            if len(times) == 1:
                times *= len(days)
            if len(days) != len(times):
                raise SourceError("统计局发布日期与时间数量不一致")
            for day, clock in zip(days, times):
                codes = list(definitions[number])
                if number == 1 and month in {1, 4, 7, 10}:
                    codes += ["GDP_QUARTER", "GDP_REAL_YOY_INDEX"]
                when = datetime.combine(date(year, month, int(day)), datetime.strptime(clock, "%H:%M").time()).replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                # Mixed monthly/quarterly releases: do not infer an actual observation period.
                events.append(event("nbs", cells[1], when, codes, "统计期以实际发布稿为准"))
    return events


def refresh_calendars(*, write=False, reader=download):
    result = {"sources": [], "failures": []}
    for agency, url in CALENDARS.items():
        try:
            if agency == "nbs":
                html = reader(url)
                anchors = BeautifulSoup(html, "html.parser").find_all("a", href=True)
                candidates = [(a.get("title", "") or a.get_text(), urljoin(url, a["href"])) for a in anchors]
                candidates = [(title, link) for title, link in candidates if re.search(r"20\d{2}年.*主要统计信息.*发布", title)]
                url = max(candidates, key=lambda item: item[0])[1] if candidates else schedule()["sources"][0]["url"]
                if not url.startswith("https://www.stats.gov.cn/"):
                    raise SourceError("统计局日历地址异常")
            text = reader(url)
            events = parse_nbs(text) if agency == "nbs" else parse_census(text) if agency == "census" else parse_ics(text, agency)
            if not events:
                raise SourceError("日历为空或未匹配登记指标，保留上一版本")
            known = {(s.country, s.code) for s in SERIES}
            keys = [(e["date"], e["time"], e["title"], e["period"]) for e in events]
            if len(keys) != len(set(keys)) or any((e["country"], code) not in known for e in events for code in e["codes"]):
                raise SourceError("日历重复或包含未登记指标")
            payload = {"events": events, "years": sorted({int(e["date"][:4]) for e in events})}
            if write:
                obj, _ = MacroCalendarSnapshot.objects.get_or_create(agency=agency, content_hash=digest(text), defaults={
                    "source_url": url, "payload": payload, "checked_at": timezone.now()})
                obj.checked_at = timezone.now()
                obj.save(update_fields=["checked_at"])
            result["sources"].append({"agency": agency, "url": url, "events": len(events), "years": payload["years"], "sha256": digest(text)})
        except Exception as exc:
            error = str(exc) if isinstance(exc, SourceError) else f"官方日历请求或解析失败（{type(exc).__name__}），保留上一版本"
            result["failures"].append({"group": "calendar_" + agency, "error": error})
    return result
