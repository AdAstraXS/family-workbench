# 宏观数据接入层

范围：中国、美国分别查看；不做中美对照或 A 股盈利专题。代码中的指标字典是
`app/macro/registry.py`，明确单位、统计期、季调、累计/单期、字段选择和换算规则。

## 采集与验收

```text
python manage.py import_macro --list
python manage.py import_macro --group nbs_fai
python manage.py import_macro --group fred_UNRATE
python manage.py import_macro --all-structured
```

以上默认只读试算，无任何数据库写入。先检查结果，再加 `--write` 明确入库。
命令逐组事务；一组失败不会撤销其他已经成功的组，但命令最终返回非零状态。
每个请求最多运行 90 秒，失败重试一次；第三方库运行在独立子进程中。

官方发布稿目前需要明确地址，不自动猜测最新文章：

```text
python manage.py import_macro --group mofcom --url https://www.mofcom.gov.cn/实际发布稿路径
python manage.py import_macro --group pbc --url https://www.pbc.gov.cn/实际发布稿路径
python manage.py import_macro --group nbs_release --url https://www.stats.gov.cn/实际发布稿路径
```

仅允许对应官方 HTTPS 域名，不跟随跨站重定向。布局、关键字段、统计期或基建范围附注
不匹配时整组拒绝入库。当前解析覆盖已核验的发布稿格式；不代表所有历史格式都支持。
商务部行业金额是可选披露项；未披露的项目不补零，也不撤销以前统计期的数据。

## 数据与修订

- `MacroIndicator` 沿用指标目录；`MacroSourceMapping` 固化登记字段和口径哈希。
- 新采集使用 `MacroObservation`，按来源映射、地区、统计期唯一。月、季、年度统一期初标记；
  周、日保留来源日期。发布日期未知时为空，不使用采集日期代替。
- `MacroObservationRevision` 保存每次观察到的变化、原始字段、口径、来源地址和响应校验值。
  第一版是首次采集版本，不声称是历史首次公布值。相同数值重复采集只更新核验时间。
- `MacroImportRun` 保存写入任务的状态、缺值数、统计期和失败原因。只读试算不留数据库记录。
- 来源空值保留为 NULL；来源未返回的旧统计期不删除。来源重复键、超精度值、全空或未来统计期拒绝导入。
- 同组入库按映射行锁串行化；较早启动但较晚完成的任务不能覆盖较新已完成任务。
- 字典口径变动会拒绝覆盖旧映射，应先核验并通过新代码/迁移明确处理。
- 增量迁移只新增表，原 `MacroDataPoint` 及其重复记录保持原样，不自动猜测历史来源，也不混入新页面。

## 已处理的口径陷阱

- 投资累计金额只取“自年初累计”；累计同比单独取统计局，不取另一口径的“同比增长”列。
- 基建同比保存当期发布稿中的统计范围附注，不以行业增速平均代替。
- FDI 为人民币实际使用外资；新设企业数量、行业金额独立，高技术与行业交叉不可加总。
- 央行住户/企事业单位贷款保留累计口径；减少记负数，万亿元用 Decimal 换算亿元。
- 70 城四条序列保留指数（基期=100），不把指数水平直接标为涨幅；拉萨全空目录行不属于有效70城。
- 进出口原始金额实际上需要除以 100000 才是亿美元，采用来源前端 `zoom: -5` 的明确换算。
  AKShare 文档中的单位文字与原始示例量级不一致，不能直接照抄。
  证据：[来源页面脚本](https://data.eastmoney.com/newstatic/js/cjsj/cn/hgjck.js)。
- 美国 CPI/PCE/GDP 等指数、非农人数水平、折年数量均按原始口径保存。页面在只读展示层计算增速，
  不改写原始观测；CPI 同比所需的未季调 `CPIAUCNS` / `CPILFENS` 与季调序列分开采集。
- 中国制造业/非制造业 PMI 标注为季调，依据[统计局说明](https://www.stats.gov.cn/zs/tjws/tjzb/202301/t20230101_1903972.html)。

## 新增就业、财政及美国观察项的来源约束

- 中国就业细分取国家统计局“城镇调查失业率 > 城镇调查失业率”结构化字段，31个大城市指标
  是合并调查率，户籍分为本地和外来。年龄段为16—24、25—29、30—59岁，不含在校生。
  新分年龄序列只接入2023年12月起的数据，解析器过滤同名字段中早期含在校生数据，不能直接连接。
- 年度预算赤字率取中国政府网政府工作报告的公布约数。必须核验报告中的年度工作任务及正式
  发布日期；不从月度财政收入与支出推算，不把预算安排标为实际执行。
- 美国新增 `DGS30`（日度固定期限30年国债）、`PPIFIS`/`PPIFID`（季调/未季调最终需求PPI）、
  `DGORDER`（季调耐用品订单、百万美元、含运输设备）、`UMCSENT`（密歇根信心调查、非季调）。
  FRED无需API Key；密歇根数据延迟一个月，早期缺月、国债停发阶段等缺值保留，不填补。
- ISM制造业/服务业PMI按指定官方月度报告的标题取统计月份及当月总指数，仅导入报告明确的点。
  标题的月份不能替换为发布日期，第三方接口只有发布日的历史不得自动挪到上月。
  公开程序下载当前会转登录页或返回错误页，禁止把错误页面解析为数据。完整历史和稳定自动更新
  尚未接入；已核验的公开标题摘录须在采集证据中标为摘录，不冒充完整HTML，发布日期未知保留空值。
  方法来源见[制造业官方报告](https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/pmi/september/)
  和[服务业官方报告](https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/services/august/)。

## 页面与运维

`/macro/` 提供中国/美国独立入口、主题趋势、城市选择、分页历史、指标百科和修订证据；页面与
展示约束见 [前端说明](macro-data-frontend.md)。`/macro/sources/` 可供普通有效家庭成员查看更新记录。
所有入口只读，不在 GET 抓取或写入。后台模型只读，修订须走采集服务。

本阶段尚未部署 NAS、执行生产迁移或配置 DSM 任务。新增 `akshare==1.19.1` 依赖，后续部署必须
构建含此依赖的新镜像，并按 NAS 技能完成备份、生产基线核对和连通性验证；不能只挂载源码。
定时任务未来用 DSM 调用此命令并保存标准输出、错误输出和退出码。异常中断可能留下“运行中”，
应结合任务日志核验，不自动冒充完成。

## 延后项

M1 的新旧口径及官方回溯、LPR/RRR 政策日期、ISM 完整历史及稳定自动获取、GDP 平减指数测算、官方文章自动发现
和历史回补仍属后续范围。此阶段不要求用户申请 FRED API Key。

## 本地验证

使用独立 `test_macro_frontend` PostgreSQL 数据库；不使用本地应用库或生产库作为测试库。
`test_macro_ingestion` 现用于实际公开样本的本地预览回放，不再作为测试运行库，以免清空已有预览数据。

```text
docker compose exec -T -e DJANGO_SETTINGS_MODULE=config.test_settings -e WORKBENCH_TEST_DB=test_macro_frontend web python manage.py test macro --keepdb
docker compose exec -T web python manage.py check
docker compose exec -T web python manage.py makemigrations --check --dry-run
git diff --check
```

2026-10-04：21 项测试通过，覆盖幂等、修订恢复旧值、回滚、旧数据保留、并发防覆盖、来源解析和权限。
系统检查仅有既有 OneNote 配置提醒。Python 3.12 临时环境成功安装 AKShare 1.19.1；正式命令的
进出口、FRED 失业率、统计局投资只读试算通过。Windows 的一次 FRED 请求超时，Linux 临时环境同源成功；
网络稳定性仍须在 NAS 部署前独立验收。

2026-10-05 收尾：79 条指标的保存样本共解析 128,730 条观测（含缺值），已在隔离库逐组导入和
重复导入，全部幂等。FRED 部分样本来自前一步同频多序列下载，其早期空列也保留；正式采集始终单序列请求。
浏览器验证通过：国家切换、外资历史与发布来源、70 城选择、分页链接保留城市，以及桌面和手机尺寸布局。
采集命令的完整回放报告及临时预览数据留在本地 `outputs/macro-source-probe/`，不提交。
