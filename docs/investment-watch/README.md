# 投资动态 · 实施依据与可用版

## 当前版本 v0.4 · 2026-09-30

已经由静态 Demo 进入正式 Django 实现。以本目录为统一依据；此前独立网站和独立“投资逻辑”编辑页只作历史参考。

页面链路：**新闻浏览 → 主题 / 个人投资动态 → 已有公司研究与综合分析 → 已有正式判断及历史**。

- 新闻浏览不依赖关注规则或模型；主题是公共材料目录。
- 投资动态管理本人候选、逐假设证据与复核，不另建判断编辑器。
- 新材料由成员选入新的综合分析，旧分析与旧引文保留。
- 第一版使用确定性规则和必要模型调用，取消热榜、日报及默认双评分。

v0.4 补齐：新增公司与探索档案、保存前召回预览、私密公司主题、公司研究总览（投研 / 新闻 / 综合分析）、管理员信源模板测试与保存、来源陈述 / 作者观点 / 分析推断、材料关系复核及确认重复后的分析复用。

事件识别采用保守策略：精确内容可自动归并，标题相似只提示人工复核；尚未完成全面语义聚簇、自动识别所有新增事实或真实模型质量验收。各任务完成边界见任务状态与验证记录。

## 入口与文件

1. [需求及优先级](01-requirements.md)
2. [架构与模块边界](02-architecture.md)
3. [已实现接口契约](03-contracts.md) / [OpenAPI](openapi.json)
4. [任务状态](04-tasks.md)
5. [验证记录及限制](05-validation.md)
6. [AIHOT 借鉴与取舍](06-aihot-decisions.md)
7. [本机试用、配置与上线手册](07-operations.md)

本机可用版：[新闻浏览](http://127.0.0.1:4319/research/watch/news/)。
原 [4318 静态 Demo](http://127.0.0.1:4318/) 保留作设计对照。

[公司研究新布局预览](http://127.0.0.1:4318/company.html)：以“研究结论 / 最新变化 / 证据资料”组织页面，统一“重新评估”入口，演示检查最新变化、完整重评、仅使用投研资料与研究历史。使用既有历史样本和明确标记的模拟场景，研究建议为人工样例。此设计尚未应用到 Django 页面；演示记录仅在浏览器保存。

本机库独立，真实公开新闻与演示研究判断分清，云端模型关闭；未部署 NAS。正式后台采集/分析代码和管理命令已实现，NAS 调度尚未启用。真实模型质量、账单和生产首轮运行仍需验收，不能用模拟模型测试代替。

## 代码位置

```text
app/investment_watch/
  models.py / migrations/       公共材料、私人候选、不可变证据、预算及租约
  services.py                   范围校验、版本、幂等操作、关联与复核
  collection.py / catalogue.py  公开资料适配、宽松预筛、主题与召回
  analysis.py / prompts/        授权后的逐假设分析及引文验证
  budget.py / worker.py         费用预留、任务恢复与批量调度
  research_bridge.py            选定新闻加入已有研究新快照
  views.py / urls.py            同源 HTML 和 JSON
  tests*.py                     权限、端到端服务、并发与契约检查
app/templates/investment_watch/
app/static/css/investment-watch.css
app/config/settings_watch_*.py  隔离本机和测试环境
config/investment-watch.env.example
```

分支：`codex/investment-watch`，基线 `f002494`。
开发目录：`C:/Users/Administrator/Documents/Codex/家庭工作台-投资动态`。
独立投资新闻项目继续保留作参考，不参与正式运行。
