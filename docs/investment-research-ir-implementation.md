# 官方 IR 获取：实现与验收说明

## 开源取舍（2026-09-26）

开发前检索并阅读了同类项目与库，未找到可直接提供本项目十三家公司全部免费官方材料、
不可变原件与私密投研引用流程的整套替代品。

| 项目 | 许可 / 定位 | 本次实际采用 |
| --- | --- | --- |
| [Fintech-Scraper](https://github.com/vishwasbabu/Fintech-Scraper) | Apache-2.0，按公司配置发现附件 | 参考公司配置、链接发现、历史去重思路；没有复制代码，没有引入 Playwright 服务 |
| [Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/bs4/doc/) | MIT，HTML 解析库 | 使用 `beautifulsoup4==4.15.0`，与同时上线的节目订阅模块共用依赖，直接用于链接、正文和表格单元格提取 |
| [pypdf](https://pypdf.readthedocs.io/en/stable/user/extract-text.html) | BSD-3-Clause，PDF 文本与页处理 | 复用项目已有 `pypdf==6.19.0`；正文保留物理页码、字符位置及原始 PDF |
| [EarningsCall Python](https://github.com/EarningsCall/earningscall-python) | MIT SDK，连接第三方 API 服务 | 未接入；SDK 开源不等于所需数据服务免费或覆盖完整 |

Office 附件复用现有 openpyxl，并用标准库解析 DOCX/PPTX；不安装办公套件、不执行宏。
没有接入收费 IR 数据 API，也没有把私密判断发给外部采集服务。

## 共用管道

- `providers/ir_registry.py`：十三家发行人身份、官网入口和附件域名/路径白名单。
- `providers/official_ir.py`：公开 Q4 财报接口共用五家；AMD/Intel 共用结果页适配；其余按官网结构保留小适配器。
- `providers/ir_http.py`：HTTPS 与跳转逐次校验、压缩解码、请求间隔、超时和单文件大小限制。
- `ir_extraction.py` / `ir_extract_worker.py`：HTML、PDF、DOCX、PPTX、XLSX 的正文提取；二进制文档在限时子进程内处理。
- `official_ir.py`：同一发行人别名共用公开材料；正文和原件追加固定版本、SHA-256、页码区段；更新失败保留已有版本。
- `/research/ir/`：十三家公司入口；日常主操作为“开始了解”，同步及错误状态放在次级区域。
- `sync_research_ir`：现有 Django 管理命令模式，支持指定公司、全部支持公司、按需正文与增量运行；失败返回非零。

财年/季度来自官方目录；只有明确的发布日期/截止日才写日期字段。Q4 目录的占位日期、
SK 海力士的电话会时间不冒充发布日期。原币种、单位、单季/累计口径保留在原文，不由采集器换算。
管理层准备稿、完整文字稿、财务报表、新闻稿、演示和股东信分别标注；不给所有公司套必备指标。

## 使用与运行边界

1. 公司卡片可创建私密探索档案，不要求持仓或先写判断。SK 海力士使用 KR/000660，TSM 与台湾代码、GOOG 与 GOOGL 按发行人识别。
2. 用户点击“检查官方 IR 材料”发现最近四个已发布季度；SpaceX 目前官网只有一个季度，照实显示。
3. 打开资料后保存正文和原件；可下载固定版原件、复制原文生成带版本及校验值的引用。
4. 图片化演示文件可保存原件，但不宣称已获得正文，不加入 AI 正文选择器；未识别图表需看原件。
5. AI 仍需用户逐次选择材料和确认。GET 不联网、不写库；公开公司材料不包含任何人的私密判断。
6. 特斯拉实时目录在本机、NAS、普通浏览器返回访问拒绝；四份官方季度附件可读取。
   明确保存 2026-09-26 核实的链接快照，显示目录失败、快照日期及无法确认后续季度的限制。
   这不算特斯拉实时目录验收通过，不尝试绕过站点访问控制。
   NAS 上部分 Q4 目录返回 HTTP 429，而官方 CDN 附件可以正常读取；共用的备用目录机制
   扩展至 Alphabet、Amazon、Meta、NVIDIA、SpaceX。链接来自 2026-09-26 的官方接口与页面，
   不是推测文件名。谷歌文字稿使用官网明确链接的 PDF。备用目录不会覆盖以后成功获取的新季度目录，
   不把读取历史附件记成实时目录验收通过，也不会因 429 自动提高频率或重试。
7. 本批不修改既有 SEC 定时任务，不恢复 SEC 验收。尚未创建 IR 的 DSM 自动任务；网页手动检查和命令入口已经提供。

命令示例：

```text
python manage.py sync_research_ir --all-supported --with-content
python manage.py sync_research_ir --company amd --with-content
```

默认命令只检查已有研究档案对应公司；首次批量建立公开公司记录需显式 `--all-supported`。
不会建立任何持仓、交易或成员判断。最近 12 小时已成功目录默认跳过；手动检查仍有两分钟冷却。

## 验收记录

离线测试覆盖发行人别名与跨市场身份、来源与跳转限制、压缩大小限制、官方日期口径、
季度分类、特斯拉降级提示、版本幂等与历史引用、跨成员权限、CSRF、只读 GET、
Office 文件类型、原件保存但无正文时不进入 AI，以及原有投研流程回归。

本地桌面浏览器已走通苹果资料与 PDF 固定引用；以本地隔离 SQLite 数据库执行十三家真实原件归档验收。
本地验收数据库、下载原件与缓存均在 `outputs/`，不提交、不上传覆盖 NAS。

2026-09-26 本地十三家公司当前目录共 196 份材料，193 份可提取正文、3 份微软演示仅保存原件。
备用目录的四份谷歌 PDF 文字稿另行实测提取成功。合并情报模块生产版本 `3b8377b` 后，
`investment_research intelligence.test_programs intelligence.test_program_network` 共 357 项测试通过、1 项跳过。
NAS 最终逐公司验收与实际运行版本另记发布验收记录；本地可用不代表 NAS 实时目录可用。

当前生产基线 `7279ee4` 的 `portfolio.test_review_hardening.FinancialHardeningTests.test_cashflow_conversion_uses_occurrence_or_period_end`
在 2026 年账本年度汇总遇到缺失 HKD 汇率时抛错；已在该精确版本的独立源码副本复现。
本批不改变账本财务口径，该既有问题单独登记，不将整套回归表述为全绿。
