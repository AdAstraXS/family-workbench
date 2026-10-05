"""Bounded discovery on official catalogues; links and redirects stay on one host."""
import json
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from .adapters import SourceError
from .fetch_worker import download, validate_url

CATALOGUES = {
    "pbc": "https://www.pbc.gov.cn/diaochatongjisi/116219/116225/index.html",
    "mofcom": "https://www.mofcom.gov.cn/xwfb/rcxwfb/index.html",
    "nbs_release": "https://www.stats.gov.cn/sj/zxfb/",
}
PATTERNS = {
    "pbc": r"^20\d{2}年(?:(?:\d{1,2})月|一季度|上半年|前三季度|全年)?(?:金融统计数据报告|社会融资规模(?:存量|增量)统计数据报告)$",
    "mofcom": r"20\d{2}年.*全国吸收外资",
    "nbs_release": r"^20\d{2}年(?:1[—–－至-]\d{1,2}月份?|上半年|前三季度|全年)?(?:全国)?固定资产投资",
}


@dataclass(frozen=True)
class ReportLink:
    group: str
    title: str
    url: str
    period: date


def title_period(title):
    year = re.search(r"(20\d{2})年", title)
    if not year:
        raise SourceError("报告标题年份缺失")
    month = re.search(r"年(?:1[—–－至-])?(\d{1,2})月份?", title)
    return date(int(year[1]), int(month[1]) if month else
                3 if "一季度" in title else 6 if "上半年" in title else 9 if "前三季度" in title else 12, 1)


def links(html, group, base):
    found = {}
    for anchor in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        title = re.sub(r"\s+", "", anchor.get("title") or anchor.get_text())
        if not re.search(PATTERNS[group], title):
            continue
        url = urljoin(base, anchor["href"])
        try:
            validate_url(url, group)
            item = ReportLink(group, title, url, title_period(title))
        except (ValueError, TypeError) as exc:
            raise SourceError("官方目录包含无效报告地址或统计期") from exc
        found[url] = item
    return list(found.values())


def discover(group, start, end, *, pages=5, reader=download):
    if group not in CATALOGUES or not 1 <= pages <= 80:
        raise SourceError("未登记的目录或分页范围")
    base = CATALOGUES[group]
    result, seen = {}, set()
    if group == "mofcom":
        html = reader(base)
        script = BeautifulSoup(html, "html.parser").find("script", querydata=True)
        if not script or script.get("url") != "/api-gateway/jpaas-publish-server/front/page/build/unit":
            raise SourceError("商务部公开目录接口发生变化")
        try:
            query = json.loads(script["querydata"].replace("'", '"'))
        except (ValueError, KeyError) as exc:
            raise SourceError("商务部目录参数无法解析") from exc
    for page in range(pages):
        if group == "pbc":
            url = base if page == 0 else urljoin(base, f"11871-{page + 1}.html")
        elif group == "nbs_release":
            url = base if page == 0 else urljoin(base, f"index_{page}.html")
        else:
            query["paramJson"] = json.dumps({"pageNo": page + 1, "pageSize": 15}, separators=(",", ":"))
            url = urljoin(base, script["url"]) + "?" + urlencode(query)
        validate_url(url, group)
        raw = reader(url)
        if group == "mofcom":
            try:
                html = json.loads(raw)["data"]["html"]
            except (ValueError, KeyError, TypeError) as exc:
                raise SourceError("商务部目录返回格式变化") from exc
        else:
            html = raw
        # Detect servers returning page one for every pagination request.
        signature = re.sub(r"\s+", "", BeautifulSoup(html, "html.parser").get_text())
        if signature in seen:
            raise SourceError("官方目录分页重复，无法确认完整覆盖")
        seen.add(signature)
        items = links(html, group, base)
        for item in items:
            if start <= item.period <= end:
                result[item.url] = item
        if items and max(item.period for item in items) < start:
            break
    if not result:
        raise SourceError("目录中未发现指定范围报告；不能视为已更新")
    return sorted(result.values(), key=lambda item: (item.period, item.url))
