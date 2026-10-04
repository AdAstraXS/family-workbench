"""Shared navigation metadata; links never trigger background work."""
from django.urls import reverse


MODULES = (
    ("portfolio", "overview", "投资组合", "briefcase-2", "账户、持仓与资产变化", "财富管理"),
    ("ledger", "overview", "家庭账本", "wallet", "收支、预算与家庭资产", "财富管理"),
    ("investment_research", "index", "投研", "chart-line", "公司研究与投资判断", "财富管理"),
    ("option_wheel", "index", "期权车轮", "chart-donut", "策略、合约与交易管理", "财富管理"),
    ("ipo", "index", "港股打新", "building-bank", "新股、申购与收益记录", "财富管理"),
    ("notes", "index", "投资笔记", "notes", "记录观点，回顾思考", "阅读与积累"),
    ("knowledge", "index", "知识中心", "book-2", "收集、整理与查找资料", "阅读与积累"),
    ("reading", "index", "在线书库", "book-2", "书籍、阅读计划与批注", "阅读与积累"),
    ("trading_journal", "index", "交易复盘", "report-money", "交易案例 · 功能筹备中", "阅读与积累"),
    ("investment_watch", "news", "新闻跟踪", "chart-line", "关注公司的重要新闻与投资线索", "观察与分析"),
    ("intelligence", "program_list", "精选订阅", "book-2", "完整访谈、节目与投资文章", "观察与分析"),
    ("macro", "index", "宏观数据", "chart-line", "观察宏观指标与变化", "观察与分析"),
    ("ai_analysis", "index", "AI 检索与问答", "sparkles", "在知识库和已授权资料中查找答案", "观察与分析"),
    ("monitoring", "index", "运行监控", "chart-line", "AI 用量、账户余额与 NAS 流量", "工作台管理"),
    ("intelligence", "index", "AI 情报", "sparkles", "废案待处理 · 保留旧方案与历史记录", "工作台管理"),
)


def workspace_navigation(request):
    match = getattr(request, "resolver_match", None)
    active = getattr(match, "app_name", "")
    is_programs = active == "intelligence" and getattr(match, "url_name", "").startswith("program_")
    modules = [dict(app=app, url=reverse(f"{app}:{view}"), label=label,
                    icon=icon, description=description, group=group,
                    badge="废案待处理" if app == "intelligence" and view == "index" else "",
                    active=active == app and (app != "intelligence" or is_programs == (view == "program_list")))
               for app, view, label, icon, description, group in MODULES]
    return {"workspace_modules": modules,
            "workspace_is_programs": is_programs,
            "workspace_is_legacy_intelligence": active == "intelligence" and not is_programs,
            "workspace_title": next((m["label"] for m in modules if m["active"]), "家庭概览")}
