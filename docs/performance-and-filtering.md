# 页面性能与自动筛选

## 本次范围

- 打新盈亏按原币分别汇总，图表选择单一币种；有成交流水时以流水币种为准，旧记录沿用证券市场币种规则。没有跨币种加总或新增换汇口径。
- 投资总览、当前账户和首页区分缺价格、缺汇率、价格需核对。缺项不会伪装成完整总额；资产快照变化明确包含出入金。无可比快照不显示虚构比较日期。
- 投研先按权限和关键词筛选、分页，再批量读取研究状态；投资总览补齐关联读取。
- 账户概览不加载全部流水；交易及现金流水每页 50 条。现金余额用完整历史的窗口计算后分页，个股盈亏仍按完整历史计算。
- 知识目录合并统计，普通列表只读取正文开头；搜索仍可命中正文末尾。
- 44 处只读查询表单使用公共自动筛选脚本。下拉选择约 250 毫秒更新，文字停顿约 650 毫秒、日期约 900 毫秒更新。中文输入组合期间不提交；筛选后重置页码和游标，保留焦点及滚动位置。
- 新分析、保存、导入和外部 Futu 查询保留明确操作按钮。自动筛选不会触发这些操作。无 JavaScript 时保留原有提交按钮。
- 新闻每页 20 条并缩短摘要；期权首页的历史合约对比可展开，明确标注报价日期与历史记录。新闻新增分类规则修正白银、煤矿等词，未批量改写历史分类。
- AI 情报保留既有“废案待处理”入口与历史资料。

## 资源与请求监测

Django 使用 STORAGES 的 CompressedManifestStaticFilesStorage。部署执行 collectstatic 后，模板引用含内容指纹的文件；验证 gzip 和 immutable 缓存，不能只检查收集命令退出码。

请求日志仅记录路由名称、方法、状态、耗时、查询次数、数据库耗时和响应字节数。没有查询参数、SQL、用户名或表单内容。站点设置只在一次请求内复用，不跨用户缓存。

将受限部署包装器读取的日志保存到本地后，可汇总：

```text
python scripts/summarize-request-performance.py < request.log
```

输出各路由的样本数、p50/p95、最大查询量和响应体积。小样本只用于诊断，不作为生产速度承诺。

## 后台任务保障

既有 11 个管理命令共用 BoundedJobCommand：全局 AI、初识报告、问题跟踪、判断分析、公司资料获取、期权分析/解释/批次建议/持仓扫描/Put 报价，以及财经资讯定时周期。

- PostgreSQL advisory lock 在不同网页进程、管理命令进程间共用 2 个执行槽；超过容量返回非零状态。
- 已存在的业务记录保存繁忙或失败原因，成员从原任务页查看并主动重试；定时周期保留非零退出码与日志。
- NAS/Linux 主线程最多运行 900 秒，模块自身更短的网络超时和授权检查继续生效。
- 异常释放执行槽；进程退出或容器重启后 PostgreSQL 自动释放锁。中断任务沿用模块现有超时恢复入口，无自动付费重试。
- 公司资料获取保留已完成项目，追加运行失败记录。
- 日志记录命令开始、结束、耗时和错误类型，不输出异常中的凭据或请求正文。子进程保留受控运行日志。
- SQLite 开发环境不提供 PostgreSQL 的跨进程锁；Windows 不提供 SIGALRM 硬时限。生产 NAS 不受这两个限制。
- 仅覆盖上述命令；维护、估值及旧情报流程不因本次修改自动纳入，也不新增 DSM 计划。

## 验证方法与实测

使用独立 PostgreSQL 和合成资料，未使用生产库进行测试。前后使用相同规模数据：

| 项目 | 修改前 | 修改后 |
| --- | ---: | ---: |
| 投研 50 家公司列表查询 | 356 | 7 |
| 投研 50 家公司、搜索无结果查询 | 356 | 6 |
| 投资总览 50 个持仓查询 | 116 | 15 |
| 知识空目录查询 | 25 | 19 |
| 账户 1,000 笔交易首屏 HTML | 1,374,104 字节 | 约 87,000 字节 |
| 账户概览加载交易/现金明细 | 全部 | 0 |
| 主样式传输（gzip） | 185,098 字节，未压缩 | 29,642 字节 |

查询结果不等于生产响应提速百分比。关联业务发生变化时，保留查询规模断言与余额断言。

统一离线设置：config.settings_quality_test；PostgreSQL 设置：config.settings_quality_pg_test。后者仅连接本机约定的测试端口，不读取生产配置。CI 保留安装依赖的实际版本日志。

```text
node scripts/test-auto-filters.mjs
python manage.py check --settings=config.settings_quality_pg_test
python manage.py makemigrations --check --dry-run --settings=config.settings_quality_pg_test
python manage.py test ipo portfolio knowledge family_core.test_runtime investment_watch investment_research ai_analysis option_wheel.tests.test_jobs.JobTests --settings=config.settings_quality_pg_test --noinput
python manage.py test option_wheel.tests.test_jobs.JobConcurrencyTests --settings=config.settings_quality_pg_test --noinput
```

旧并发测试自定义了数据库序列化恢复；与其他恢复测试混跑会重复导入 ContentType，故单独运行。后台执行槽测试只取得和释放 advisory lock，不清空数据库。

桌面与手机验收覆盖：投研第二页搜索后回第一页、浏览器返回继续搜索、打新币种切换、账户日期筛选、新闻分类与关键词组合。IME、分页游标清理、命名按钮、无效输入与 POST 排除另有脚本回归。

本次不新增数据库迁移，不更改 Python 依赖、Compose、生产配置和定时任务。
