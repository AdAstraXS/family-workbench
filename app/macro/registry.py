"""Reviewed source mappings. Changes to units/definitions require a new series code."""
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Series:
    code: str
    name: str
    country: str
    category: str
    unit: str
    frequency: str
    provider: str
    group: str
    field: str
    basis: str
    seasonal: str = "未季调"
    params: dict = field(default_factory=dict)
    selector: str = ""
    transform: str = "identity"
    version: int = 1

    def definition(self):
        return asdict(self)


SERIES = []


def cn(group, function, fields, *, provider="akshare", params=None, frequency="月度", basis="当月", selector="", transform="identity", seasonal="未季调"):
    for code, name, column, unit, category in fields:
        SERIES.append(Series(code, name, "CN", category, unit, frequency, provider, group,
                             column, basis, seasonal, params={"function": function, **(params or {})}, selector=selector, transform=transform))


cn("cn_cpi", "macro_china_cpi", [
    ("CPI_YOY", "居民消费价格同比", "全国-同比增长", "%", "通胀"),
    ("CPI_MOM", "居民消费价格环比", "全国-环比增长", "%", "通胀"),
])
cn("cn_ppi", "macro_china_ppi", [("PPI_YOY", "工业生产者出厂价格同比", "当月同比增长", "%", "通胀")])
cn("cn_pmi", "macro_china_pmi", [
    ("PMI_MANUFACTURING", "制造业 PMI", "制造业-指数", "指数（50为荣枯线）", "景气"),
    ("PMI_NONMANUFACTURING", "非制造业商务活动指数", "非制造业-指数", "指数（50为荣枯线）", "景气"),
], seasonal="季调")
cn("cn_retail", "macro_china_consumer_goods_retail", [
    ("RETAIL", "社会消费品零售总额", "当月", "亿元", "消费"),
    ("RETAIL_YOY", "社会消费品零售同比", "同比增长", "%", "消费"),
], basis="来源当月口径；1—2月可能合并")
cn("cn_industry", "macro_china_gyzjz", [("INDUSTRY_YOY", "规模以上工业增加值同比", "同比增长", "%", "生产")], basis="来源当月口径；1—2月合并")
cn("cn_investment", "macro_china_gdzctz", [("FAI_CUM", "固定资产投资累计金额（不含农户）", "自年初累计", "亿元", "投资")], basis="年初至今累计")
cn("cn_money", "macro_china_money_supply", [
    ("M2", "广义货币 M2 余额", "货币和准货币(M2)-数量(亿元)", "亿元", "金融"),
    ("M2_YOY", "广义货币 M2 同比", "货币和准货币(M2)-同比增长", "%", "金融"),
], basis="月末")
cn("cn_loans", "macro_china_new_financial_credit", [("LOANS_NEW", "新增人民币贷款", "当月", "亿元", "金融")])
cn("cn_trade", "macro_china_hgjck", [
    ("EXPORTS", "货物出口额", "当月出口额-金额", "亿美元", "外贸"),
    ("IMPORTS", "货物进口额", "当月进口额-金额", "亿美元", "外贸"),
], transform="thousand_usd_to_100m", basis="当月；原始千美元除以100000换算为亿美元")
cn("cn_fiscal", "macro_china_czsr", [("FISCAL_REVENUE_CUM", "财政收入累计金额", "累计", "亿元", "财政")], basis="年初至今累计")
cn("cn_unemployment", "macro_china_urban_unemployment", [("UNEMPLOYMENT", "全国城镇调查失业率", "value", "%", "就业")], selector="全国城镇调查失业率")


def nbs(group, path, fields, *, kind="月度数据", period="2010-", basis="年初至今累计"):
    cn(group, "macro_china_nbs_nation", fields, provider="nbs", basis=basis,
       frequency={"月度数据": "月度", "季度数据": "季度", "年度数据": "年度"}[kind],
       params={"kind": kind, "path": path, "period": period})


nbs("nbs_fai", "固定资产投资 (不含农户) > 固定资产投资概况", [
    ("FAI_CUM_YOY", "固定资产投资累计同比（不含农户）", "固定资产投资额累计增长(%)", "%", "投资"),
    ("PRIVATE_FAI_CUM_YOY", "民间固定资产投资累计同比", "民间固定资产投资累计增长(%)", "%", "投资"),
])
nbs("nbs_fai_industry", "固定资产投资 (不含农户) > 按行业分固定资产投资增速（2018-）", [
    ("MANUFACTURING_FAI_CUM_YOY", "制造业投资累计同比", "制造业固定资产投资额累计增长(%)", "%", "投资"),
], period="2018-")
nbs("nbs_fai_growth", "固定资产投资 (不含农户) > 固定资产投资增速", [
    ("STATE_FAI_CUM_YOY", "国有及国有控股投资累计同比", "国有及国有控股固定资产投资额累计增长(%)", "%", "投资"),
    ("EQUIPMENT_FAI_CUM_YOY", "设备工器具购置投资累计同比", "设备工器具购置固定资产投资额累计增长(%)", "%", "投资"),
    ("NEW_FAI_CUM_YOY", "新建投资累计同比", "新建固定资产投资额累计增长(%)", "%", "投资"),
    ("EXPAND_FAI_CUM_YOY", "扩建投资累计同比", "扩建固定资产投资额累计增长(%)", "%", "投资"),
    ("RENOVATE_FAI_CUM_YOY", "改建投资累计同比", "改建固定资产投资额累计增长(%)", "%", "投资"),
])
nbs("nbs_property", "房地产 > 房地产开发投资情况", [
    ("PROPERTY_INVESTMENT_CUM", "房地产开发投资累计金额", "房地产投资__累计值(亿元)", "亿元", "房地产"),
    ("PROPERTY_INVESTMENT_CUM_YOY", "房地产开发投资累计同比", "房地产投资_累计增长(%)", "%", "房地产"),
])
nbs("nbs_profit", "工业 > 工业企业主要经济指标", [
    ("INDUSTRIAL_PROFIT_CUM", "规模以上工业企业利润累计金额", "利润总额_累计值(亿元)", "亿元", "生产"),
    ("INDUSTRIAL_PROFIT_CUM_YOY", "规模以上工业企业利润累计同比", "利润总额累计增长(%)", "%", "生产"),
])
nbs("nbs_gdp_nominal", "国民经济核算 > 国内生产总值 (现价)", [
    ("GDP_QUARTER", "现价 GDP（单季）", "国内生产总值当季值(亿元)", "亿元", "增长"),
], kind="季度数据", basis="当季，现价")
nbs("nbs_gdp_index", "国民经济核算 > 国内生产总值指数", [
    ("GDP_REAL_YOY_INDEX", "实际 GDP 同比指数（单季）", "国内生产总值指数(上年同期=100)当季值", "上年同期=100", "增长"),
], kind="季度数据", basis="当季，可比价格")
nbs("nbs_gdp_annual", "国民经济核算 > 国内生产总值", [
    ("GDP_ANNUAL", "现价 GDP（年度）", "国内生产总值(亿元)", "亿元", "增长"),
], kind="年度数据", basis="全年，现价")
nbs("nbs_unemployment_detail", "城镇调查失业率 > 城镇调查失业率", [
    ("UNEMPLOYMENT_CITIES31", "31个大城市城镇调查失业率", "31个大城市城镇调查失业率(%)", "%", "就业"),
    ("UNEMPLOYMENT_LOCAL", "本地户籍劳动力调查失业率", "全国城镇本地户籍劳动力失业率(%)", "%", "就业"),
    ("UNEMPLOYMENT_MIGRANT", "外来户籍劳动力调查失业率", "全国城镇外来户籍劳动力失业率(%)", "%", "就业"),
], period="2018-", basis="全国城镇月度比率；31城为合并调查指标")
for code, age in [("UNEMPLOYMENT_16_24", "16—24"), ("UNEMPLOYMENT_25_29", "25—29"), ("UNEMPLOYMENT_30_59", "30—59")]:
    SERIES.append(Series(code, f"{age}岁劳动力失业率（不含在校生）", "CN", "就业", "%", "月度", "nbs",
        "nbs_unemployment_age", f"全国城镇{age}岁劳动力失业率(%)", "不含在校生；2023年12月起新口径",
        params={"function": "macro_china_nbs_nation", "kind": "月度数据", "path": "城镇调查失业率 > 城镇调查失业率", "period": "2023-"},
        selector="from:2023-12-01"))
for suffix, label in [("NEW_MOM", "新建商品住宅销售价格指数(上月=100)"), ("NEW_YOY", "新建商品住宅销售价格指数(上年同月=100)"),
                      ("USED_MOM", "二手住宅销售价格指数(上月=100)"), ("USED_YOY", "二手住宅销售价格指数(上年同月=100)")]:
    SERIES.append(Series("HOUSE_" + suffix, label, "CN", "房地产", "指数（基期=100）", "月度", "nbs_city",
                         "nbs_house_" + suffix.lower(), label, "分城市；指数水平", params={
                             "function": "macro_china_nbs_region", "kind": "主要城市月度价格",
                             "path": "价格 > 70个大中城市住宅销售价格指数", "region": None, "indicator": label, "period": "2011-",
                         }))

for code, name, unit, freq, category, seasonal, basis in [
    ("CPIAUCSL", "居民消费价格指数", "1982—1984=100", "月度", "通胀", "季调", "指数水平"),
    ("CPILFESL", "核心居民消费价格指数", "1982—1984=100", "月度", "通胀", "季调", "剔除食品和能源"),
    ("CPIAUCNS", "居民消费价格指数（未季调，对比基数）", "1982—1984=100", "月度", "通胀", "未季调", "同比计算基数；与季调环比分开"),
    ("CPILFENS", "核心居民消费价格指数（未季调，对比基数）", "1982—1984=100", "月度", "通胀", "未季调", "剔除食品和能源；同比计算基数"),
    ("PCEPI", "个人消费支出价格指数", "2017=100", "月度", "通胀", "季调", "指数水平"),
    ("PCEPILFE", "核心个人消费支出价格指数", "2017=100", "月度", "通胀", "季调", "剔除食品和能源"),
    ("PAYEMS", "非农就业人数", "千人", "月度", "就业", "季调", "人数水平，并非新增人数"),
    ("UNRATE", "失业率", "%", "月度", "就业", "季调", "月度比率"),
    ("CIVPART", "劳动参与率", "%", "月度", "就业", "季调", "月度比率"),
    ("CES0500000003", "私人非农平均时薪", "美元/小时", "月度", "就业", "季调", "名义工资"),
    ("ICSA", "首次申请失业救济人数", "人", "周度", "就业", "季调", "截至周六的一周"),
    ("GDPC1", "实际 GDP", "十亿美元（2017年链式价格）", "季度", "增长", "季调", "折年水平"),
    ("GDPDEF", "GDP 平减指数", "2017=100", "季度", "增长", "季调", "指数水平"),
    ("INDPRO", "工业生产指数", "2017=100", "月度", "生产", "季调", "指数水平"),
    ("RSAFS", "零售与餐饮销售额", "百万美元", "月度", "消费", "季调", "名义月度金额"),
    ("PCEC96", "实际个人消费支出", "十亿美元（2017年链式价格）", "月度", "消费", "季调", "折年水平"),
    ("DSPIC96", "实际可支配个人收入", "十亿美元（2017年链式价格）", "月度", "消费", "季调", "折年水平"),
    ("PSAVERT", "个人储蓄率", "%", "月度", "消费", "季调", "可支配收入占比"),
    ("HOUST", "新屋开工", "千套", "月度", "房地产", "季调", "折年数量"),
    ("PERMIT", "新屋营建许可", "千套", "月度", "房地产", "季调", "折年数量"),
    ("HSN1F", "新建独栋住宅销售", "千套", "月度", "房地产", "季调", "折年数量"),
    ("MORTGAGE30US", "30年固定房贷利率", "%", "周度", "房地产", "未季调", "周度均值"),
    ("DFEDTARL", "联邦基金目标利率下限", "%", "日度", "利率", "未季调", "政策目标，包括非工作日"),
    ("DFEDTARU", "联邦基金目标利率上限", "%", "日度", "利率", "未季调", "政策目标，包括非工作日"),
    ("EFFR", "有效联邦基金利率", "%", "日度", "利率", "未季调", "工作日；缺值保留"),
    ("DGS2", "2年期国债收益率", "%", "日度", "利率", "未季调", "固定期限；缺值保留"),
    ("DGS10", "10年期国债收益率", "%", "日度", "利率", "未季调", "固定期限；缺值保留"),
    ("DGS30", "30年期国债收益率", "%", "日度", "利率", "未季调", "固定期限；缺值保留"),
    ("PPIFIS", "最终需求生产者价格指数 PPI", "2009年11月=100", "月度", "通胀", "季调", "最终需求；涵盖商品与服务"),
    ("PPIFID", "最终需求 PPI（未季调，对比基数）", "2009年11月=100", "月度", "通胀", "未季调", "PPI同比计算基数"),
    ("DGORDER", "耐用品新增订单", "百万美元", "月度", "生产", "季调", "名义订单金额；包含运输设备"),
    ("UMCSENT", "密歇根大学消费者信心指数", "1966年一季度=100", "月度", "消费", "未季调", "调查信心指数；FRED延迟一个月"),
]:
    SERIES.append(Series(code, name, "US", category, unit, freq, "fred", "fred_" + code, code, basis, seasonal))

for code, name, unit, basis in [
    ("FDI_CUM", "实际使用外资累计金额", "亿元人民币", "年初至今累计"),
    ("FDI_CUM_YOY", "实际使用外资累计同比", "%", "年初至今累计"),
    ("FDI_NEW_COMPANIES_CUM", "新设外商投资企业累计数量", "家", "年初至今累计"),
    ("FDI_MANUFACTURING_CUM", "制造业实际使用外资累计金额", "亿元人民币", "年初至今累计"),
    ("FDI_SERVICES_CUM", "服务业实际使用外资累计金额", "亿元人民币", "年初至今累计"),
    ("FDI_HIGHTECH_CUM", "高技术产业实际使用外资累计金额", "亿元人民币", "年初至今累计；与行业维度交叉，不可加总"),
]:
    SERIES.append(Series(code, name, "CN", "投资", unit, "月度", "mofcom", "mofcom", code, basis))
SERIES.append(Series("INFRASTRUCTURE_CUM_YOY", "基础设施投资累计同比", "CN", "投资", "%", "月度", "nbs_release", "nbs_release", "INFRASTRUCTURE_CUM_YOY", "年初至今累计；范围以当期发布稿附注为准"))
for code, name, unit, basis in [
    ("TSF_STOCK", "社会融资规模存量", "亿元", "月末"),
    ("TSF_STOCK_YOY", "社会融资规模存量同比", "%", "月末"),
    ("TSF_CUM", "社会融资规模累计增量", "亿元", "年初至今累计"),
    ("GOVERNMENT_BONDS_STOCK", "社融政府债券余额", "亿元", "月末"),
    ("GOVERNMENT_BONDS_CUM", "社融政府债券累计净融资", "亿元", "年初至今累计"),
    ("HOUSEHOLD_LOANS_CUM", "住户贷款累计增量", "亿元", "年初至今累计"),
    ("HOUSEHOLD_SHORT_LOANS_CUM", "住户短期贷款累计增量", "亿元", "年初至今累计"),
    ("HOUSEHOLD_LONG_LOANS_CUM", "住户中长期贷款累计增量", "亿元", "年初至今累计"),
    ("COMPANY_LOANS_CUM", "企事业单位贷款累计增量", "亿元", "年初至今累计"),
    ("COMPANY_SHORT_LOANS_CUM", "企事业单位短期贷款累计增量", "亿元", "年初至今累计"),
    ("COMPANY_LONG_LOANS_CUM", "企事业单位中长期贷款累计增量", "亿元", "年初至今累计"),
    ("COMPANY_BILLS_CUM", "企事业单位票据融资累计增量", "亿元", "年初至今累计"),
]:
    SERIES.append(Series(code, name, "CN", "金融", unit, "月度", "pbc", "pbc", code, basis))

for code, name, group in [("PMI_ISM_MANUFACTURING", "ISM制造业 PMI", "ism_manufacturing"), ("PMI_ISM_SERVICES", "ISM服务业 PMI", "ism_services")]:
    SERIES.append(Series(code, name, "US", "景气", "指数（50为荣枯线）", "月度", "ism", group, code,
                         "官方月度报告；部分组成项季调", "部分季调"))
SERIES.append(Series("DEFICIT_BUDGET_RATIO", "官方年度预算赤字率", "CN", "财政", "%", "年度", "gov_budget", "gov_budget",
                     "DEFICIT_BUDGET_RATIO", "年度预算安排，约数；不是月度收支差额或实际执行赤字率"))
GROUPS = {s.group: [entry for entry in SERIES if entry.group == s.group] for s in SERIES}
OFFICIAL_GROUPS = {"mofcom", "pbc", "nbs_release", "ism_manufacturing", "ism_services", "gov_budget"}
