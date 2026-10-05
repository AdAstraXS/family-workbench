"""Source-backed, reviewed explanations; teaching numbers never come from observations.

Each registered series needs an explicit entry. Shared statistical methods are reused
only within the same indicator family; the scope and interpretation remain specific.
"""
import json
from copy import deepcopy
from pathlib import Path

from .registry import SERIES


SPEC = {(s.country, s.code): s for s in SERIES}
_examples = json.loads(Path(__file__).with_name("guide_examples.json").read_text(encoding="utf-8"))
GUIDES = {tuple(k.split(":")): v for k, v in _examples["guides"].items()}
BASICS = _examples["basics"] + [
    ["存量、增量与少增", "存量是某个时点的余额，增量是一段时期的净变化。去年增加 10、今年增加 8，叫少增 2；今年仍在增加，不能说余额减少了 2。核销、汇率或口径变化也可能影响存量与增量的衔接。"],
    ["折年水平与折年增速", "折年水平把当月或当季的季调活动速度换算成全年尺度，并非已经实现的全年总量，也不是预测。季度环比折年增速则为（本季÷上季）的四次方减 1；月度为十二次方减 1。两者都不同于同比。"],
    ["可比口径与基数效应", "统计范围、分类或方法可能调整，官方增速有时使用重算的去年同期基数。去年异常低会抬高今年同比，去年异常高会压低同比；看变化要同时检查基数和当期表现。"],
    ["链式价格与不能加总", "链式数量指标用相邻时期的价格权重连接，帮助剔除价格变化。以链式价格表示的不同分项一般不能直接相加得到总量；参照年换算也不等于各年一直使用同一篮子。"],
]

SOURCES = {
    "fai": ["国家统计局：投资统计和计算方法", "https://www.stats.gov.cn/zs/tjws/zytjzbqs/qshgdzctz/202410/t20241029_1957205.html"],
    "cpi": ["国家统计局：CPI 编制方法", "https://www.stats.gov.cn/zs/tjws/zytjzbqs/jmxxggzs/202411/t20241127_1957589.html"],
    "gdp_cn": ["国家统计局：GDP 的核算方法", "https://www.stats.gov.cn/zs/tjws/zytjzbqs/gnsczz/202410/t20241025_1957169.html"],
    "industry_cn": ["国家统计局：工业生产增长速度的计算方法", "https://www.stats.gov.cn/zs/tjws/zytjzbqs/gysczzsd/202410/t20241025_1957170.html"],
    "profit": ["国家统计局：工业利润与统计范围附注", "https://www.stats.gov.cn/sj/zxfbhjd/202607/t20260727_1964194.html"],
    "retail_cn": ["国家统计局：社会消费品零售总额", "https://www.stats.gov.cn/zs/tjws/tjzb/202301/t20230101_1903707.html"],
    "house_cn": ["国家统计局：住宅价格基础数据来源", "https://www.stats.gov.cn/zs/tjws/zytjzbqs/zzxsjgzs/202501/t20250121_1958389.html"],
    "unemployment_cn": ["国家统计局：调查失业率", "https://www.stats.gov.cn/zs/tjws/tjzb/202301/t20230101_1903672.html"],
    "money": ["人民银行：狭义货币与广义货币", "https://www.pbc.gov.cn/rmyh/109339/2025080818580470423/index.html"],
    "tsf": ["人民银行：社融增量与统计附注", "https://www.pbc.gov.cn/diaochatongjisi/116219/116225/523c260b344c4f1390664430295064a9/index.html"],
    "fdi": ["商务部：外商投资统计调查制度", "https://wzs.mofcom.gov.cn/zcfb/art/2024/art_fed6e44d5890491e8f0041816e4bf490.html"],
    "fdi_amount": ["商务部：实际使用外资定义与统计公报", "https://images.mofcom.gov.cn/wzs/202310/20231010105622259.pdf"],
    "fiscal": ["财政部：财政收支口径", "https://gks.mof.gov.cn/tongjishuju/202601/t20260130_3982923.htm"],
    "cps": ["BLS：劳动力调查定义", "https://www.bls.gov/cps/definitions.htm"],
    "ces": ["BLS：工资单调查方法", "https://www.bls.gov/web/empsit/cesfaq.htm"],
    "cpi_us": ["BLS：消费价格指数计算", "https://www.bls.gov/opub/hom/cpi/calculation.htm"],
    "bea": ["BEA：国民账户方法手册", "https://www.bea.gov/resources/methodologies/nipa-handbook"],
    "pio": ["BEA：个人收入与支出定义", "https://www.bea.gov/news/pio-release-additional-information"],
    "gdp_us": ["BEA：认识 GDP", "https://www.bea.gov/resources/learning-center/what-to-know-gdp"],
    "ip_us": ["美联储：工业生产统计方法", "https://www.federalreserve.gov/releases/g17/About.htm"],
    "housing_us": ["Census：住宅建设与销售定义", "https://www.census.gov/construction/soc/definitions.html"],
    "housing_method": ["Census：住宅调查如何采集数据", "https://www.census.gov/construction/soc/methodology.html"],
    "retail_us": ["Census：零售统计定义", "https://www.census.gov/retail/definitions.html"],
    "claims": ["美国劳工部：失业保险申请定义", "https://www.dol.gov/agencies/eta/ui-modernization/use-plain-language/ui-lexicon-resource"],
    "mortgage": ["Freddie Mac：房贷调查方法", "https://www.freddiemac.com/research/insight/20221103-freddie-macs-newly-enhanced-mortgage-rate-survey"],
    "effr": ["纽约联储：有效联邦基金利率", "https://www.newyorkfed.org/markets/reference-rates/effr"],
    "treasury": ["美国财政部：国债收益率曲线方法", "https://home.treasury.gov/policy-issues/financing-the-government/interest-rate-statistics/treasury-yield-curve-methodology"],
}


def put(country, code, *, lead, scope, calculation, formula, example, method, meaning, pitfalls, related, sources, aliases="", kind="", together=""):
    spec = SPEC[(country, code)]
    GUIDES[(country, code)] = {"title": spec.name, "aliases": aliases + " " + code, "kind": kind,
        "lead": lead, "scope": scope, "calculation": calculation, "formula": formula, "example": example,
        "method": method, "meaning": meaning, "pitfalls": pitfalls, "related": related,
        "sources": [SOURCES[s] for s in sources], "together": together}


INVESTMENT_SCOPES = {
    "PRIVATE_FAI_CUM_YOY": "民间投资按控股和登记注册等统计规则识别民间投资主体；不等于只计算个体户或上市民营公司。",
    "MANUFACTURING_FAI_CUM_YOY": "制造业项目的建设和设备等投入，按行业分类归属；不等于制造业销售收入或工业产量。",
    "STATE_FAI_CUM_YOY": "由国有及国有控股单位完成的固定资产投资；不等于中央财政支出，也不涵盖所有政府开支。",
    "EQUIPMENT_FAI_CUM_YOY": "投资构成中的设备、工具、器具购置，不包含整个项目的全部施工和其他费用。",
    "NEW_FAI_CUM_YOY": "按建设性质归为新建的项目投入，不等于当期所有新开工项目的计划总金额。",
    "EXPAND_FAI_CUM_YOY": "按建设性质归为扩建的项目投入，体现扩大既有生产或服务能力的建设活动。",
    "RENOVATE_FAI_CUM_YOY": "按建设性质归为改建的项目投入，涉及原有设施改造；不能把它只理解成住宅装修。",
    "INFRASTRUCTURE_CUM_YOY": "基础设施项目投入。包含哪些行业、是否包含电力等领域必须以该统计期发布稿附注为准。不同基础设施口径不可混接。",
    "INFRASTRUCTURE_EX_UTILITIES_CUM_YOY": "基础设施投资中不包含电力、热力、燃气及水生产和供应业的累计同比。单独保存官方旧口径历史，不能与包含这些行业的指标拼接或直接相减。",
    "PROPERTY_INVESTMENT_CUM_YOY": "房地产开发企业的开发投资完成额，包括开发建设相关投入；不等于房屋销售额或购房者支付的房款。",
}
for code, scope in INVESTMENT_SCOPES.items():
    guide = deepcopy(GUIDES[("CN", "FAI_CUM_YOY")])
    guide.update(title=SPEC[("CN", code)].name, scope=scope,
                 lead=f"看{SPEC[('CN', code)].name.replace('累计同比', '')}从年初到现在，比去年同期投入多了还是少了。",
                 related=["CN:FAI_CUM_YOY", "CN:FAI_CUM"], sources=[SOURCES["fai"]], aliases=code + " 投资 FAI")
    if code.startswith("INFRASTRUCTURE_"):
        guide["pitfalls"] = ["先核对当期基础设施范围附注。", "不能将多个行业增速简单平均得出基建增速。"] + guide["pitfalls"][:2]
    GUIDES[("CN", code)] = guide

for code, scope in [
    ("FAI_CUM", "月度不含农户口径，包含计划总投资 500 万元及以上建设项目和房地产开发投资。"),
    ("PROPERTY_INVESTMENT_CUM", "房地产开发企业完成的开发建设投入，不是房屋销售收入。"),
]:
    put("CN", code, lead="统计从年初到本月累计完成了多少建设投资。", scope=scope,
        calculation="将对应范围内项目的已完成投资额汇总。", formula="累计投资额 = 对应范围内 1—本月投资完成额合计",
        example="若 1—6 月累计 100 亿元，1—7 月累计 115 亿元，在范围与版本一致时，7 月单月为 15 亿元，不能把两期累计相加为 215 亿元。",
        method="项目或开发企业按月报送完成额，统计部门审核汇总；按现价记录，包含价格变化。",
        meaning="反映建设投入规模。金额变大可能同时受数量和价格影响，不能直接当成实际产出增长。",
        pitfalls=["累计值不能逐月相加。", "完成投资不等于计划投资。", "跨期相减先确认口径、单位与修订版本一致。"],
        related=["CN:FAI_CUM_YOY", "CN:PROPERTY_INVESTMENT_CUM_YOY"], sources=["fai"], aliases="固投 投资 FAI", kind="累计金额")

SOURCES["pmi"] = ["国家统计局：PMI 统计范围与计算方法", "https://www.stats.gov.cn/sj/zxfbhjd/202609/t20260930_1965449.html"]
SOURCES["trade"] = ["海关总署：月度进出口统计", "https://english.customs.gov.cn/statics/report/monthly.html"]
SOURCES["ppi"] = ["国家统计局：PPI 编制方法", "https://www.stats.gov.cn/zs/tjws/tjzb/202301/t20230101_1903637.html"]
SOURCES["policy"] = ["美联储：政策目标利率区间", "https://www.federalreserve.gov/economy-at-a-glance-policy-rate.htm"]

put("CN", "CPI_MOM", lead="看居民通常购买的商品和服务，本月比上月总体贵了还是便宜了。",
    scope="覆盖居民消费的一篮子商品与服务，按消费支出结构赋予权重，不是某一商品的价格。",
    calculation="比较本月与上月的加权消费价格水平。", formula="环比 =（本月价格指数 ÷ 上月价格指数 − 1）× 100%",
    example="假设加权价格水平由 100 变为 100.5，环比为 0.5%；这不是同比。",
    method="统计部门在调查网点采价，按分类权重编制指数。春节、天气等季节因素会影响未季调环比。",
    meaning="观察近期价格变化；配合同比区分当月变化和去年基数影响。", pitfalls=["不是每个家庭都涨价相同。", "环比不能简单乘 12 当作年通胀。", "同比回升时环比仍可能为负。"],
    related=["CN:CPI_YOY", "CN:PPI_YOY"], sources=["cpi"], aliases="CPI 物价 消费价格", kind="环比增速")
put("CN", "PPI_YOY", lead="看工业企业卖出产品的出厂价格，比去年同月总体涨了多少。",
    scope="工业生产者出厂环节的产品价格，不是居民购物价格，也不是股票价格。",
    calculation="汇总代表产品价格变化并加权，比较去年同月。", formula="同比 =（本月出厂价格指数 ÷ 去年同月指数 − 1）× 100%",
    example="假设可比价格指数由 100 到 97，同比为 −3%，表示价格总体下降。",
    method="统计部门对工业企业代表产品采价，按工业销售结构权重编制。产品和权重会按统计制度调整。",
    meaning="反映工业品价格环境。与产量、成本一起看，才能判断收入和利润压力。",
    pitfalls=["PPI 下降不是工业产量下降 3%。", "不会原样传导到 CPI。", "出厂价格不等于原材料购进价格。"], related=["CN:CPI_YOY", "CN:INDUSTRIAL_PROFIT_CUM_YOY"],
    sources=["ppi"], aliases="PPI 出厂价格 工业品 通缩", kind="同比增速")
put("CN", "PMI_NONMANUFACTURING", lead="看服务业和建筑业的商务活动，比上月总体扩张还是收缩。",
    scope="非制造业采购经理调查中的商务活动分类指数，包括服务业和建筑业；不是制造业五项加权公式。",
    calculation="采用扩散指数，回答增加计全权、持平计半权，按调查权重汇总并季调。",
    formula="商务活动扩散指数 = 增加的加权比例 + 持平的加权比例 × 0.5",
    example="假设增加占 40%、持平占 30%、减少占 30%，指数为 55，表示活动总体扩张；不是产出增长 5%。",
    method="分行业对采购经理进行月度问卷调查，汇总相对上月的活动方向，消除季节因素。",
    meaning="50 为临界点；从 54 降到 51 表示仍扩张但扩张面减弱。",
    pitfalls=["不是非制造业的产值增速。", "不是制造业 PMI 的五项复合公式。", "服务业与建筑业内部可能方向不同。"],
    related=["CN:PMI_MANUFACTURING", "CN:RETAIL_YOY"], sources=["pmi"], aliases="PMI 服务业 建筑业 景气", kind="扩散指数")

for code, growth in [("RETAIL", False), ("RETAIL_YOY", True)]:
    put("CN", code, lead="看商品零售和餐饮消费的销售规模" + ("比去年同期增长多少。" if growth else "有多大。"),
        scope="售给个人和社会集团用于非生产、非经营用途的实物商品及餐饮收入。多数其他服务消费不在此项中。",
        calculation="按调查范围汇总名义销售额。增速使用官方可比基数；1—2 月可能合并发布。",
        formula="同比 =（本期销售额 ÷ 去年同期可比销售额 − 1）× 100%" if growth else "零售总额 = 对应范围商品零售额 + 餐饮收入",
        example="假设本期 110 亿元、去年可比同期 100 亿元，同比 10%；这 10% 同时包含价格与数量变化。",
        method="限额以上单位调查与限额以下抽样调查等结合，统计部门审核汇总。以来源标明的当月或合并期为准。",
        meaning="观察商品消费和餐饮需求，需配合价格因素判断实际购买量。",
        pitfalls=["不等于全部居民消费支出。", "名义增长不等于实际增长。", "1—2 月合并值不能当作 2 月单月。"],
        related=["CN:CPI_YOY", "CN:RETAIL" if growth else "CN:RETAIL_YOY"], sources=["retail_cn"], aliases="社零 消费 零售", kind="同比增速" if growth else "金额")
put("CN", "INDUSTRY_YOY", lead="看规模以上工业企业创造的实际增加值，比去年同期增长多少。",
    scope="年主营业务收入 2000 万元及以上工业企业；增加值是生产新创造的价值，不是销售收入。",
    calculation="按可比价格测算工业增加值增长，剔除价格变动影响。", formula="同比 =（本期可比价增加值 ÷ 去年同期可比价增加值 − 1）× 100%",
    example="假设可比价增加值从 100 到 105，同比为 5%；不能据此说名义销售收入增长 5%。",
    method="依据企业生产等资料测算工业增加值速度，对样本范围变化按可比口径处理，1—2 月合并发布。",
    meaning="反映工业生产的实际增长，配合 PPI 与利润观察量、价、盈利。",
    pitfalls=["不是所有工业企业。", "不是营业收入增速。", "不能用公开现价金额简单复算官方实际增速。"],
    related=["CN:PPI_YOY", "CN:INDUSTRIAL_PROFIT_CUM_YOY"], sources=["industry_cn"], aliases="工业 规上工业 增加值", kind="实际同比增速")
for code, growth in [("INDUSTRIAL_PROFIT_CUM", False), ("INDUSTRIAL_PROFIT_CUM_YOY", True)]:
    put("CN", code, lead="看规模以上工业企业从年初到本月的利润" + ("比去年同期多了还是少了。" if growth else "总规模。"),
        scope="规模以上工业企业的利润总额，包括经营及其他损益；不是上市公司的归母净利润。",
        calculation="汇总累计利润总额；同比按官方处理企业范围变化后的可比基数计算。",
        formula="同比 =（本年累计利润 ÷ 去年同期可比累计利润 − 1）× 100%" if growth else "累计利润总额 = 范围内企业本年累计利润总额合计",
        example="假设去年可比累计利润 100、本年 110，同比为 10%；不是利润率 10%。",
        method="企业报送财务资料，统计部门审核。企业进退库和清理重复等使增速与简单用旧公报金额复算的结果可能不同。",
        meaning="观察工业企业盈利环境；利润会同时受销量、售价、成本和其他损益影响。",
        pitfalls=["累计不能逐月相加。", "利润同比不是利润率。", "去年基数为零或负数时，不能机械套普通同比公式，遵循官方说明。"],
        related=["CN:INDUSTRY_YOY", "CN:PPI_YOY"], sources=["profit"], aliases="工业利润 盈利 利润总额", kind="累计同比" if growth else "累计金额")
for code, annual in [("GDP_QUARTER", False), ("GDP_ANNUAL", True)]:
    put("CN", code, lead="看国内在" + ("一年" if annual else "一个季度") + "生产的最终商品和服务，按当期价格值多少钱。",
        scope="国内常住生产单位创造的增加值，避免把中间投入反复计算。按现价核算，是流量。",
        calculation="通过生产、收入等核算资料汇总增加值，并进行核算衔接。", formula="生产法 GDP = 各行业增加值合计",
        example="假设面粉卖 40、面包卖 100，不能把两者全额加成 140；面包生产环节增加值须扣除面粉等中间投入。",
        method="统计部门结合调查、行政资料和核算规则编制，后续核实或经济普查可能修订。",
        meaning="衡量经济活动的名义规模，价格上涨也能抬高现价 GDP。",
        pitfalls=["不是居民收入或财富余额。", "现价增长不等于实际增长。", "本指标为全年金额。" if annual else "本指标为单季金额，不能当作年初累计。"],
        related=["CN:GDP_REAL_YOY_INDEX", "CN:GDP_QUARTER" if annual else "CN:GDP_ANNUAL"], sources=["gdp_cn"], aliases="GDP 国内生产总值 名义 现价", kind="年度金额" if annual else "单季金额")
put("CN", "GDP_REAL_YOY_INDEX", lead="看单个季度剔除价格影响后的 GDP，比去年同季增长多少。",
    scope="国内生产总值的实际数量变化，以去年同季度为 100，不是现价金额。",
    calculation="官方按可比价格核算实际增长，并以去年同期为 100 表示。", formula="实际同比增速（%）= 同比指数 − 100",
    example="指数 104.5 对应实际同比增长 4.5%；指数不是增长 104.5%。",
    method="统计部门对行业增加值使用价格缩减或数量外推等方法，衔接实际 GDP 增长。",
    meaning="观察经济实际扩张或收缩。它比较去年同季，不是本季相对上季。",
    pitfalls=["不要把指数水平当增长率。", "不是环比或环比折年增速。", "不能直接用现价 GDP 除 CPI 得出精确实际 GDP。"],
    related=["CN:GDP_QUARTER", "CN:GDP_ANNUAL"], sources=["gdp_cn"], aliases="GDP 实际增长 可比价", kind="同比指数")
for code in ["HOUSE_NEW_MOM", "HOUSE_NEW_YOY", "HOUSE_USED_MOM", "HOUSE_USED_YOY"]:
    new, mom = "NEW" in code, "MOM" in code
    put("CN", code, lead=f"看所选城市的{'新建商品' if new else '二手'}住宅价格，比{'上月' if mom else '去年同月'}总体涨跌多少。",
        scope="70 个大中城市分别编制的住宅销售价格指数。新房与二手房独立统计，不是全国房价均值。",
        calculation="在住宅分类基础上编制价格指数，将比较期设为 100。", formula="对应价格涨跌幅（%）= 指数 − 100",
        example="指数 99.8 表示较比较期下降 0.2%；102 表示上涨 2%。这不是每套房的涨跌。",
        method="新房主要使用网签资料，二手房结合调查等资料，按住宅分类与权重编制，基期与方法调整以当期附注为准。",
        meaning="观察该城市该类住宅价格方向，不能据此给一套具体房屋估价。",
        pitfalls=["比较期是上月。" if mom else "比较期是去年同月。", "城市之间不能简单平均得到全国指数。", "不是平均成交总价；住房结构和质量需按统计方法处理。"],
        related=["CN:HOUSE_NEW_MOM", "CN:HOUSE_NEW_YOY", "CN:HOUSE_USED_MOM", "CN:HOUSE_USED_YOY"], sources=["house_cn"], aliases="房价 70城 房地产 新房 二手房", kind="环比指数" if mom else "同比指数")

put("CN", "UNEMPLOYMENT", lead="看城镇劳动力中，符合失业条件的人占多少。",
    scope="城镇调查劳动力中的失业人口；失业识别涉及没有工作、求职与可工作等调查条件，不是全体人口。",
    calculation="用失业人数除以就业与失业人数之和。", formula="调查失业率 = 失业人口 ÷ 劳动力人口 × 100%",
    example="假设就业 95 人、失业 5 人，失业率 5%；未参与劳动市场的人不在这个分母里。",
    method="统计部门通过住户劳动力抽样调查，按统一规则识别就业、失业和非劳动力人口。",
    meaning="反映劳动力市场压力，劳动参与变化也能改变失业率。",
    pitfalls=["不是没有工作的人占全体人口比例。", "调查失业率不是登记失业率。", "失业率下降不一定都来自就业增加。"],
    related=["CN:PMI_MANUFACTURING", "CN:RETAIL_YOY"], sources=["unemployment_cn"], aliases="就业 失业 劳动力", kind="比例")
for code, growth in [("M2", False), ("M2_YOY", True)]:
    put("CN", code, lead="看现金和较广范围存款构成的货币余额" + ("比去年同月增长多少。" if growth else "有多大。"),
        scope="广义货币，包括流通现金和按央行现行口径计入的存款等。M1 统计口径调整等须结合官方衔接说明。",
        calculation="在月末汇总纳入范围的货币项目；同比比较去年同月末的可比余额。",
        formula="M2 同比 =（本月末余额 ÷ 去年同月末可比余额 − 1）× 100%" if growth else "M2 余额 = 按央行口径计入的现金与存款等合计",
        example="假设去年月末余额 100、今年 108，同比为 8%；不是这个月新印了 8 的纸币。",
        method="央行依据金融机构报表等编制货币统计，范围和分类变化应查官方说明。",
        meaning="反映广义货币规模，不能单独据此推断通胀或资产价格必然上涨。",
        pitfalls=["余额不能逐月相加。", "M2 不是央行印钞量。", "M2、贷款与社融有重叠，不能加成总资金量。"],
        related=["CN:M2" if growth else "CN:M2_YOY", "CN:TSF_STOCK", "CN:LOANS_NEW"], sources=["money"], aliases="货币 M2 广义货币 流动性", kind="余额同比" if growth else "月末余额")
put("CN", "LOANS_NEW", lead="看金融机构人民币贷款余额在本月净增加多少。",
    scope="金融机构人民币贷款统计，机构、借款主体范围以央行报表为准；与社融中的对实体经济贷款口径不完全相同。",
    calculation="贷款净增加受发放、偿还及统计调整等影响，不是当月发放贷款的毛额。", formula="本月新增贷款 ≈ 本月末余额 − 上月末余额（另核对统计调整）",
    example="假设发放 20、偿还 12，在无其他调整时净增 8；不是只发放了 8。",
    method="央行汇总金融机构贷款报表，使用官方公布的当月新增值。",
    meaning="观察信贷供给和需求，配合住户、企业与期限结构判断融资去向。",
    pitfalls=["同比少增不等于余额下降。", "不是贷款发放毛额。", "不能与社融贷款分项直接重复相加。"],
    related=["CN:HOUSEHOLD_LOANS_CUM", "CN:COMPANY_LOANS_CUM", "CN:TSF_CUM"], sources=["tsf"], aliases="信贷 新增贷款 金融", kind="单月净增量")
for code, export in [("EXPORTS", True), ("IMPORTS", False)]:
    put("CN", code, lead=f"看本月货物{'出口' if export else '进口'}的美元计价规模。",
        scope="海关货物贸易统计，不含服务贸易。当前指标单位为亿美元，不是人民币金额。",
        calculation="按海关统计范围汇总申报货物价值，按来源美元计价数据展示。", formula="月度金额 = 该月纳入统计的货物贸易价值合计",
        example="假设出口 120 亿美元、进口 100 亿美元，同口径贸易差额为 20 亿美元；不可把人民币与美元金额相减。",
        method="海关依据报关等资料编制统计，公布值可能在核实后修订。",
        meaning="观察对外货物贸易规模；金额变化同时可能来自数量、价格和汇率。",
        pitfalls=["金额上涨不等于货物数量同幅上涨。", "不是货物与服务全部贸易。", "人民币和美元口径增速可能不同。"],
        related=["CN:IMPORTS" if export else "CN:EXPORTS", "CN:INDUSTRY_YOY"], sources=["trade"], aliases="外贸 海关 出口 进口", kind="当月美元金额")
put("CN", "FISCAL_REVENUE_CUM", lead="看年初至本月的一般公共预算收入规模。",
    scope="按当期财政发布口径，主要包括税收与非税收入，涵盖中央和地方一般公共预算收入。政府性基金等另有预算体系。",
    calculation="汇总本年已实现的一般公共预算收入。", formula="累计收入 = 1—本月对应预算收入合计",
    example="假设 1—6 月收入 100、1—7 月 112，同范围同版本下 7 月为 12；不是 212。",
    method="财政部门汇总预算执行资料。历史表头、预算制度或范围变化需要核对当期说明。",
    meaning="反映财政收入能力，需与支出、税种和退税等政策因素一起看。",
    pitfalls=["不是政府所有收入。", "不能把土地出让收入自动并入一般公共预算。", "累计金额不能逐月相加。"],
    related=["CN:GDP_QUARTER", "CN:RETAIL_YOY"], sources=["fiscal"], aliases="财政 税收 一般公共预算", kind="累计金额")

FDI_SCOPES = {
    "FDI_CUM": "实际使用的外商直接投资资金，按来源人民币计价；不是签约或计划金额。",
    "FDI_CUM_YOY": "实际使用外资累计金额的同比变化，官方可能为保持可比性调整同期范围。",
    "FDI_NEW_COMPANIES_CUM": "本年新设立的外商投资企业数量，单位是家；不反映每家企业投资规模。",
    "FDI_MANUFACTURING_CUM": "归属制造业的实际使用外资，不是制造业固定资产投资的全部资金。",
    "FDI_SERVICES_CUM": "归属服务业的实际使用外资，与制造业是行业划分。",
    "FDI_HIGHTECH_CUM": "归属高技术产业的实际使用外资，与制造业、服务业维度交叉，不能加总。",
}
for code, scope in FDI_SCOPES.items():
    growth, count = code.endswith("YOY"), code == "FDI_NEW_COMPANIES_CUM"
    put("CN", code, lead="看外国投资者在国内的直接投资" + ("新设企业数量。" if count else "实际到位情况。"), scope=scope,
        calculation="按商务部外商投资统计制度汇总年初至本月到位金额或新设企业数；同比使用同期可比口径。",
        formula="累计同比 =（本年累计金额 ÷ 去年同期可比金额 − 1）× 100%" if growth else "累计值 = 本年 1—本月对应范围的金额或数量合计",
        example=("假设 1—6 月新设 100 家、1—7 月新设 120 家，同口径下 7 月新设 20 家；不是把累计数相加得到 220 家。" if count else
                 "假设制造业到资 60、服务业到资 40，其中高技术产业到资 30，高技术与行业划分可能重叠，不能相加得到 130。" if code == "FDI_HIGHTECH_CUM" else
                 "假设累计外资从去年同期 100 到今年 90，同比为 −10%；累计额仍为正，不代表所有外资撤离。"),
        method="商务部依据外商投资信息报告等资料编制。金融领域是否纳入、同比基数如何处理，以当期发布稿附注为准。",
        meaning="观察外商直接投资活动，企业数量、实际到资与行业分布反映不同维度。",
        pitfalls=["不是境外证券资金流入。", "新增企业数量不能替代到资金额。", "高技术产业与制造业、服务业交叉，不可相加。", "人民币金额与历史美元金额不能混接。"],
        related=["CN:FDI_CUM", "CN:FDI_CUM_YOY", "CN:FDI_NEW_COMPANIES_CUM"], sources=["fdi", "fdi_amount"], aliases="FDI 外商直接投资 外资 到资", kind="累计同比" if growth else "累计数量" if count else "累计金额")

TSF_SCOPES = {
    "TSF_STOCK": "实体经济从金融体系获得资金形成的期末融资余额，含贷款、债券等多种渠道。",
    "TSF_STOCK_YOY": "社融存量比去年同月末的变化，不是本月增量同比。",
    "TSF_CUM": "从年初至本月实体经济通过社融各渠道获得的融资净增量。",
    "GOVERNMENT_BONDS_STOCK": "社融中的政府债券期末余额，是社融存量的一部分。",
    "GOVERNMENT_BONDS_CUM": "社融中的政府债券累计净融资，发行与到期偿还等共同影响结果；不是发行毛额。",
    "HOUSEHOLD_LOANS_CUM": "金融统计中的住户人民币贷款累计净增量，不等于只有住房贷款。",
    "HOUSEHOLD_SHORT_LOANS_CUM": "住户短期人民币贷款累计净增量，包含相应期限范围内的消费与经营等贷款。",
    "HOUSEHOLD_LONG_LOANS_CUM": "住户中长期人民币贷款累计净增量，不等于全部都是住房按揭。",
    "COMPANY_LOANS_CUM": "企事业单位人民币贷款累计净增量，覆盖对应借款主体的多个期限和工具。",
    "COMPANY_SHORT_LOANS_CUM": "企事业单位短期人民币贷款累计净增量，通常用于较短期限融资；不能直接视作固定资产投资。",
    "COMPANY_LONG_LOANS_CUM": "企事业单位中长期人民币贷款累计净增量，不能直接视作当期已完成的建设投资。",
    "COMPANY_BILLS_CUM": "企事业单位票据融资的人民币贷款累计净增量，是贷款融资的一种形式。",
}
for code, scope in TSF_SCOPES.items():
    stock, growth = "STOCK" in code, code.endswith("YOY")
    put("CN", code, lead="看" + ("融资余额规模及其变化。" if stock else "年初以来净增加或净减少了多少融资。"), scope=scope,
        calculation="使用央行对应统计表的余额或累计净增量；核销、汇率、范围变更等会影响增量与余额的衔接。",
        formula="余额同比 =（本月末余额 ÷ 去年同月末可比余额 − 1）× 100%" if growth else "期末余额 = 按统计制度汇总的未偿融资余额" if stock else "累计净增量 = 1—本月对应渠道的净增量合计",
        example=("假设去年同月末余额 100、今年 108，存量同比为 8%；108 是月末余额，不能当作本月新增 108。" if stock else
                 "假设去年累计净增 10、今年净增 8，叫同比少增 2，余额仍可能增加 8；若净增为 −2，则该期为净减少。"),
        method="央行汇总金融机构及相关市场资料编制，社融与金融机构人民币贷款的统计主体范围并不完全一致。",
        meaning="观察融资扩张力度及结构。存量看融资规模，净增量看当期融资活动。",
        pitfalls=["存量不能逐月相加。", "少增不等于负增长。", "社融总量包含政府债券等分项，不能再与分项重复相加。", "贷款分项与社融中的贷款口径不能混同。"],
        related=(["CN:HOUSEHOLD_LOANS_CUM", "CN:HOUSEHOLD_SHORT_LOANS_CUM", "CN:HOUSEHOLD_LONG_LOANS_CUM", "CN:LOANS_NEW"] if code.startswith("HOUSEHOLD") else
                 ["CN:COMPANY_LOANS_CUM", "CN:COMPANY_SHORT_LOANS_CUM", "CN:COMPANY_LONG_LOANS_CUM", "CN:COMPANY_BILLS_CUM", "CN:LOANS_NEW"] if code.startswith("COMPANY") else
                 ["CN:GOVERNMENT_BONDS_STOCK", "CN:GOVERNMENT_BONDS_CUM", "CN:TSF_CUM"] if code.startswith("GOVERNMENT") else
                 ["CN:TSF_STOCK", "CN:TSF_STOCK_YOY", "CN:TSF_CUM", "CN:LOANS_NEW"]),
        sources=["tsf"], aliases="住户 贷款 信贷" if code.startswith("HOUSEHOLD") else "企业 贷款 信贷 票据" if code.startswith("COMPANY") else "社融 TSF 政府债券" if code.startswith("GOVERNMENT") else "社融 TSF", kind="余额同比" if growth else "月末余额" if stock else "累计净增量")

for code, pce, core in [("CPIAUCSL", False, False), ("CPILFESL", False, True), ("PCEPI", True, False), ("PCEPILFE", True, True)]:
    put("US", code, lead="看美国" + ("个人消费支出" if pce else "居民消费") + "价格水平" + ("剔除食品和能源后的变化。" if core else "的变化。"),
        scope=("PCE 包含居民直接购买及他人代其支付的消费，例如部分医疗支出。" if pce else "CPI 关注城市消费者购买的一篮子商品与服务。") + ("核心指数剔除食品和能源，但不表示这些支出不重要。" if core else "食品与能源包含在总指数内。"),
        calculation="按分类价格及消费权重汇总。" + ("PCE 使用链式 Fisher 方法，权重随消费结构变化。" if pce else "CPI 按 BLS 指数方法编制，各层级采用相应公式。"),
        formula="同比 =（本月指数 ÷ 去年同月指数 − 1）× 100%；环比 =（本月指数 ÷ 上月指数 − 1）× 100%",
        example="假设指数从去年同月 120 到 123，同比为 2.5%；123 本身不是通胀率 123%。",
        method="BEA 结合消费支出及价格资料编制，FRED 提供此季调序列。" if pce else "BLS 在样本商品、服务与住房等项目采价、加权，FRED 提供此季调序列。",
        meaning="用于观察价格趋势。指数基期只是尺度，不能用不同基期指数的数值大小判断谁更贵。",
        pitfalls=["原始指数水平不是通胀率；页面主位为计算后的同比与环比。", "核心不是完全排除所有价格波动。", "CPI 与 PCE 的范围、权重和公式不同。", "CPI 同比使用未季调指数，环比使用季调指数；PCE 按季调序列比较。"],
        related=["US:CPIAUCSL", "US:CPILFESL", "US:PCEPI", "US:PCEPILFE"], sources=["bea", "pio"] if pce else ["cpi_us"], aliases="PCE 消费 通胀 核心" if pce else "CPI 物价 通胀 核心", kind="季调价格指数")
for code, participation in [("UNRATE", False), ("CIVPART", True)]:
    put("US", code, lead="看美国" + ("符合调查范围的人口中，有多少进入劳动市场。" if participation else "劳动力中有多少处于失业状态。"),
        scope="CPS 调查范围为 16 岁及以上非机构化平民人口。劳动力是就业与失业人口之和；失业有求职、可工作等识别条件。",
        calculation="劳动参与率将劳动力除以调查人口，失业率将失业人口除以劳动力。",
        formula="劳动参与率 = 劳动力 ÷ 调查范围人口 × 100%" if participation else "失业率 = 失业人口 ÷ 劳动力 × 100%",
        example="假设调查人口 100、就业 57、失业 3，参与率为 60%，失业率为 3÷60=5%。",
        method="BLS 依据家庭抽样调查分类就业状态、加权并季调，与企业工资单调查不同。",
        meaning="观察劳动市场参与程度。" if participation else "观察劳动市场压力；求职者退出劳动力也可能使失业率下降。",
        pitfalls=["失业率分母不是全体人口。", "不求职者通常不属于失业人口。", "不能直接用非农工资单岗位数代入家庭调查公式。"],
        related=["US:UNRATE" if participation else "US:CIVPART", "US:PAYEMS"], sources=["cps"], aliases="失业 就业 参与 劳动力 CPS", kind="季调比例")
put("US", "CES0500000003", lead="看美国私人非农员工，每个受薪工时平均获得多少名义工资。",
    scope="私人非农全体员工，按工资单和支付工时统计，不是工资中位数，也不包括所有福利成本。",
    calculation="将工资总额除以对应支付工时，并按调查方法汇总、季调。", formula="平均时薪 = 对应工资总额 ÷ 对应支付工时",
    example="假设支付工资 1000 美元、对应 50 小时，平均 20 美元/小时；并不代表每个人都拿 20。",
    method="BLS 企业工资单调查收集工资与工时资料，员工和行业构成改变也会影响均值。",
    meaning="观察工资压力；判断购买力还需考虑物价变化。",
    pitfalls=["不是实际工资，未扣通胀。", "不是中位数或每名员工加薪幅度。", "低工资岗位增减会影响总体平均值。"], related=["US:PAYEMS", "US:CPIAUCSL"], sources=["ces"], aliases="时薪 工资 薪资 AHE", kind="名义平均工资")
put("US", "ICSA", lead="看一周内有多少人首次提交失业保险申请。",
    scope="失业保险制度中的首次申请，不是全部失业人口，也不是已经批准领取的总人数。",
    calculation="汇总首次申请并季调；周度日期表示截至周六的一周。", formula="周度首次申请 = 该周首次申请记录按统计规则汇总",
    example="假设首次申请 20 万，并不表示当周净减少了 20 万个岗位，也不包括所有没有资格申请的人。",
    method="美国劳工部汇总各州行政申请资料并进行季调，初值可能修订。",
    meaning="帮助较快观察就业市场压力。单周波动较大，应结合多周趋势。",
    pitfalls=["申请不是批准。", "首次申请与持续领取不同。", "不是非农岗位净变化。"], related=["US:UNRATE", "US:PAYEMS"], sources=["claims"], aliases="初请 失业金 失业救济 ICSA", kind="季调周度人数")
put("US", "GDPC1", lead="看美国剔除价格变化后的经济产出水平，以当季活动速度折成年尺度。",
    scope="国内最终商品与服务的实际产出，单位为十亿美元、2017 年链式价格；是季度季调折年水平。",
    calculation="BEA 用数量与价格资料编制链式实际 GDP，将季度季调水平折年。", formula="折年水平 = 季调季度水平 × 4；环比折年增速 =（本季 ÷ 上季）⁴ − 1",
    example="假设季调季度产出 6000，折年水平 24000；不代表本季实际生产了全年 24000。",
    method="BEA 编制国民账户并使用链式数量方法，初报、二报、三报及年度修订可能改变历史值。",
    meaning="观察实际经济活动；页面主位为同比、季度环比与季度环比折年，原始水平放在次要位置。三种增速不能混称。",
    pitfalls=["不是 GDP 增长率。", "不是本季度金额或全年已实现总量。", "链式价格分项通常不可简单相加。"], related=["US:GDPDEF", "US:PCEC96"], sources=["bea", "gdp_us"], aliases="GDP 实际 增长 经济 折年", kind="季调折年实际水平")
put("US", "GDPDEF", lead="看美国国内生产的最终商品与服务的总体价格变化。",
    scope="覆盖 GDP 中消费、投资、政府与出口等国内生产内容，进口不是国内生产；范围不同于 CPI。",
    calculation="以名义 GDP 与链式实际 GDP 的比值编制隐含价格指数。", formula="GDP 隐含平减指数 = 名义 GDP ÷ 实际 GDP × 100（按参照年尺度）",
    example="假设同尺度名义 GDP 120、实际 GDP 100，指数 120；同比仍要再与去年同季指数比较。",
    method="BEA 国民账户编制，组成随经济产出结构变化，季度季调序列经 FRED 提供。",
    meaning="观察国内产出的总体价格，与居民消费价格反映不同范围。",
    pitfalls=["指数水平不是通胀率。", "不等于 CPI。", "不能随意把名义金额与不同参照年的实际金额相除。"], related=["US:GDPC1", "US:CPIAUCSL", "US:PCEPI"], sources=["bea"], aliases="GDP 平减 价格 通胀", kind="季调价格指数")
put("US", "INDPRO", lead="看美国工业部门的实际生产数量变化。",
    scope="制造业、采矿业及电力和燃气公用事业，不含全部服务业。2017 年为 100。",
    calculation="综合产品产量及投入等资料，按行业权重编制数量指数。", formula="指数 = 相对参照期的工业生产数量水平 × 100（按官方加权方法）",
    example="指数 105 表示高于参照年尺度；本月同比仍须与去年同月指数比较。",
    method="美联储依据实际产量、工时等资料估算工业产出，采用行业权重和链式方法，季调并修订。",
    meaning="观察工业实际活动，不包含产品售价上涨带来的名义销售增长。",
    pitfalls=["不是销售收入指数。", "不是全经济 GDP。", "基期指数 105 不表示同比 5%。"], related=["US:GDPC1", "US:PAYEMS"], sources=["ip_us"], aliases="工业 生产 IP INDPRO", kind="季调数量指数")
put("US", "RSAFS", lead="看美国零售与餐饮服务企业一个月卖了多少钱。",
    scope="零售与餐饮销售，名义金额；不包含全部服务消费，汽车等大项可能影响总量。",
    calculation="按企业调查汇总销售额，季调但不剔除价格变化。", formula="月度销售额 = 调查范围内零售与餐饮销售金额合计",
    example="假设售价上涨 5%、销量不变，名义销售额也可能上涨约 5%，实际购买量没有同步增长。",
    method="Census 对企业进行月度调查与估算，初值可能修订；FRED 提供季调序列。",
    meaning="观察商品及餐饮消费需求，配合 PCE 和物价判断实际消费。",
    pitfalls=["不是全部居民消费。", "不是实际消费数量。", "单位为百万美元，不是十亿美元。"], related=["US:PCEC96", "US:CPIAUCSL"], sources=["retail_us"], aliases="零售 餐饮 消费 销售", kind="季调名义月度金额")
for code, income in [("PCEC96", False), ("DSPIC96", True)]:
    put("US", code, lead="看美国个人" + ("税后收入" if income else "消费支出") + "剔除价格影响后的水平。",
        scope="可支配个人收入是个人收入扣除个人当期税后的收入。" if income else "个人消费支出含居民直接购买及他人代付的消费，覆盖商品与服务。",
        calculation="BEA 将名义" + ("可支配收入" if income else "个人消费支出") + "按价格指数转换为实际水平，按月季调折年。",
        formula="实际水平 = 名义水平 ÷ 对应价格指数 × 100（参照年尺度）；月度季调水平折年 ×12",
        example="假设名义收入从 100 到 105、价格也从 100 到 105，实际购买力约保持不变。",
        method="BEA 国民账户结合调查和行政资料编制，单位为十亿美元、2017 年链式价格，季调折年序列由 FRED 提供。",
        meaning="观察家庭部门的" + ("购买力基础。" if income else "实际消费活动。") + "折年水平方便比较速度，不是当月金额。",
        pitfalls=["不是名义金额。", "不是当月或本年已实现总额。", "总体水平不等于每个家庭的情况。"],
        related=["US:DSPIC96" if not income else "US:PCEC96", "US:PSAVERT", "US:PCEPI"], sources=["pio", "bea"], aliases="实际 收入 消费 DPI PCE 购买力", kind="季调折年实际水平")
put("US", "PSAVERT", lead="看美国个人税后收入中，有多少在当期没有用于个人支出。",
    scope="国民账户中的个人储蓄占可支配个人收入比例。个人支出除 PCE 外还包括个人利息支付、个人当期转移支出。",
    calculation="先以可支配收入减个人支出得储蓄，再除以可支配收入。", formula="储蓄率 =（可支配个人收入 − 个人支出）÷ 可支配个人收入 × 100%",
    example="假设税后收入 100、消费 90、利息和转移支出 2，储蓄 8、储蓄率 8%；不能算成 10%。",
    method="BEA 按国民账户定义编制，储蓄是收支差额，相关收入或支出修订会改变储蓄率。",
    meaning="观察家庭部门收入与支出之间的缓冲，不等于银行存款增长率。",
    pitfalls=["不能只用收入减 PCE。", "资产价格上涨不是当期储蓄。", "总体比例不是每户的储蓄比例。"], related=["US:DSPIC96", "US:PCEC96"], sources=["pio"], aliases="储蓄 消费 收入 储蓄率", kind="季调比例")
for code, description in [
    ("HOUST", "已经开始地基开挖等实际施工的住宅单位"),
    ("PERMIT", "获得建设许可的住宅单位"),
    ("HSN1F", "已签销售合同或付定金的新建独栋住宅")]:
    put("US", code, lead="看美国" + description + "的活动规模。", scope=description + "。本指标按住房单位计数，页面单位千套、季调折年；销售仅限新建独栋住宅。",
        calculation="按调查定义汇总对应活动，再季调并折成年尺度。", formula="月度折年数量 = 季调月度数量 × 12",
        example="假设月度季调 10 万套，折年为 120 万套；不是本月建成或售出 120 万套。",
        method="Census 与 HUD 结合建筑许可资料和住宅建设抽样调查等编制；初报会修订。",
        meaning="许可、开工、销售是不同环节，配合房贷利率判断活动变化。销售可能发生在开工前。",
        pitfalls=["折年数量不是已实现全年总数。", "许可不等于开工，开工不等于竣工。", "新房销售不含存量房交易。" if code == "HSN1F" else "一栋多户住宅可以包含多个住房单位。"],
        related=["US:HOUST", "US:PERMIT", "US:HSN1F", "US:MORTGAGE30US"], sources=["housing_us", "housing_method"], aliases="住房 新屋 开工 许可 营建 销售 房地产", kind="季调折年数量")
put("US", "MORTGAGE30US", lead="看符合调查条件的美国 30 年固定住房贷款，平均利率处于什么水平。",
    scope="Freddie Mac PMMS 的符合筛选条件的购房贷款申请，代表特定产品与借款人范围；不是所有房贷。",
    calculation="按调查方法汇总符合条件的贷款利率，形成周度均值。", formula="周度调查平均利率 = 合格样本按调查规则汇总的利率",
    example="调查为 6.5% 时，个人实际报价仍会因信用、首付、费用及贷款条件而不同。",
    method="2022 年改进后的 PMMS 使用申请数据并筛选，历史旧方法与新方法应留意方法衔接。",
    meaning="观察购房融资成本，利率上升通常提高同额贷款的付息负担。",
    pitfalls=["不是个人可保证取得的报价。", "不等于联邦基金利率。", "周度均值不能当作每笔申请利率。"], related=["US:HOUST", "US:HSN1F", "US:DGS10"], sources=["mortgage"], aliases="房贷 按揭 利率 PMMS", kind="周度调查利率")
for code, lower in [("DFEDTARL", True), ("DFEDTARU", False)]:
    put("US", code, lead="看美联储联邦基金目标利率区间的" + ("下限。" if lower else "上限。"),
        scope="FOMC 设定的政策目标区间边界，不是市场成交利率；日度序列包含政策不变的非工作日。",
        calculation="按政策决策记录当日有效的区间边界。", formula="目标区间 = [下限, 上限]，边界不是平均成交价",
        example="假设区间为 4.00%—4.25%，下限 4.00%、上限 4.25%；市场有效利率可能为 4.08%。",
        method="美联储发布政策决策与生效安排，FRED 记录目标边界。",
        meaning="反映货币政策设定。上下限须一起阅读，并与实际 EFFR 区分。",
        pitfalls=["不是每个借款人的贷款利率。", "不是有效联邦基金成交利率。", "区间变化与房贷报价变化不必一比一。"],
        related=["US:DFEDTARL", "US:DFEDTARU", "US:EFFR"], sources=["policy"], aliases="美联储 FOMC 联邦基金 政策 利率", kind="政策目标利率")
put("US", "EFFR", lead="看美国联邦基金市场实际隔夜交易的代表利率。",
    scope="纳入纽约联储统计的隔夜联邦基金交易，区别于政策目标边界。",
    calculation="按成交量权重计算交易利率的中位数。", formula="EFFR = 联邦基金合格交易的成交量加权中位利率",
    example="中位利率不是把各笔利率简单平均；成交量较大的交易对中位位置影响更大。",
    method="纽约联储依据 FR 2420 报告交易资料计算，工作日发布，非工作日或来源缺值不补造。",
    meaning="反映隔夜市场实际资金价格，可与政策目标区间一起观察。",
    pitfalls=["不是加权算术平均。", "不是贷款给家庭的报价。", "休市缺值不能自动沿用当日利率。"], related=["US:DFEDTARL", "US:DFEDTARU", "US:DGS2"], sources=["effr"], aliases="隔夜 联邦基金 有效利率", kind="成交量加权中位利率")
for code, years in [("DGS2", 2), ("DGS10", 10), ("DGS30", 30)]:
    put("US", code, lead=f"看美国国债收益率曲线上 {years} 年固定期限的市场利率。",
        scope="固定期限国债收益率，是由市场报价拟合的期限曲线读取值；不是某一只债券的票息。",
        calculation="财政部用国债市场报价构建收益率曲线，从对应期限读取收益率。", formula=f"{years} 年固定期限收益率 = 当日拟合国债曲线在 {years} 年处的收益率",
        example="收益率 4% 不是债券价格上涨 4%；在其他条件相同下，收益率上升通常对应债券价格下降。",
        method="美国财政部按公布的收益率曲线方法估计，FRED 提供工作日序列，休市缺值保留。",
        meaning="反映该期限的市场资金价格与预期。不同期限差值可观察曲线形状，但不是确定的经济预测。",
        pitfalls=["不是单只债券票面利率。", "不是投资的保证收益。", "曲线倒挂不能保证衰退时间或结果。"], related=["US:DGS2", "US:DGS10", "US:EFFR"], sources=["treasury"], aliases="国债 美债 收益率 利率 曲线", kind="固定期限市场收益率")

for auxiliary, primary in [("CPIAUCNS", "CPIAUCSL"), ("CPILFENS", "CPILFESL")]:
    original = GUIDES[("US", primary)]
    GUIDES[("US", auxiliary)] = {**original, "title": SPEC[("US", auxiliary)].name,
        "lead": "美国 CPI 同比所用的未季调指数；月度环比优先查看对应季调序列。",
        "scope": original["scope"] + " 本序列未做季节调整，与季调序列的值分开保存。",
        "formula": "同比 =（本月未季调指数 ÷ 上年同月未季调指数 − 1）× 100%",
        "calculation": "按相同未季调序列比较上年同月。季调环比采用对应季调序列，不能将两条序列混作分子与分母。",
        "method": "BLS 按 CPI 采价及加权方法编制，FRED 提供该未季调序列。与对应季调序列独立保存。",
        "kind": "未季调价格指数", "related": ["US:" + primary],
        "sources": original["sources"] + [("FRED · 未季调原始序列", "https://fred.stlouisfed.org/series/" + auxiliary)]}

SOURCES.update({
    "unemployment_new": ["国家统计局：分年龄组失业率新口径", "https://www.stats.gov.cn/xxgk/sjfb/zxfb2020/202401/t20240117_1946644.html"],
    "unemployment_detail": ["国家统计局：就业调查分层数据", "https://www.stats.gov.cn/sj/zxfbhjd/202609/t20260915_1965307.html"],
    "ppi_us": ["BLS · FRED 最终需求 PPI", "https://fred.stlouisfed.org/series/PPIFIS"],
    "orders_us": ["Census · FRED 耐用品订单", "https://fred.stlouisfed.org/series/DGORDER"],
    "sentiment_us": ["密歇根大学 · FRED 消费者信心", "https://fred.stlouisfed.org/series/UMCSENT"],
    "ism": ["ISM：制造业与服务业调查方法", "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/services/august/"],
    "budget": ["国务院：2026年政府工作报告", "https://www.gov.cn/yaowen/liebiao/202603/content_7062625.htm"],
})
for code, scope in [
    ("UNEMPLOYMENT_CITIES31", "31个大城市城镇劳动力的合并调查比率；不是31条单城市失业率，也不是简单平均。"),
    ("UNEMPLOYMENT_LOCAL", "调查地本地户籍的城镇劳动力，不按每个人出生地判断。"),
    ("UNEMPLOYMENT_MIGRANT", "调查地外来户籍的城镇劳动力，包含农业与非农业户籍，不等于全部农民工。"),
    ("UNEMPLOYMENT_16_24", "全国城镇16—24岁劳动力，不包含在校生；新口径自2023年12月起。"),
    ("UNEMPLOYMENT_25_29", "全国城镇25—29岁劳动力，不包含在校生；新口径自2023年12月起。"),
    ("UNEMPLOYMENT_30_59", "全国城镇30—59岁劳动力，不包含在校生；不能与旧25—59岁指标混接。"),
]:
    put("CN", code, lead="观察特定劳动力群体的就业压力。", scope=scope,
        calculation="按月度劳动力抽样调查，将符合失业定义的人数除以对应群体劳动力人数。", formula="调查失业率 = 失业人数 ÷（就业人数 + 失业人数）× 100%",
        example="假设某群体劳动力100人、失业5人，失业率为5%，分母不是该群体全部人口。",
        method="国家统计局劳动力调查；新分年龄序列排除在校生，旧口径历史不拼接。",
        meaning="帮助区分年龄、户籍与大城市就业压力，和全国总体率一起看。",
        pitfalls=["失业率的变化用百分点表达，不把比率再算成就业增长率。", "群体分母与覆盖范围不同，不能直接相加或简单平均。", "2023年12月前含在校生青年失业率不能与新口径连接。"],
        related=["CN:UNEMPLOYMENT"], sources=["unemployment_cn", "unemployment_new", "unemployment_detail"], aliases="就业 青年 年龄 户籍 31城", kind="月度调查比率")
for code in ["PPIFIS", "PPIFID"]:
    put("US", code, lead="观察美国生产者出售最终需求商品与服务的价格变化。",
        scope="最终需求商品、服务及建筑等，区别于中国工业出厂价格PPI；2009年11月=100。",
        calculation="BLS按生产者收到的销售价格调查和交易权重编制指数。同比用未季调基数，环比用季调基数。",
        formula="同比 =（本月未季调指数 ÷ 上年同月未季调指数 − 1）×100%；环比使用季调指数比较上月",
        example="假设去年同月100、本月103，同比为3%，不是指数103本身等于103%涨幅。",
        method="BLS最终需求PPI由FRED分发，季调与未季调序列独立入库，历史值可能修订。",
        meaning="观察上游与服务生产者价格压力，配合CPI了解消费端价格。",
        pitfalls=["不是只有制造业商品。", "指数水平不是同比；季调与未季调基数不可混用。"],
        related=["US:CPIAUCSL", "US:PCEPI"], sources=["ppi_us"], aliases="生产者 物价 PPI 最终需求", kind="价格指数")
put("US", "DGORDER", lead="观察制造商收到的耐用品新订单。", scope="耐用品为通常可使用三年及以上的产品，包含汽车、飞机等运输设备；名义金额、百万美元。",
    calculation="Census制造业出货、库存和订单调查汇总新订单，按月季调。", formula="增长率 =（本期订单金额 ÷ 对比期订单金额 − 1）×100%",
    example="假设订单从100到105，增长5%；大型飞机订单可能使当月明显波动。",
    method="Census M3调查经FRED提供季调金额；初值和历史可能修订。", meaning="作为制造业后续生产与投资需求的扩展观察项。",
    pitfalls=["订单不是已经完成的出货。", "名义金额受价格与运输设备大单影响，不等于实际产量。"], related=["US:INDPRO", "US:GDPC1"], sources=["orders_us"], aliases="耐用品 新订单 制造业", kind="季调名义订单金额")
put("US", "UMCSENT", lead="观察美国消费者对自身财务和经济前景的信心。", scope="密歇根大学消费者调查，1966年一季度=100；与谘商会消费者信心指数不同，FRED延迟一个月提供。",
    calculation="按调查问题的积极与消极回答构成相对评分，汇总并按基期归一化。", formula="信心指数 = 调查回答综合评分按基期尺度归一化；不是消费支出增速",
    example="假设指数从60降到55，下降5个指数点，不能说消费者支出下降5%。",
    method="Surveys of Consumers, University of Michigan，经FRED分发；非季调，以来源实际统计期显示。", meaning="补充零售和收入数据，观察主观预期与实际支出的关系。",
    pitfalls=["不是谘商会指数，不能拼接。", "调查情绪不等于真实消费金额，延迟数据不冒充即时数据。"], related=["US:RSAFS", "US:DSPIC96"], sources=["sentiment_us"], aliases="消费者 信心 情绪 密歇根", kind="调查信心指数")
for code, sector in [("PMI_ISM_MANUFACTURING", "制造业"), ("PMI_ISM_SERVICES", "服务业")]:
    put("US", code, lead=f"观察ISM调查中美国{sector}活动的扩张或收缩。", scope=f"美国{sector}采购与供应管理人员月度调查，50为行业扩张与收缩的分界；不是S&P Global PMI。",
        calculation="分项扩散指数结合回答变好、不变、变差的占比；制造业五个分项各占20%，服务业四个分项等权。",
        formula="分项扩散指数 = 改善回答占比 + 0.5×不变回答占比；总PMI按规定权重合成",
        example="假设30%改善、50%不变，分项为55；不是生产同比增长55%。",
        method="ISM月度官方报告，部分组成项季调；只按报告明确统计月份接入，完整历史另需核验可用来源。",
        meaning="补充GDP和工业生产，观察企业景气方向。", pitfalls=["50是调查扩张分界，不是50%的经济增长。", "不能与中国PMI或其他供应商PMI直接混成同一序列。"],
        related=["US:GDPC1", "US:INDPRO"], sources=["ism"], aliases="ISM PMI 景气 采购经理", kind="月度扩散指数")
put("CN", "DEFICIT_BUDGET_RATIO", lead="观察政府为一个年度安排的预算赤字强度。", scope="政府工作报告公布的年度官方预算赤字率约数；不是月度财政支出减收入，也不是实际执行或广义赤字率。",
    calculation="引用官方报告给出的年度预算安排，不用已公布GDP或月度收支自行替换该口径。", formula="官方预算赤字率 = 年度预算赤字 ÷ 预算对应GDP口径 ×100%（采用公布约数）",
    example="假设官方安排约4%，页面保留约数含义，不能据此说全年实际结果已经是4%。",
    method="按年度政府工作报告及其明确发布日期核验，变化记录保留来源证据。", meaning="判断年度财政政策安排，并与月度预算收支分别阅读。",
    pitfalls=["不是实际执行值。", "不把地方专项债和特别国债任意相加后仍叫官方赤字率。"], related=["CN:FISCAL_REVENUE_CUM", "CN:GDP_ANNUAL"], sources=["budget"], aliases="财政 预算 赤字率 政策", kind="年度预算安排约数")

# Fail visibly at startup instead of silently publishing a partial encyclopedia.
if set(GUIDES) != set(SPEC):
    raise RuntimeError(f"Indicator guide coverage mismatch: missing={set(SPEC)-set(GUIDES)}, extra={set(GUIDES)-set(SPEC)}")
