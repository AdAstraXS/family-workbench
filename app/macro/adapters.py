"""Pure parsers: unexpected schema/periods fail closed before any database write."""
import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser


class SourceError(ValueError):
    pass


@dataclass(frozen=True)
class Point:
    code: str
    period: date
    value: Decimal | None
    geography: str = "全国"
    release_date: date | None = None
    evidence: dict = field(default_factory=dict)


def number(value):
    if value is None or str(value).strip() in {"", ".", "--", "nan", "NaN", "None", "null"}:
        return None
    try:
        result = Decimal(str(value).strip().replace(",", "").removesuffix("%"))
    except InvalidOperation as exc:
        raise SourceError("数值格式改变，已停止导入") from exc
    if not result.is_finite() or abs(result) >= Decimal("1e16"):
        raise SourceError("数值超出可存储范围")
    rounded = result.quantize(Decimal("0.00000001"))
    if result != rounded:
        raise SourceError("来源精度超过八位小数，请先核验")
    return rounded


def period_date(value):
    value = str(value).strip()
    q = re.fullmatch(r"(\d{4})年?第?([一二三四1-4])季度", value)
    if q:
        quarter = {"一": 1, "二": 2, "三": 3, "四": 4}.get(q[2], int(q[2]) if q[2].isdigit() else 0)
        return date(int(q[1]), quarter * 3 - 2, 1)
    for pattern in [r"(\d{4})年(\d{1,2})月份?", r"(\d{4})(\d{2})", r"(\d{4})-(\d{2})"]:
        m = re.fullmatch(pattern, value)
        if m:
            return date(int(m[1]), int(m[2]), 1)
    if re.fullmatch(r"\d{4}年?", value):
        return date(int(value[:4]), 1, 1)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SourceError("统计期格式改变，已停止导入") from exc


def digest(payload):
    if not isinstance(payload, bytes):
        payload = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def parse_fred(text, series):
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if reader.fieldnames != ["observation_date", series.field]:
        raise SourceError("FRED CSV 列与登记序列不符")
    return [Point(series.code, period_date(row["observation_date"]), number(row[series.field]),
                  evidence=row) for row in reader]


def parse_frame(rows, series):
    points = []
    for spec in series:
        if spec.provider == "nbs":
            selected = [row for row in rows if row.get("index") == spec.field]
            if len(selected) != 1:
                raise SourceError(f"统计局字段缺失或重复：{spec.code}")
            for label, value in selected[0].items():
                if label != "index":
                    period = period_date(label)
                    if spec.selector.startswith("from:") and period < date.fromisoformat(spec.selector[5:]):
                        continue
                    points.append(Point(spec.code, period, number(value), evidence={"field": spec.field, "period": label, "value": value}))
        elif spec.provider == "nbs_city":
            cities = set()
            for row in rows:
                city = row.get("index")
                if not isinstance(city, str) or not city or city in cities:
                    raise SourceError("统计局城市标识缺失或重复")
                cities.add(city)
                values = [(key, number(value)) for key, value in row.items() if key != "index"]
                # Catalog includes Lhasa but the official 70-city release does not.
                if city in {"拉萨", "拉萨市"} and all(value is None for _, value in values):
                    continue
                for label, value in values:
                    points.append(Point(spec.code, period_date(label), value, city,
                                        evidence={"field": spec.field, "period": label, "value": row[label]}))
            if len({p.geography for p in points if p.code == spec.code}) != 70:
                raise SourceError("住宅价格城市集合不等于70，需核验来源")
        else:
            selected = rows
            if spec.selector:
                selected = [row for row in rows if str(row.get("item", "")).strip() == spec.selector]
            if not selected:
                raise SourceError(f"来源未包含统计对象：{spec.code}")
            for row in selected:
                if spec.field not in row or ("月份" not in row and "date" not in row):
                    raise SourceError(f"来源字段改变：{spec.code}")
                value = number(row[spec.field])
                if value is not None and spec.transform == "thousand_usd_to_100m":
                    value = number(value / Decimal("100000"))
                points.append(Point(spec.code, period_date(row.get("月份", row.get("date"))), value, evidence=row))
    return points


class PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0
        self.publication = {}

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip += 1
        if tag == "meta":
            fields = dict(attrs)
            name = (fields.get("name") or "").lower()
            if name in {"pubdate", "publishdate", "firstpublishedtime"}:
                self.publication[name] = fields.get("content", "")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def parse_official(html, group):
    parser = PageText()
    parser.feed(html)
    text = re.sub(r"\s+", "", " ".join(parser.parts)).replace("％", "%")
    # Read publication metadata separately. Never use the fetch date as release date.
    metadata = next((parser.publication[key] for key in ["pubdate", "publishdate", "firstpublishedtime"] if parser.publication.get(key)), "")
    published = re.match(r"(\d{4}[-/]\d{2}[-/]\d{2})", metadata)
    if not published:
        published = re.search(r"(?:来源：.{0,40}?)?(\d{4}[-/]\d{2}[-/]\d{2})\s*\d{2}:\d{2}", " ".join(parser.parts))
    try:
        release = date.fromisoformat(published[1].replace("/", "-")) if published else None
    except ValueError as exc:
        raise SourceError("官方发布日期格式无效") from exc
    points = []

    def extract(code, pattern, *, monetary=False, signed=False, required=True):
        match = re.search(pattern, text)
        if not match:
            if required:
                raise SourceError(f"官方发布字段未匹配：{code}")
            return
        value = number(match["value"])
        if monetary and match["unit"] == "万亿元":
            value *= 10000
        if signed and match["direction"] in {"减少", "下降"}:
            value = -value
        points.append(Point(code, period, value, release_date=release, evidence={"excerpt": match[0], "source_notes": notes}))

    if group in {"ism_manufacturing", "ism_services"}:
        sector = "Manufacturing" if group == "ism_manufacturing" else "Services"
        months = {name: n for n, name in enumerate(["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"], 1)}
        title = re.search(r"(" + "|".join(months) + r")(20\d{2})ISM[^a-zA-Z]{0,4}" + sector + r"PMI", text, re.I)
        if not title:
            raise SourceError("ISM报告统计月份或部门未核验")
        period = date(int(title[2]), months[title[1].title()], 1)
        code = "PMI_ISM_MANUFACTURING" if sector == "Manufacturing" else "PMI_ISM_SERVICES"
        value = re.search(sector + r"PMI[^a-zA-Z\d]{0,8}at(?P<value>\d+(?:\.\d+)?)%", text, re.I)
        if not value:
            raise SourceError("ISM报告总指数未匹配")
        return [Point(code, period, number(value["value"]), release_date=release,
                      evidence={"excerpt": value[0], "source_notes": "官方报告当月总指数；不将发布日期当成统计月份，不以商业接口的无统计期数据回填历史。"})]
    if group == "gov_budget":
        if not release or f"{release.year}年政府工作任务" not in text or "政府工作报告" not in text:
            raise SourceError("年度预算报告年份或发布日期未核验")
        period = date(release.year, 1, 1)
        notes = "官方年度预算安排约数；不等于月度财政收支缺口或实际执行赤字率。"
        extract("DEFICIT_BUDGET_RATIO", r"今年赤字率拟按(?P<value>\d+(?:\.\d+)?)%左右安排")
    elif group == "mofcom":
        m = re.search(r"(\d{4})年1[-—–－至](\d{1,2})月.{0,25}(?:全国吸收外资|全国新设立外商投资企业)", text)
        if not m:
            m = re.search(r"(\d{4})年(1)月.{0,25}(?:全国吸收外资|全国新设立外商投资企业)", text)
        if not m:
            annual = re.search(r"(\d{4})年(?:全年)?全国吸收外资", text)
            m = (annual[0], annual[1], "12") if annual else None
        if not m:
            raise SourceError("商务部累计统计期未匹配")
        period = date(int(m[1]), int(m[2]), 1)
        notes = "人民币累计口径；高技术与行业分类交叉，不可加总。"
        extract("FDI_CUM", r"实际使用外资金额(?P<value>[\d.]+)亿元人民币")
        extract("FDI_CUM_YOY", r"实际使用外资金额[\d.]+亿元人民币[，,]同比(?P<direction>增长|下降)(?P<value>[\d.]+)%", signed=True)
        extract("FDI_NEW_COMPANIES_CUM", r"新设立外商投资企业(?P<value>\d+)家")
        for code, label in [("FDI_MANUFACTURING_CUM", "制造业"), ("FDI_SERVICES_CUM", "服务业"), ("FDI_HIGHTECH_CUM", "高技术产业")]:
            extract(code, label + r"实际使用外资(?P<value>[\d.]+)亿元人民币", required=False)
    elif group == "nbs_release":
        m = re.search(r"(\d{4})年1[—－至-](\d{1,2})月份?(?:全国)?固定资产投资", text)
        if not m:
            annual = re.search(r"(\d{4})年(上半年|前三季度|全年)?(?:全国)?固定资产投资", text)
            m = (annual[0], annual[1], {"上半年": "6", "前三季度": "9"}.get(annual[2], "12")) if annual else None
        if not m:
            raise SourceError("统计局投资发布稿统计期未匹配")
        period = date(int(m[1]), int(m[2]), 1)
        growth = re.search(r"基础设施投资(?:（(?P<scope>[^）]{0,80})）)?(?:同比|比上年)(?:增长|下降)[\d.]+%", text)
        exclusion = growth if growth and growth["scope"] == "不含电力、热力、燃气及水生产和供应业" else None
        if growth and growth["scope"] and "不含" in growth["scope"] and not exclusion:
            raise SourceError("基建统计范围发生未登记变化，请先核验")
        definition = exclusion or re.search(r"基础设施投资[：:].{20,1500}?基础设施投资增速按可比口径计算。", text)
        if not definition:
            raise SourceError("基建投资统计范围未匹配，不能沿用旧口径")
        notes = definition[0]
        code = "INFRASTRUCTURE_EX_UTILITIES_CUM_YOY" if exclusion else "INFRASTRUCTURE_CUM_YOY"
        extract(code, r"基础设施投资(?:（[^）]{0,80}）)?(?:同比|比上年)(?P<direction>增长|下降)(?P<value>[\d.]+)%", signed=True)
    elif group == "pbc":
        m = re.search(r"(\d{4})年(\d{1,2})月(?:金融统计数据报告|社会融资规模(?:存量|增量)统计数据报告|末社会融资规模存量)", text)
        if not m:
            quarter = re.search(r"(\d{4})年(一季度|上半年|前三季度|全年)?(?:金融统计数据报告|社会融资规模(?:存量|增量)统计数据报告)", text)
            m = (quarter[0], quarter[1], {"一季度": "3", "上半年": "6", "前三季度": "9"}.get(quarter[2], "12")) if quarter else None
        if not m:
            raise SourceError("央行报告统计期未匹配")
        period = date(int(m[1]), int(m[2]), 1)
        notes = "初步统计；报告累计增量不等于当月；企事业单位为非金融企业及机关团体。"
        amount = r"(?P<value>[\d.]+)(?P<unit>万亿元|亿元)"
        direction = r"(?P<direction>增加|减少)"
        # Older releases publish finance, stock and cumulative flow separately.
        # Each recognised section remains complete; absent sections are never filled with zero.
        stock = "社会融资规模存量为" in text
        flow_phrase = "社会融资规模增量累计为"
        if period.month == 1 and flow_phrase not in text:
            # January monthly flow is exactly the year-to-date flow, with explicit period proof.
            flow_phrase = "社会融资规模增量为"
        flow = flow_phrase in text
        loans = "金融统计数据报告" in text
        if not any((stock, flow, loans)):
            raise SourceError("央行报告未包含登记的统计对象")
        if stock:
            extract("TSF_STOCK", "社会融资规模存量为" + amount, monetary=True)
            extract("TSF_STOCK_YOY", r"社会融资规模存量为[\d.]+万亿元，同比(?P<direction>增长|下降)(?P<value>[\d.]+)%", signed=True)
            extract("GOVERNMENT_BONDS_STOCK", "政府债券余额(?:为)?" + amount, monetary=True)
        if flow:
            # The first paragraph is cumulative; later paragraphs may be monthly.
            original = text
            chunks = [re.split(r"\d{1,2}月份?社会融资规模增量", text[match.start():], maxsplit=1)[0]
                      for match in re.finditer(re.escape(flow_phrase), text)]
            text = next((chunk for chunk in chunks if re.search(r"政府债券净融资[\d.]+(?:万亿元|亿元)", chunk)), "")
            extract("TSF_CUM", flow_phrase + amount, monetary=True)
            extract("GOVERNMENT_BONDS_CUM", r"政府债券净融资" + amount, monetary=True)
            text = original
        for code, label in ([("HOUSEHOLD", "住户"), ("COMPANY", r"企（事）业单位")] if loans else []):
            section = re.search(label + r"贷款(?:增加|减少)[^；。]+", text)
            if not section:
                raise SourceError("央行贷款部门字段未匹配")
            if period.month != 1 and not re.search(r"(?:前[一二两三四五六七八九十\d]+个?月|上半年|一季度|前三季度|全年)[^。]{0,60}人民币贷款", text):
                raise SourceError("央行贷款累计范围未核验，不能用当月增量回填累计指标")
            extract(code + "_LOANS_CUM", label + "贷款" + direction + amount, monetary=True, signed=True)
            original = text
            text = section[0]
            for term, name in [("SHORT", "短期贷款"), ("LONG", "中长期贷款")]:
                extract(code + "_" + term + "_LOANS_CUM", name + direction + amount, monetary=True, signed=True)
            if code == "COMPANY":
                extract("COMPANY_BILLS_CUM", "票据融资" + direction + amount, monetary=True, signed=True)
            text = original
    else:
        raise SourceError("未登记的官方适配器")
    return points
