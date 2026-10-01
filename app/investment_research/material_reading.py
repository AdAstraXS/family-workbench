"""Human reading and the five-step source inventory share the same archive."""
from .models import CompanyMaterial
from .official_ir import documents_for_security
from .number_display import money, number
from django.db.models.functions import Length

RESEARCH_SECTIONS = {
    "investment_thesis_content": "投资论点", "fundamentals_content": "基本面",
    "economic_moat_content": "竞争优势", "uncertainty_content": "不确定性与风险",
    "financial_health_content": "财务健康", "capital_allocation_content": "资本配置",
    "valuation_content": "估值分析", "fair_value_content": "公允价值分析",
    "analyst_note_content": "分析师最新观点", "bull_say": "看多观点", "bear_say": "看空观点",
}


def profile_content(data):
    payload = data.get("payload", data)
    result = {"narratives": [], "highlights": [], "details": []}
    if not isinstance(payload, list):
        return result
    narrative_names = ("公司业务", "主营业务", "业务", "公司简介", "公司介绍", "业务描述")
    highlights = {"成立日期", "CEO", "总经理", "员工数量", "年结日"}
    for row in payload:
        if not isinstance(row, dict):
            continue
        name, value = str(row.get("name") or "").strip(), str(row.get("value") or "").strip()
        if not name or value.lower() in {"", "n/a", "nan", "none", "--", "-"}:
            continue
        item = {"title": name, "text": value}
        if name in narrative_names:
            result["narratives"].append(item)
        elif name in highlights:
            if name == "员工数量" and value.isdigit():
                item["text"] = f"{int(value):,} 人"
            result["highlights"].append(item)
        else:
            result["details"].append(item)
    result["narratives"].sort(key=lambda r: narrative_names.index(r["title"]))
    return result


def reading_sections(version):
    data = version.data
    payload = data.get("payload", data)
    sections = []
    if version.material.kind == "profile" and isinstance(payload, list):
        return profile_content(data)["narratives"]
    elif version.material.kind == "research":
        for key, label in RESEARCH_SECTIONS.items():
            values = payload.get(key, [])
            for value in values if isinstance(values, list) else [values]:
                if isinstance(value, dict) and value.get("context"):
                    sections.append({"title": label, "text": value["context"],
                                     "date": value.get("update_time_str", "")})
    elif version.material.kind == "ratings":
        labels = {"buy": "买入占比", "hold": "持有占比", "sell": "卖出占比", "strong_buy": "强烈买入占比", "underperform": "跑输大盘占比",
                  "buy_count": "买入", "hold_count": "持有", "sell_count": "卖出",
                  "strong_buy_count": "强烈买入", "strong_sell_count": "强烈卖出",
                  "analyst_count": "分析师数量", "rating": "评级", "rating_name": "评级",
                  "update_time_str": "更新时间", "total": "样本数量", "num": "样本数量"}
        def walk(value):
            if isinstance(value, dict):
                for k, v in value.items():
                    if k in labels and not isinstance(v, (dict, list)):
                        display = {"BUY": "买入", "HOLD": "持有", "SELL": "卖出", "STRONG_BUY": "强烈买入", "UNDERPERFORM": "跑输大盘"}.get(str(v), str(v))
                        if k in {"buy", "hold", "sell", "strong_buy", "underperform"}:
                            ratio = number(v)
                            display = f"{ratio:,.2f}%" if ratio is not None else "来源未标注"
                        sections.append({"title": labels[k], "text": display})
                    elif isinstance(v, (dict, list)):
                        walk(v)
            elif isinstance(value, list):
                for row in value:
                    walk(row)
        walk(payload)
    elif version.material.kind == "industry":
        for plate in payload.get("plates", []):
            name = plate.get("plate_name") or plate.get("name")
            if name:
                sections.append({"title": "所属行业 / 板块", "text": name})
        for chain in payload.get("chains", []):
            name = chain.get("name") or chain.get("chain_name")
            if name:
                sections.append({"title": "相关产业链线索", "text": name})
        for entry in payload.get("details", []):
            detail = entry.get("detail", {})
            names = [n.get("name", "") for n in detail.get("node_list", []) if n.get("name")]
            if names:
                sections.append({"title": detail.get("name") or "产业链环节", "text": " · ".join(names)})
            stocks = entry.get("stocks", {}).get("items", [])
            if stocks:
                sections.append({"title": "相关公司（来源列表前十项）", "text": "、".join(
                    str(s.get("name") or s.get("stock_name") or s.get("code") or "") for s in stocks)})
        sections.append({"title": "资料边界", "text": payload.get("note", "")})
    return sections


CORE_FACTS = {
    "RevenueFromContractWithCustomerExcludingAssessedTax": "客户合同收入（不含代收税费）", "Revenues": "收入（广义口径）",
    "SalesRevenueNet": "销售收入净额", "Revenue": "营业收入", "GrossProfit": "毛利",
    "OperatingIncomeLoss": "营业利润", "ProfitLossFromOperatingActivities": "营业利润",
    "NetIncomeLoss": "净利润", "ProfitLoss": "净利润", "Assets": "资产总额",
    "Liabilities": "负债总额", "StockholdersEquity": "股东权益", "Equity": "股东权益",
    "NetCashProvidedByUsedInOperatingActivities": "经营活动现金流",
    "CashFlowsFromUsedInOperatingActivities": "经营活动现金流",
    "EarningsPerShareDiluted": "稀释每股收益", "DilutedEarningsLossPerShare": "稀释每股收益",
}


def fact_rows(data):
    rows = []
    for taxonomy, facts in data.get("facts", {}).items():
        if taxonomy not in {"us-gaap", "ifrs-full"}:
            continue
        for code, fact in facts.items():
            if code not in CORE_FACTS:
                continue
            for unit, values in fact.get("units", {}).items():
                if not (len(unit) == 3 and unit.isupper() or unit.endswith("/shares")):
                    continue
                by_period = {}
                for value in values:
                    if value.get("form") not in {"10-K", "20-F", "40-F", "10-K/A", "20-F/A", "40-F/A"}:
                        continue
                    start, end = value.get("start", ""), value.get("end", "")
                    if start:
                        from datetime import date
                        try:
                            days = (date.fromisoformat(end) - date.fromisoformat(start)).days
                        except ValueError:
                            continue
                        if not 330 <= days <= 380:
                            continue
                    period = (start, end)
                    previous = by_period.get(period)
                    if not previous or value.get("filed", "") > previous.get("filed", ""):
                        by_period[period] = value
                for (start, end), value in sorted(by_period.items(), reverse=True)[:3]:
                    rows.append({"label": CORE_FACTS[code], "source_label": fact.get("label", ""),
                        "code": code, "start": start, "end": end, "value": number(value.get("val")),
                        "period": f"{start} — {end}" if start else end, "currency": unit,
                        "standard": "IFRS" if taxonomy == "ifrs-full" else "US GAAP",
                        "amount": money(value.get("val"), unit.split("/")[0], per_share=unit.endswith("/shares")),
                        "filed": value.get("filed"), "accession": value.get("accn")})
    return rows


STEPS = (
    ("了解生意", ("profile", "financials", "sec_document"), "公司概况、主营构成、年报业务描述"),
    ("看懂竞争", ("research", "sec_document"), "晨星研究与年报竞争讨论；实际客户、供应商与竞争关系需有正文证据"),
    ("核查财务", ("financials", "facts", "sec_document"), "财务报表、原币种、报告期和会计准则"),
    ("机会与风险", ("research", "sec_document"), "年报风险、晨星研究与多空论据；评级比例不作为事实证据"),
    ("形成待验证假设", (), "基于前四步资料由 AI 辅助提出问题，再由你确认；资料齐全不代表判断成立"),
)


def inventory(security):
    materials = list(CompanyMaterial.objects.filter(security=security).exclude(kind__in=["labels", "statement", "sec_index"]).exclude(source_url__endswith="-index-headers.html").order_by("kind", "title"))
    refs = []
    for material in materials:
        version = material.versions.annotate(text_size=Length("text")).only("id", "number", "report_date", "fetched_at").first()
        material.latest = version
        if version and material.kind not in {"industry", "ratings"}:
            refs.append({"material_id": material.pk, "version_id": version.pk,
                         "kind": material.kind, "title": material.title,
                         "report_date": version.report_date, "fetched_at": version.fetched_at.isoformat(),
                         "last_error": material.last_error,
                         "has_readable_text": version.text_size > 0 if material.kind == "sec_document"
                         else bool(profile_content(version.data)["narratives"]) if material.kind == "profile" else None})
    legacy = documents_for_security(security).exclude(content_text="").order_by("-published_at")
    legacy_refs = list(legacy.values("id", "title", "source", "published_at", "source_url")[:100])
    for item in legacy_refs:
        refs.append({"legacy_document_id": item["id"], "kind": "sec_document", "title": item["title"],
                     "report_date": str(item["published_at"] or ""), "has_readable_text": True})
    steps = []
    for title, kinds, boundary in STEPS:
        present = [r for r in refs if r["kind"] in kinds and r.get("has_readable_text") is not False]
        missing = [k for k in kinds if not any(r["kind"] == k for r in present)]
        from .company_sources import SOURCE_TASKS
        steps.append({"title": title, "materials": present, "missing": [SOURCE_TASKS.get(k, "SEC 报告正文") for k in missing],
                      "boundary": boundary, "status": "已有部分材料" if present else "待分析" if not kinds else "待补充"})
    return materials, {"company": security.name, "symbol": security.symbol, "market": security.market,
        "currency_policy": "原币种、原统计口径；未做货币换算", "steps": steps,
        "existing_official_documents": legacy_refs,
        "note": "这是一份资料清单，不是 AI 分析结论。读取时须确认正文、时效性及缺失字段。"}
