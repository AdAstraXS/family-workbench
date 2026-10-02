"""Configurable public taxonomy, independent of personal research."""

TOPICS = [
    ("us", "美国市场", "市场与资产", "美国经济、公司经营与市场变化。", []),
    ("china", "中国市场", "市场与资产", "中国经济、企业与供需变化。", []),
    (
        "commodities",
        "商品与供需",
        "市场与资产",
        "库存、价格、产能与产业链。",
        [
            "原油",
            "大宗商品",
            "碳酸锂",
            "铜价",
            "铁矿石",
            "库存",
            "黄金",
            "oil",
            "commodity",
        ],
    ),
    (
        "funds",
        "基金与 ETF",
        "市场与资产",
        "资金流向、基金配置与产品。",
        ["基金", "etf", "fund"],
    ),
    (
        "microsoft",
        "微软",
        "公司与行业",
        "微软财报、产品及经营变化。",
        ["微软", "microsoft", "msft"],
    ),
    (
        "cloud-ai",
        "云计算与 AI 商业化",
        "公司与行业",
        "云服务、企业采用与产品收费。",
        ["azure", "copilot", "cloud", "openai", "AI", "人工智能", "云计算"],
    ),
    (
        "capex",
        "资本开支与基础设施",
        "公司与行业",
        "资本投入、建设、电力与回报。",
        ["资本开支", "数据中心", "算力", "capex", "data center", "供电"],
    ),
    (
        "rates",
        "宏观、利率与债券",
        "全球环境",
        "利率、通胀、融资与经济增长。",
        [
            "利率",
            "通胀",
            "债券",
            "国债",
            "美联储",
            "央行",
            "财政",
            "pmi",
            "gdp",
            "汇率",
            "美元/日元",
            "股指",
            "指数",
            "interest rate",
            "inflation",
            "fed",
            "federal reserve",
            "federal open market",
            "fomc",
            "discount rate",
            "monetary policy",
        ],
    ),
    (
        "geopolitics",
        "地缘政治与贸易",
        "全球环境",
        "贸易限制、国际政策与供应链风险。",
        [
            "关税",
            "制裁",
            "地缘",
            "出口管制",
            "贸易",
            "台湾",
            "伊朗",
            "战争",
            "tariff",
            "sanction",
        ],
    ),
]
CATEGORIES = ["公司", "产品", "行业", "宏观", "商品", "基金"]


def contains(text, term):
    import re
    import unicodedata

    text = unicodedata.normalize("NFKC", text).casefold()
    term = unicodedata.normalize("NFKC", term).strip().casefold()
    if not term:
        return False
    if re.fullmatch(r"[a-z0-9 ._-]+", term):
        return bool(
            re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text)
        )
    return term in text


def classify(title, summary, market):
    text = title + " " + summary
    topics = [
        key for key, _, _, _, words in TOPICS if any(contains(text, w) for w in words)
    ]
    if market in {"美国", "中国"}:
        topics.append("us" if market == "美国" else "china")
    for key, words in {
        "us": [
            "美国",
            "美联储",
            "美股",
            "纳斯达克",
            "微软",
            "microsoft",
            "federal reserve",
            "fomc",
        ],
        "china": [
            "中国",
            "我国",
            "人民币",
            "上海",
            "北京",
            "广州",
            "创业板",
            "科创",
            "港股",
            "a股",
        ],
    }.items():
        if key not in topics and any(contains(text, w) for w in words):
            topics.append(key)
    category = next(
        (
            label
            for key, label in [
                ("commodities", "商品"),
                ("funds", "基金"),
                ("rates", "宏观"),
                ("geopolitics", "宏观"),
                ("capex", "行业"),
                ("cloud-ai", "产品"),
            ]
            if key in topics
        ),
        "公司",
    )
    return topics, category


def qualifies(title, summary):
    """Broad finance intake. Only reject clearly unrelated lifestyle material."""
    text = title + " " + summary
    finance = [
        "经济",
        "金融",
        "公司",
        "产业",
        "行业",
        "市场",
        "投资",
        "融资",
        "消费",
        "供应链",
        "成本",
        "能源",
        "电力",
        "财报",
        "营收",
        "利润",
        "股",
        "债",
        "汇率",
        "资本",
        "银行",
        "房地",
        "财政",
        "就业",
        "数据中心",
        "芯片",
        "AI",
        "科技",
        "制造",
        "铁路",
    ]
    unrelated = [
        "明星私生活",
        "影视剧",
        "恋爱",
        "婚恋",
        "穿搭",
        "减肥",
        "养生",
        "美食教程",
        "娱乐八卦",
        "水位骤降",
    ]
    return not (
        any(w in text for w in unrelated)
        and not any(contains(text, w) for w in finance)
    )


def match_rule(rule, version):
    if not rule.enabled:
        return False, "自动关注已暂停"
    text = version.title + " " + version.summary
    if any(contains(text, word) for word in rule.exclude):
        return False, "命中排除词"
    if rule.include and not any(contains(text, word) for word in rule.include):
        return False, "未命中包含词"
    hits = [w for w in rule.aliases + rule.topics if contains(text, w)]
    return bool(hits), (("命中 " + "、".join(hits) + "；仅为候选相关性")[:500]
                        if hits else "未命中公司别名或关联领域")
