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
    ("reading", "index", "在线书库", "book-2", "阅读空间 · 功能筹备中", "阅读与积累"),
    ("trading_journal", "index", "交易复盘", "report-money", "交易案例 · 功能筹备中", "阅读与积累"),
    ("intelligence", "index", "AI 情报", "sparkles", "动态、人物与资讯线索", "观察与分析"),
    ("macro", "index", "宏观数据", "chart-line", "观察宏观指标与变化", "观察与分析"),
    ("ai_analysis", "index", "AI 分析", "sparkles", "分析工具与模型服务", "观察与分析"),
)


def workspace_navigation(request):
    active = getattr(getattr(request, "resolver_match", None), "app_name", "")
    modules = [dict(app=app, url=reverse(f"{app}:{view}"), label=label,
                    icon=icon, description=description, group=group, active=active == app)
               for app, view, label, icon, description, group in MODULES]
    return {"workspace_modules": modules,
            "workspace_title": next((m["label"] for m in modules if m["active"]), "家庭概览")}
