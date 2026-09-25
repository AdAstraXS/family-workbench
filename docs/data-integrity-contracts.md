# 数据与访问边界

本文记录服务层和页面需要保持的长期约束。生产数据检查、历史修正与部署仍遵循 AGENTS.md 和 NAS 部署技能。

## 收支及资产估值

- 收入、支出事实保留原币和原金额。人民币报表调用 `ledger.valuation.cashflow_amount`：单笔按发生日，按月录入按统计期末日，采用该日或此前最近的有效汇率；逐笔使用 Decimal、四舍五入到分后汇总。
- 无有效汇率时，不以 1:1 或零值伪装完整结果。报表说明缺项；年度、月度明细仍显示原币记录，受影响的成员和家庭合计不显示。
- 投资摘要使用 `portfolio.valuation.value_portfolio`。缺价格或汇率时首页不显示完整总额，陈旧价格单独提示。月末资产快照与投资估值有重叠，不相加。
- 正式资产余额快照必须具备实际使用外币所需的正汇率。草稿可暂缺汇率，但不能进入正式摘要。网页、后台和导入校验共用 `ledger.valuation.calculate_base_amount`。
- 快照、年度预算的表头与明细在同一数据库事务内保存。

## 投资事实与投影

- 流水是事实，当前持仓和关联现金流水是可重建投影。重建使用账户行锁，锁保持到外层事务结束。
- 新增交易入口应在同一个 `transaction.atomic()` 中完成事实写入及重建。跨账户修改先调用 `lock_accounts`，按账户 ID 排序锁定全部受影响账户，再修改流水。PostgreSQL 使用 `NO KEY UPDATE`，避免与新流水的外键检查冲突。
- 模型的单条 save/delete 会取得账户锁；批量 SQL、QuerySet.update/delete、bulk_create 不经过这些方法，调用方必须预先锁定全部受影响账户并负责重建。历史批量导入不要与其他交易写入并行执行。
- 同步交易回到来源模块管理。期权到期、行权、指派的关联流水禁止单独编辑或删除；完整事件撤销功能需要独立设计，不能通过删除其中一条代替。
- IPO 后台批量删除调用与单条删除相同的同步清理服务；后台禁止直接删除会级联申购记录的上市资料。

## 私密内容

- 检索索引是投影，不是权限依据。搜索、计数和筛选通过 `accessible_search_entries` 实时核对原始笔记、文档及来源、专题成果的访问范围。
- 来源或文档改为私密后，无需等待重新索引即可收紧访问。
- 绑定成员的超级管理员同样遵守内容权限；未绑定或停用成员的管理员不能读取内容后台。
- 笔记、知识资料及 AI 请求/结果后台按业务可见范围只读展示。内容修改通过前台业务流程；后台关联选择器不能成为私密标题的旁路。

## 页面、缓存与测试

- GET 页面只读取已有行情及汇率。抓取由显式任务或 POST 操作触发。
- 汇率缓存限于一次请求或估值调用，不能使用跨请求的永久缓存。价格查询只取各标的截止日期之前的最新记录。
- 编辑、删除和详情的 `return_to` 只接受可解析的本地业务路径，携带原筛选及页码。没有有效返回地址时使用模块父级。
- 本地测试使用 `config.test_settings`，默认库名 `test_workbench_hardening`，或显式设置以 `test_` 开头且不等于应用库的 `WORKBENCH_TEST_DB`。切换有不同迁移历史的分支时，优先用独立测试库，避免复用污染的 keepdb。

```powershell
docker compose exec -T -e DJANGO_SETTINGS_MODULE=config.test_settings web python manage.py test ipo portfolio --keepdb --noinput
docker compose exec -T -e DJANGO_SETTINGS_MODULE=config.test_settings web python manage.py test --keepdb --noinput
docker compose exec -T web python manage.py check
docker compose exec -T web python manage.py makemigrations --check --dry-run
git diff --check
```
