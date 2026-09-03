from django import template

register = template.Library()
REASONS = {
    "policy_disabled": "策略未启用", "config_invalid": "策略参数无效",
    "account_status": "账户证据不完整", "account_currency": "账户币种不符",
    "account_age_invalid": "账户确认时间无效", "account_age_future": "账户确认时间在未来",
    "account_age_expired": "账户容量确认已过期", "account_margin_status_unknown": "融资状态未确认",
    "account_margin_balance_unknown": "借款余额未确认", "account_margin_active": "存在融资或借贷",
    "account_nav_missing": "账户净值缺失", "account_nav_nonpositive": "账户净值不大于零",
    "account_cash_missing": "已结算现金缺失", "account_cash_negative": "已结算现金为负",
    "account_reserved_missing": "预留现金缺失", "account_reserved_negative": "预留现金为负",
    "account_reserved_exceeds": "预留现金超过现金余额", "account_exposure_missing": "已有标的敞口缺失",
    "account_exposure_negative": "已有标的敞口无效", "contract_count": "合约张数无效",
    "option_type": "期权类型不符", "dte": "到期天数无效", "strike": "行权价无效",
    "standard": "未证实为标准合约", "adjusted_status_unknown": "合约调整状态未知",
    "adjusted": "已调整合约不在支持范围", "index": "指数期权不在支持范围",
    "asset": "标的资产类型不符", "underlying_market": "标的市场不符",
    "exercise": "行权方式未核实", "deliverable": "交割标的未核实",
    "multiplier": "合约乘数不是已核实的 100 股", "settlement": "交割方式未核实",
    "quote_currency": "报价币种不符", "quote_quality": "报价数据不完整",
    "quote_delay": "报价实时性未通过", "quote_freshness": "报价新鲜度未通过",
    "quote_age_invalid": "报价时间无效", "quote_age_future": "报价时间在未来",
    "quote_age_expired": "报价超过允许时效", "quote_session": "未核实为正常交易时段",
    "quote_bid": "买价无效", "quote_ask": "卖价无效", "quote_spread": "买卖价差超过上限",
    "quote_open_interest": "未平仓合约数不足", "quote_volume": "成交量不足",
    "quote_probability_missing": "行权模型概率缺失", "quote_probability_range": "行权模型概率数值无效",
    "event": "财报或除息门控未通过", "technical": "技术证据不完整",
    "cash_insufficient": "可用现金不足以全额担保", "nav_ratio": "标的敞口超过账户净值上限",
    "execution_gate_closed": "执行闸门关闭，仅供分析",
    "covered_shares_insufficient": "可用于备兑的正股不足 100 股",
    "covered_call_cost_basis_missing": "备兑正股成本证据缺失",
    "covered_call_strike_below_cost": "看涨期权行权价低于正股成本",
    "quote_not_realtime_fresh": "报价实时性或新鲜度未通过",
    "quote_bid_ask": "买卖报价无效或价差不符",
}


@register.filter
def wheel_reason(value):
    return REASONS.get(str(value), "其他规则阻断：" + str(value))


@register.filter
def wheel_reasons(values):
    if not values:
        return "无"
    return "；".join(wheel_reason(v) for v in (values if isinstance(values, list) else [values]))
