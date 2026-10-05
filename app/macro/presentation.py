"""Read-only growth views over exact calendar periods and Decimal observations."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext, ROUND_HALF_UP


CPI_BASES = {"CPIAUCSL": "CPIAUCNS", "CPILFESL": "CPILFENS", "PPIFIS": "PPIFID"}
AUXILIARY = set(CPI_BASES.values())
US_GROWTH = {"CPIAUCSL", "CPILFESL", "CPIAUCNS", "CPILFENS", "PCEPI", "PCEPILFE", "GDPC1", "GDPDEF",
             "INDPRO", "RSAFS", "PCEC96", "DSPIC96", "CES0500000003", "HOUST", "PERMIT", "HSN1F", "PAYEMS", "PPIFIS", "PPIFID", "DGORDER"}
CN_PAIRS = {"FAI_CUM": "FAI_CUM_YOY", "PROPERTY_INVESTMENT_CUM": "PROPERTY_INVESTMENT_CUM_YOY",
            "INDUSTRIAL_PROFIT_CUM": "INDUSTRIAL_PROFIT_CUM_YOY", "FDI_CUM": "FDI_CUM_YOY",
            "RETAIL": "RETAIL_YOY", "M2": "M2_YOY", "TSF_STOCK": "TSF_STOCK_YOY", "GDP_QUARTER": "GDP_REAL_YOY_INDEX"}
CN_LEVELS = {"EXPORTS", "IMPORTS", "GDP_ANNUAL", "FISCAL_REVENUE_CUM", "FDI_NEW_COMPANIES_CUM",
             "FDI_MANUFACTURING_CUM", "FDI_SERVICES_CUM", "FDI_HIGHTECH_CUM", "GOVERNMENT_BONDS_STOCK"}
CN_INDEX_RATES = {"GDP_REAL_YOY_INDEX", "HOUSE_NEW_MOM", "HOUSE_NEW_YOY", "HOUSE_USED_MOM", "HOUSE_USED_YOY"}
PRESENTATION_CODES = US_GROWTH | set(CN_PAIRS) | set(CN_PAIRS.values()) | CN_LEVELS | CN_INDEX_RATES


def chart_reference(spec, measure):
    if measure != "level":
        return "0"
    if spec.code.startswith("PMI_"):
        return "50"
    if spec.code in CN_INDEX_RATES:
        return "100"
    if spec.unit == "%" or "增量" in spec.name or "净融资" in spec.name:
        return "0"
    return None


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    unit: str = "%"


def profile(spec):
    if spec.country == "US" and spec.code in US_GROWTH:
        modes = [Metric("yoy", "同比" + ("（未季调）" if spec.code in CPI_BASES or spec.code in AUXILIARY else "")),
                 Metric("mom", "季度环比" if spec.frequency == "季度" else "环比" + ("（未季调）" if spec.code in AUXILIARY else "（季调）"))]
        default = "yoy"
        if spec.code in {"GDPC1", "GDPDEF"}:
            modes.insert(0, Metric("annualized", "季度环比折年"))
            if spec.code == "GDPC1":
                default = "annualized"
        if spec.code == "PAYEMS":
            modes.insert(0, Metric("change", "月新增", "千人"))
            default = "change"
        note = "按当前已入库指数或水平值计算；与官方发布稿的舍入值可能存在差异。环比比较相邻月份，同比比较上年同月。"
        if spec.code in CPI_BASES:
            note += " 价格指数同比采用对应未季调序列，环比采用季调序列；缺少未季调基数时不以季调同比替代。"
        if spec.frequency == "季度":
            note = "同比比较上年同季，季度环比比较上季。GDP 季度环比折年 =（本季 ÷ 上季的比值⁴ − 1）× 100%，与普通季度环比分开。按当前已入库版本计算，可能与官方舍入值略有差异。"
        if spec.code == "PAYEMS":
            note += " 月新增 = 本月人数 − 上月人数；原始人数单位为千人。"
        return {"modes": modes, "default": default, "note": note}
    if spec.country == "CN" and spec.code in CN_INDEX_RATES:
        label = "环比" if spec.code.endswith("MOM") else "实际同比" if spec.code == "GDP_REAL_YOY_INDEX" else "同比"
        return {"modes": [Metric("index_rate", label)], "default": "index_rate", "note": "原始指数以对比期=100，变化率 = 指数 − 100。原始指数保留在次要位置。"}
    if spec.country == "CN" and spec.code in CN_PAIRS:
        label = "实际同比" if spec.code == "GDP_QUARTER" else "累计同比" if "累计" in spec.basis else "同比"
        modes = [Metric("yoy", label)]
        if spec.code in {"M2", "TSF_STOCK", "RETAIL"}:
            modes.append(Metric("mom", "环比（未季调）"))
        return {"modes": modes, "default": "level", "paired": True, "note": "同比优先使用同统计期的来源发布增速，缺少该期增速时不以金额强算替代。" +
                ("本页原始 GDP 为现价金额；实际同比来自可比价格指数。实际季度环比尚未接入，不能用未季调名义金额冒充。" if spec.code == "GDP_QUARTER" else "累计指标不计算相邻累计值的环比；月度未季调环比受季节因素影响。")}
    if spec.country == "CN" and spec.code in CN_LEVELS:
        modes = [Metric("yoy", "累计同比" if "累计" in spec.basis else "名义同比" if spec.code == "GDP_ANNUAL" else "同比")]
        if spec.frequency == "月度" and "累计" not in spec.basis:
            modes.append(Metric("mom", "环比（未季调）"))
        return {"modes": modes, "default": "yoy", "note": "按当前已入库同口径值计算。累计同比比较去年相同累计区间，不计算累计环比；未季调月度环比会受春节和季节因素影响。对比基数非正数、缺期或缺值时不计算。"}
    return None


def shifted(period, months):
    value = period.year * 12 + period.month - 1 + months
    return date(value // 12, value % 12 + 1, 1)


def percentage(current, base, power=1):
    if current is None or base is None or base <= 0 or current < 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 48
        return (((current / base) ** power - 1) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def values(spec, period, current, history):
    """history is keyed by (country, code, exact period); absent/null never bridged."""
    p = profile(spec)
    if not p or not period:
        return {}
    result = {}
    for mode in p["modes"]:
        if mode.key == "index_rate":
            result[mode.key] = current - 100 if current is not None else None
        elif mode.key == "yoy" and spec.country == "CN" and spec.code in CN_PAIRS:
            rate = history.get(("CN", CN_PAIRS[spec.code], period))
            result[mode.key] = rate - 100 if rate is not None and spec.code == "GDP_QUARTER" else rate
        else:
            source = CPI_BASES.get(spec.code, spec.code) if mode.key == "yoy" else spec.code
            numerator = history.get((spec.country, source, period)) if source != spec.code else current
            prior = shifted(period, -12 if mode.key == "yoy" else -3 if spec.frequency == "季度" else -1)
            base = history.get((spec.country, source, prior))
            result[mode.key] = (numerator - base if numerator is not None and base is not None else None) if mode.key == "change" else percentage(numerator, base, 4 if mode.key == "annualized" else 1)
    return result


def presentation(spec, period, current, history):
    p = profile(spec)
    if not p:
        return None
    computed = values(spec, period, current, history)
    metrics = [{"key": m.key, "label": m.label, "unit": m.unit, "value": computed.get(m.key)} for m in p["modes"]]
    default = p["default"]
    if default == "level" or (current is not None and all(m["value"] is None for m in metrics)):
        default = "level"
        primary = {"key": "level", "label": "原始值", "unit": spec.unit, "value": current}
    else:
        primary = next(m for m in metrics if m["key"] == default)
    return {**p, "default": default, "metrics": metrics, "primary": primary}
