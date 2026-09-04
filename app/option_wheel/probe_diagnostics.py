"""Bounded, allowlisted diagnostics: never publish provider text or payloads."""

import re


STEPS = {
    "query_subscription": "订阅状态接口",
    "subscribe": "订阅接口",
    "unsubscribe": "退订接口",
    "get_market_snapshot": "行情快照接口",
    "get_stock_quote": "动态报价接口",
    "subscription_before": "订阅额度预检",
    "subscription_after": "订阅恢复核对",
    "underlying_snapshot": "正股快照",
    "expiration_date": "到期日",
    "chain": "期权链",
    "subscription": "行情订阅",
    "option_snapshot": "期权快照",
    "option_quote": "期权报价",
    "get_option_exercise_probability": "概率数据",
    "get_option_volatility": "波动率数据",
    "request_history_kline": "历史价格",
    "get_earnings_calendar": "财报日历",
    "get_dividend_calendar": "除息日历",
}
CATEGORIES = {
    "method_lookup_error": "SDK 接口读取失败",
    "method_unsupported": "SDK 不支持接口",
    "signature_mismatch": "SDK 参数不兼容",
    "sdk_exception": "SDK 调用异常",
    "invalid_response": "返回格式异常",
    "provider_error": "行情服务拒绝请求",
    "field_missing": "必要字段缺失",
    "empty": "无返回数据",
    "malformed_data": "数据格式异常",
    "inconsistent_page_count": "分页数量不一致",
    "incomplete_pagination": "分页不完整",
    "pagination_limit": "超过分页上限",
}
ERRORS = {
    "futu_sdk_import_failed": "无法加载 Futu SDK",
    "dynamic_subscription_lock_unavailable": "行情探针正在运行或无法取得锁",
    "quote_context_connection_failed": "无法建立 OpenD 行情连接",
    "market_session:not_regular": "未取得正常交易时段证据",
    "underlying_snapshot:empty": "正股快照为空",
    "expiration_date:no_eligible_expiration": "没有符合范围的到期日",
    "expiration_date:truncated_to_max_scan": "到期日扫描达到上限",
    "representative_contract:no_standard_put": "没有标准 Put 代表合约",
    "subscription:quote_subtype_unsupported": "SDK 不支持报价订阅类型",
    "unexpected_symbol_probe_error": "标的探测发生内部异常",
    "unexpected_probe_runtime_error": "行情探针发生内部异常",
    "probe_interrupted": "行情探针被中断",
    "subscription_cleanup_exception": "临时订阅清理异常",
    "subscription_after_query_exception": "订阅恢复查询异常",
    "quote_context_close_failed": "行情连接关闭失败",
    "dynamic_subscription_lock_release_failed": "行情探针锁释放失败",
}
STATES = {
    "MORNING": "正常交易时段", "AFTERNOON": "正常交易时段",
    "PRE_MARKET_BEGIN": "盘前", "AFTER_HOURS_BEGIN": "盘后",
    "CLOSED": "休市", "REST": "休市",
}
CLEANUP = {"restored": "已恢复", "not_requested": "未发起", "partial": "核对不完整", "failed": "失败"}


def _issue_text(issue):
    if isinstance(issue, str):
        if issue in ERRORS:
            return ERRORS[issue]
        if issue.startswith(("optional_method_unsupported:", "required_methods_unsupported:")):
            methods = issue.split(":", 1)[1].split(",")
            names = [STEPS.get(method, "其他必要接口") for method in methods[:8]]
            return "SDK 不支持：" + "、".join(names)
        if issue.startswith("dynamic candidate limit exceeded:"):
            return "本批候选数量超过系统上限"
        if issue.startswith("subscription_cleanup:"):
            return "临时订阅未完全恢复"
        return "未分类错误（原始内容已隐藏）"
    if not isinstance(issue, dict):
        return "未知诊断项"
    source = str(issue.get("source", ""))
    step = STEPS.get(source)
    if step is None:
        step = next((STEPS[key] for key in ("chain", "option_quote", "option_snapshot", "subscription")
                     if source.startswith(key + "_")), "其他数据步骤")
    category = CATEGORIES.get(str(issue.get("category", "")), "校验未通过")
    # Only fixed hints leave this function; provider text may contain credentials.
    error = str(issue.get("error", "")).lower()
    hints = []
    for words, hint in (
        (("未登录", "not login", "not logged", "login required"), "服务提示未登录"),
        (("权限", "permission", "no right", "not entitled"), "服务提示权限不足"),
        (("限频", "频率", "too frequent", "rate limit"), "服务提示请求限频"),
        (("超时", "timeout", "timed out"), "服务提示超时"),
    ):
        if any(word in error for word in words):
            hints.append(hint)
    return f"{step}：{category}" + (f"（{'、'.join(hints)}）" if hints else "")


def probe_failure_summary(result, requested_symbols):
    """Return only fixed text and validated, caller-authorized symbols."""
    items = []

    def add(text):
        if text not in items and len(items) < 12:
            items.append(text)

    for issue in result.get("errors", [])[:30]:
        add(_issue_text(issue))
    allowed = {f"US.{symbol}" for symbol in requested_symbols if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,15}", symbol)}
    for symbol in result.get("symbols", [])[:20]:
        if symbol.get("symbol") not in allowed:
            continue
        label = symbol["symbol"].removeprefix("US.")
        for issue in symbol.get("errors", [])[:20]:
            add(f"{label}：{_issue_text(issue)}")
        for key, title in (("history", "历史价格"), ("earnings", "财报日历"), ("ex_dividend", "除息日历")):
            evidence = symbol.get(key, {})
            if evidence.get("status") not in (None, "ok", "not_requested"):
                add(f"{label}：{title}校验未通过")
                if evidence.get("issue"):
                    add(f"{label}：{_issue_text(evidence['issue'])}")
        for contract in symbol.get("representative_contracts", [])[:12]:
            for key, title in (("probability", "概率数据"), ("volatility", "波动率数据")):
                evidence = contract.get("analytics", {}).get(key, {})
                if evidence.get("status") not in (None, "ok", "not_requested"):
                    add(f"{label}：{title}校验未通过")
                    if evidence.get("issue"):
                        add(f"{label}：{_issue_text(evidence['issue'])}")
        if symbol.get("status") != "success" and not symbol.get("errors"):
            add(f"{label}：报价、合约身份或分析字段不完整")
    if not items:
        add("未返回可分类的失败项")
    market = STATES.get(result.get("market_state", {}).get("market_us"), "未知")
    cleanup = CLEANUP.get(result.get("subscription", {}).get("cleanup_status"), "未知")
    return f"市场状态：{market}；订阅清理：{cleanup}。" + "；".join(items)
