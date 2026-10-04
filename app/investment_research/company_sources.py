"""Bounded public-source collectors. Currency parameters are deliberately omitted."""
import json
import math
import socket
from contextlib import contextmanager
from django.conf import settings
from django.utils import timezone
from .futu_financials import _statement_data, _breakdown_data, provider_code, statement_tables
from .material_store import save_material, record_failure
from .models import FutuFinancialSnapshot
from .providers.futu_public import public_url, fetch_page, enrich_names

SOURCE_TASKS = {
    "sec": "SEC 官方文件", "facts": "SEC 财务指标",
    "profile": "公司概况", "financials": "富途财务报表与主营构成",
    "research": "晨星研究报告",
    "ir": "公司官方 IR",
}
RETIRED_SOURCES = {"ratings", "industry"}


def json_value(value):
    if hasattr(value, "to_dict"):
        return json_value(value.to_dict("records"))
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if isinstance(value, float):
        return str(value) if math.isfinite(value) else None
    if hasattr(value, "item"):
        return json_value(value.item())
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


@contextmanager
def quote_context():
    from futu import OpenQuoteContext
    socket.create_connection((settings.FUTU_OPEND_HOST, settings.FUTU_OPEND_PORT), timeout=3).close()
    context = OpenQuoteContext(host=settings.FUTU_OPEND_HOST, port=settings.FUTU_OPEND_PORT)
    try:
        yield context
    finally:
        context.close()


def call(context, method, *args, **kwargs):
    fn = getattr(context, method, None)
    if fn is None:
        raise ValueError("当前富途 SDK 尚不支持此项接口，需要更新 SDK。")
    result = fn(*args, **kwargs)
    ret, data = result[:2]
    if ret != 0:
        # Provider errors may contain account details; only show a fixed message.
        raise ValueError("富途暂未提供此项数据，可能是覆盖范围、权限或请求频率限制。")
    if len(result) == 4:
        return {"items": json_value(data), "next_page": json_value(result[2]), "total": json_value(result[3])}
    return json_value(data)


def search_futu(query):
    with quote_context() as context:
        return call(context, "get_search_quote", query, max_count=20)


def collect_futu(security, kind):
    if kind in RETIRED_SOURCES:
        raise ValueError("此类资料已停止采集：现有接口未提供公司研究所需的事实或分析理由。")
    code = provider_code(security)
    with quote_context() as context:
        if kind == "financials":
            return collect_financials(security, code, context)
        method = {"profile": "get_company_profile", "research": "get_research_morningstar_report",
                  "ratings": "get_research_analyst_consensus"}.get(kind)
        if method:
            data = call(context, method, code)
        elif kind == "industry":
            # Plate membership is explicit. Industry-chain exploration is kept
            # separate: matching words do not establish a supplier relationship.
            data = {"plates": call(context, "get_owner_plate", [code]), "chains": []}
            from futu import Market
            prefix = code.split(".")[0]
            market = getattr(Market, "CN" if prefix in {"SH", "SZ"} else prefix, None)
            if market:
                keywords = [p.get("plate_name") for p in data["plates"]
                            if p.get("plate_type") in {"INDUSTRY", "CONCEPT"} and p.get("plate_name")][:3]
                data["searches"], data["details"] = [], []
                seen = set()
                for keyword in keywords or [security.name[:30]]:
                    try:
                        result = call(context, "get_industrial_chain_list", market, keyword=keyword, count=3)
                        data["searches"].append({"keyword": keyword, **result})
                        for row in result["items"]:
                            if row["chain_id"] not in seen:
                                seen.add(row["chain_id"])
                                data["chains"].append(row)
                    except ValueError:
                        data["searches"].append({"keyword": keyword, "error": "此次未取得产业链搜索结果"})
                for row in data["chains"][:2]:
                    try:
                        detail = call(context, "get_industrial_chain_detail", row["chain_id"])
                        stocks = call(context, "get_industrial_plate_stock", chain_id=row["chain_id"], market_list=[market], count=10)
                        data["details"].append({"detail": detail, "stocks": stocks})
                    except ValueError:
                        pass
            data["note"] = "行业归属来自富途；产业链搜索结果仅供进一步研究，不代表已证实的供应或客户关系。"
        else:
            raise ValueError("未知资料类型")
    if not data or isinstance(data, dict) and not any(data.values()):
        raise ValueError("接口未返回可用资料。")
    date = data.get("analyst_report_update_time_str", "") if isinstance(data, dict) else ""
    _, changed = save_material(security, kind, kind, SOURCE_TASKS[kind],
        source_url=f"https://www.futunn.com/stock/{code.split('.', 1)[1]}-{code.split('.')[0]}",
        data={"provider": "富途 OpenD", "code": code, "payload": data}, report_date=date)
    return "已保存新版本" if changed else "资料无变化，已核查"


def collect_financials(security, code, context):
    statements, errors = [], []
    for stype, title in ((1, "利润表"), (2, "资产负债表"), (3, "现金流量表"), (4, "主要指标")):
        try:
            payload = call(context, "get_financials_statements", code, statement_type=stype, financial_type=7, num=3)
        except ValueError:
            errors.append(f"{title}本次获取失败，已有版本仍保留。")
            record_failure(security, f"futu-statement-{stype}", "statement", title, errors[-1])
            continue
        reports = _statement_data(payload)
        if stype < 4 and any(not item["name"] for rpt in reports for item in rpt["items"]):
            try:
                url = public_url(code, stype)
                raw = fetch_page(url)
                evidence = enrich_names(code, stype, reports, raw)
                if not evidence:
                    recent = call(context, "get_financials_statements", code,
                                  statement_type=stype, financial_type=10, num=3)
                    verification = _statement_data(recent, annual_only=False)
                    evidence = enrich_names(code, stype, reports, raw, verification_reports=verification)
                    save_material(security, f"futu-label-check-{stype}", "labels", f"{title}同源核对数据",
                                  data={"original": recent, "matches": evidence})
                save_material(security, f"futu-labels-{stype}", "labels", f"{title}字段核对来源",
                    source_url=url, raw=raw, data={"matches": evidence}, media_type="text/html")
            except Exception:
                errors.append(f"{title}部分字段名称未能从富途同源页面核对。")
        statements.append({"type": stype, "title": title, "reports": reports})
        save_material(security, f"futu-statement-{stype}", "statement", title,
            data={"provider": "富途 OpenD", "code": code, "original": payload,
                  "statement": statements[-1]})
    try:
        breakdown_payload = call(context, "get_financials_revenue_breakdown", code, financial_type=7)
        breakdown = _breakdown_data(breakdown_payload)
        breakdown_error = ""
    except ValueError:
        breakdown, breakdown_payload = None, {}
        breakdown_error = "主营构成获取失败，可以重试；先阅读已取得的报表。"
        errors.append(breakdown_error)
    if not any(r["items"] for s in statements for r in s["reports"]) and not breakdown:
        raise ValueError("富途未返回可用的报表或主营构成，已有版本仍保留。")
    data = {"statements": statements, "breakdown": breakdown, "breakdown_error": breakdown_error, "warnings": errors}
    FutuFinancialSnapshot.objects.update_or_create(security=security, defaults={
        "provider_code": code, "data": data, "fetched_at": timezone.now(), "last_error": "；".join(errors)[:500]})
    tables = statement_tables(statements, code)
    missing = sum(t["missing_names"] for t in tables)
    _, changed = save_material(security, "financials", "financials", SOURCE_TASKS["financials"],
        data={**data, "original_breakdown": breakdown_payload, "missing_names": missing},
        report_date=max((r.get('period_end', '') for t in tables for r in t['reports']), default=''))
    if len(statements) != 4 or breakdown_error:
        raise ValueError("已保存取得的财务资料，部分报表或主营构成仍待补充。")
    return f"已保存，{missing} 项字段名称待补充" if missing else "财务资料已保存，字段名称已核对"
