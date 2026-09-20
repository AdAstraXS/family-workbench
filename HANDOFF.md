# M2-A Hermes 交接状态

日期：2026-09-20。

## M2A-5.1 手动同步按钮（主代理，2026-09-20；优先于下方记录）

在档案详情页和官方资料页新增“立即同步”按钮。按钮只对当前档案所有者显示，使用 CSRF 保护的 POST 请求，按当前证券同步 SEC 与 Microsoft IR；同步完成后跳转资料页，并提示新增、更新、无变化和失败数量。viewer、跨档案请求和 GET 均拒绝，不会因打开页面自动联网。

新增 1 个 CSRF 测试（并补充 viewer 页面不显示按钮断言）。按钮改动前的完整投研测试为 **235/235 OK**；新增测试尚待容器环境恢复后复跑。桌面和窄屏浏览器验收通过。此次仅改视图、路由、模板和测试，无模型或迁移变化。

下一步：提交此网页改动并部署到 NAS；部署前复用既有安全流程创建数据库备份，部署后检查按钮页面和生产数据基线。

## M2A-5 最终完成（主代理，2026-09-20；优先于下方记录）

M2A 官方资料归档第一阶段已完成。分支仍为 `codex/investment-research-m2a`，HEAD 仍为 `a6de59a53b96af11e2b569152de2a217905dfb29`，M2A-1 至 M2A-5 全部成果保留为未提交 diff；未访问 NAS、未部署、未写生产数据库。

实现：
- 档案详情页新增“官方资料”摘要，显示归档数量、最近成功同步时间及需要关注的来源数。
- 新增档案内官方资料列表和资料详情两个只读页面；资料只按档案证券读取，跨档案、跨家庭、管理员和 superuser 均不能绕过 owner 权限。
- 列表展示来源状态、短错误、类型、发布日期、报告期和正文提取状态；详情页展示已提取纯文本，原文链接仅指向已归档官方 URL。
- 所有 GET 页面只查本地数据库，不调用 SEC/Microsoft IR，不触发同步或写入；正文经 Django 自动转义。
- 新增 11 个页面测试，覆盖匿名、未绑定/停用成员、viewer、跨档案隔离、证券范围、转义、排序、分页、空态、状态、GET 无副作用和 POST 405。

验证：
- 离线全模块测试 **231/231 OK**；`check`、`makemigrations --check --dry-run`、`git diff --check` 全部通过，仅有 knowledge 既有配置警告。
- 桌面及窄屏浏览器验收通过：档案入口、来源状态、资料卡片、元数据正文页显示正常；窄屏无横向溢出。
- 浏览器使用临时 SQLite 和合成 MSFT 数据，没有访问外部来源；临时服务已停止。

详细报告：`outputs/cloud-m2a-validation/M2A5-FINAL-REVIEW.md`。总交接：`REVIEW-RETURN-M2A.md`。

精确下一步：主代理进行提交前范围审计，只暂存 M2A 源码、迁移、测试和长期文档，排除 `outputs/`、交接日志及临时预览文件；随后提交并在得到明确部署指令后，按 NAS 部署技能先备份和核对生产基线，再部署精确 commit。AI 摘要与自动关联不属于本阶段，不应顺带开始。

## M2A-4 最终完成（主代理接管截断会话，2026-09-20；优先于下方记录）

Hermes 在写完 M2A-4 代码与 17 个新测试后因上下文超限中断，尚未执行验证或更新交接。主代理已接管、审查和修复，M2A-4 现已通过。分支仍为 `codex/investment-research-m2a`，HEAD 仍为 `a6de59a53b96af11e2b569152de2a217905dfb29`，所有成果未提交；未访问 NAS、未联网同步、未写生产数据库。

实现：
- `source_sync.py`：两个来源统一返回 `created/updated/unchanged/failed`；只比较业务字段，单独刷新 `fetched_at` 计 unchanged；新增 `sync_research_sources()`，只选择已有 dossier 的不同证券，逐证券/来源隔离执行，Microsoft IR 非 MSFT 计 skipped，汇总五类计数。
- `management/commands/sync_research_sources.py`：新增 `--symbol`、`--source`、`--max-documents`、`--fail-on-error`，终端仅输出证券/来源/短原因与计数。
- `tests.py`：新增编排与命令测试，并更新旧同步计数断言。

主代理修复：旧测试未适配 unchanged；命令 fixture 给不同证券复用了同一 accession；只有 MSFT 时错误期待 skipped；证券过滤原用大写输入配合区分大小写的 `symbol__in`，现改为数据库侧 `Upper(symbol)` 比较，并用混合大小写存储测试覆盖。

验证：全模块 **220/220 OK**；命令 help 正常；`check`、迁移一致性、`git diff --check` 均通过，仅有 knowledge 既有警告。详细报告：`outputs/cloud-m2a-validation/M2A4-FINAL-REVIEW.md`。

精确下一步：M2A-5 是页面与视觉判断工作，按 `AGENTS.md` 应由主代理直接实现和浏览器验收，不再交给 Hermes。范围仅为档案内官方资料只读页面，不联网、不触发同步、不做 AI。

## M2A-3 最终复审通过（主代理，2026-09-20；优先于下方记录）

主代理已复查并修复 M2A-3 的真实页面兼容与客户端边界问题，可以进入 M2A-4。分支仍为 `codex/investment-research-m2a`，HEAD 仍为 `a6de59a53b96af11e2b569152de2a217905dfb29`，全部成果未提交；未访问 NAS、未写生产数据库。

主代理修复：query/fragment 统一移除并将财报路径规范成小写 canonical URL，避免同一财报因 UTM 或路径大小写重复；只采用首个 HTML head title，避免隐私 SVG title 污染；真实页面无 time/meta 时从 Microsoft 固定 `REDMOND, Wash. — 日期 —` 发布行解析日期；畸形端口统一映射 `MicrosoftIRUrlError`；拒绝 bool/无穷 timeout 与非法正文上限；响应体读取阶段的超时/断线统一映射 connector 异常。新增 6 个针对性测试，fixture 改为真实 dateline 结构。

验证：
- 全模块离线测试：**203/203 OK**；`check`、迁移一致性、`git diff --check` 均通过，仅有 knowledge 既有警告。
- 真实 Microsoft IR 小流量验收通过：当前首页发现 FY26 Q4，标题 `FY26 Q4 - Press Releases - Investor Relations - Microsoft`，发布日期 `2026-07-29`，正文 26,348 字符且含营收句；使用该真实快照在 SQLite 内存库首次同步创建 1 条、重复同步仍为 1 条。

详细结论：`outputs/cloud-m2a-validation/M2A3-FINAL-REVIEW.md`。精确下一批：将主仓库 `docs/investment-research-hermes/M2A-4-START.md` 交给全新 Hermes 会话，只做统一同步编排与管理命令，完成后停止。

## M2A-3 完成记录（Hermes，待主代理复审；优先于下方记录）

本批只实现 MSFT 专用 Microsoft IR connector、正文纯文本提取、幂等同步函数与全 mock/fixture 离线测试；未开始 M2A-4。分支 `codex/investment-research-m2a`，HEAD 仍为 `a6de59a53b96af11e2b569152de2a217905dfb29`，M2A-1/2 未提交成果全部保留；未 commit/push、未访问 NAS、未真实联网。

修改文件：
- `app/investment_research/providers/microsoft_ir.py`：新增（约 360 行）。`MicrosoftIRClient`（固定入口 `https://www.microsoft.com/en-us/investor/`、官方 host 白名单 www.microsoft.com/microsoft.com、opener 可注入、UA 固定、超时/响应体上限/正文上限走 settings）；`normalize_url`（HTTPS+白名单、去 fragment 保 query、拒绝 userinfo/非 443 端口/协议相对外站/跳外站、裸域归一化为 www）；`discover_earnings_links`（`/en-us/investor/earnings/fy-YYYY-qN/press-release-webcast` 模式，重复/查询变体去重，非财报链接忽略）；`extract_earnings_page`（HTMLParser 纯文本：跳 head/script/style/nav/noscript/iframe/svg/template，`<title>` 优先采集，`<time datetime>` 与 meta published_time 取日期，空正文 → `MicrosoftIREmptyBodyError`）；异常族 `MicrosoftIRError` 及 Config/Url/HTTP/Network/Timeout/ResponseTooLarge/Parse/EmptyBody 子类；`document_type_for_path`（press-release-webcast→earnings_release、webcast/earnings-call→earnings_call、news/announcement→investor_update、else→other）。
- `app/investment_research/source_sync.py`：追加 `sync_microsoft_ir_documents(security, client, now=None)`——首页发现 → 逐页抓取，按 `(source, external_id)` 幂等 upsert，sha256 变化才更新 content_text，单文档失败保留旧正文与 `last_success_at`、记截断 `last_error`；首页失败/零链接整体抛 `MicrosoftIRSyncError` 并记 state。
- `app/config/settings.py`：新增 `RESEARCH_MICROSOFT_IR_TIMEOUT_SECONDS`(15)、`RESEARCH_MICROSOFT_IR_MAX_RESPONSE_BYTES`(5MB)、`RESEARCH_MICROSOFT_IR_MAX_CONTENT_CHARS`(200000)，env 可覆盖。
- `app/config/settings_research_test.py`：对应离线测试值（timeout=2s、max_bytes=1MB、max_chars=200000，无真实凭据）。
- `app/investment_research/testdata/microsoft_ir_home.html`、`microsoft_ir_earnings.html`：新增 fixture（首页含有效/重复/查询变体/外部/非财报链接；earnings 页含 title、`<time>`、噪声标签与正文段落）。
- `app/investment_research/tests.py`：import 追加 `hashlib`/`Path`/provider 符号与 `SOURCE_MICROSOFT_IR`；追加 5 个测试类共 **39 个用例**（MicrosoftIRUrlNormalizationTests 9、MicrosoftIRDiscoveryTests 4、MicrosoftIRExtractionTests 7、MicrosoftIRClientTests 10、MicrosoftIRSyncServiceTests 9），全部 mock opener/fixture，无真实网络。

命令与退出码（均 `--network none`，Docker `nas-ai-ai-api-nas-web:latest` 只读挂载 `app/`）：
- `manage.py test investment_research --settings=config.settings_research_test` → exit 0，**Ran 196 tests, OK**（既有 157 + 新增 39）。
- `manage.py check --settings=config.settings_research_test` → exit 0（仅 2 条既有 knowledge 警告）。
- `manage.py makemigrations --check --dry-run --settings=config.settings_research_test` → exit 0，`No changes detected`。
- `git diff --check` → exit 0。

日志：`outputs/hermes-m2a-validation/m2a3-test.log`、`m2a3-check.log`、`m2a3-migrations.log`、`m2a3-git-diff-check.log`。

未验证项：
- SQLite 内存库，未验证 PostgreSQL 并发；未真实访问 `microsoft.com`（真实页面结构、CDN 跳转行为、TLS 证书链由主代理后续单独验收）。
- webcast 页 ≠ transcript 的语义区分仅按路径映射，未核对真实站点；正文上限 200000 字符为规格默认值，未压测。
- 未开始 M2A-4 统一同步编排与管理命令。

精确下一批：`docs/investment-research-hermes/M2A-4-SYNC-COMMAND.md`（统一同步编排与管理命令）。主代理复审本批通过后，把 `M2A-4` 交接包提示词复制给全新 Hermes 会话，只执行 M2A-4，完成后停止。

## M2A-2 最终复审通过（主代理，2026-09-20；优先于下方记录）

主代理已复查修复后的 `sec.py`、`source_sync.py` 与针对性测试，M2A-2 离线契约通过，可以进入 M2A-3。分支仍为 `codex/investment-research-m2a`，HEAD 仍为 `a6de59a53b96af11e2b569152de2a217905dfb29`，M2A-1/2 全部成果保持未提交；未访问 NAS、未写生产数据库。

主代理复跑结果：
- 全模块 SQLite 离线测试：**157/157 OK**。
- 独立边界审计脚本：**5/5 OK**，覆盖无效配置、连接重置、畸形 SEC 列、首次 CIK 失败状态、跨证券 accession 冲突。
- `check`、迁移一致性检查、`git diff --check`：均通过；只有 knowledge 既有警告。

真实 SEC 小流量验收使用声明应用与联系邮箱的 User-Agent、2 请求/秒，从当前开发机 Docker 出口请求 `company_tickers.json`，首个请求仍被 SEC 返回 HTTP 403；代码正确抛出 `SecHTTPError`，没有解析或数据库写入。该结果表明当前出口未获 SEC 边缘策略放行，**不能宣称真实 SEC 联网已通过**。后续部署前需从 NAS 出口用生产 `RESEARCH_SEC_USER_AGENT` 再验；不要把联系邮箱写入仓库。

详细结论：`outputs/cloud-m2a-validation/M2A2-FINAL-REVIEW.md`。精确下一批：按主仓库 `docs/investment-research-hermes/M2A-3-START.md` 开启全新 Hermes 会话，只做 Microsoft IR connector，完成后停止。

## M2A-2 修复批完成（Hermes，待主代理复审）

修复主代理复审发现的 SEC 边界问题（`M2A2-REVIEW.md`），只改 3 个文件 + 本日志，保留 M2A-1/2 全部未提交成果，未 commit/push、未访问 NAS、未真实联网。

修改文件：
- `app/investment_research/providers/sec.py`：构造时数值配置校验（timeout/max_response_bytes ≤0、rate_limit ≤0 或 >5、max_retries 非整数/负、backoff <0 → 统一 `SecConfigError`，不静默钳制）；`_get` 将 `ConnectionResetError` 等 `OSError` 包装为 `SecNetworkError`（保持 HTTPError/TimeoutError/URLError 既有分类顺序，错误不含响应正文）；`parse_recent_filings` 要求全部 REQUIRED_FILING_COLUMNS 为 JSON list（None/str/dict/数字 → `SecFilingsParseError`，再查等长）；`filing_url` 加固：拒绝 query/fragment/控制字符/百分号编码路径穿越（unquote 后查 `""`/`.`/`..` 段），正常 primaryDocument 文件名仍通过，无第三方依赖。
- `app/investment_research/source_sync.py`：`sync_sec_documents` 入口校验 `max_documents` 为 1..100 整数（bool 拒绝），非法值在创建 state/调用 client 前抛 `SecSyncError`；CIK 首次解析失败也更新已有/新建 state 的 `last_checked_at` 与截断 `last_error`、保留 `last_success_at` 与既有文档后重抛原 `SecClientError`；同一 `(source, external_id)` 已存在但 security 不同 → 计 failed、写短错误、保留原 `document.security`；同证券更新时 `update_fields` 不再含 `security`。
- `app/investment_research/tests.py`：追加 5 个正式测试类共 **37 个用例**（SecClientConfigValidationTests 15、SecClientOSErrorWrapTests 5、SecFilingsColumnTypeTests 5、SecFilingUrlHardeningTests 6、SecSyncBoundaryRepairTests 6）；`_make_client` 默认 rate_limit 10000→5.0，503 重试测试的 sleep 断言改为过滤节流 sleep。

命令与退出码（均 `--network none`，Docker 只读挂载 `app/`）：
- `manage.py test investment_research --settings=config.settings_research_test` → exit 0，**Ran 157 tests, OK**（既有 120 + 新增 37）。
- 主代理复现脚本 `outputs/cloud-m2a-validation/m2a2_review_checks.py` → **5/5 通过**（exit 0；正式验收以 tests.py 为准）。
- `manage.py check` → exit 0（2 条既有 knowledge 警告）；`makemigrations --check --dry-run investment_research` → exit 0；`git diff --check` → exit 0。

日志：`outputs/hermes-m2a-validation/m2a2-repair-test.log`、`m2a2-repair-review-script.log`、`m2a2-repair-check.log`、`m2a2-repair-makemigrations-check.log`、`m2a2-repair-git-diff-check.log`。

下一步：主代理复审本批；通过后按 `M2A-3-MICROSOFT-IR.md` 进入 M2A-3。

## 最新复审状态（主代理，优先于下方记录）

M2A-2 现有 120 个模块测试由主代理复跑通过，但独立边界测试复现 5 个缺口：数值配置未校验、连接重置未包装、畸形列泄漏 TypeError、CIK 解析失败未记录状态、accession 冲突会静默改挂证券。暂不进入 M2A-3。详细报告：`outputs/cloud-m2a-validation/M2A2-REVIEW.md`。

## M2A-2 完成记录（Hermes，待主代理复审）

本批只实现 SEC 客户端、SEC 单证券元数据同步服务、必要测试设置和离线测试；未开始 Microsoft IR、正文抓取、统一管理命令、页面或 AI。

修改文件（保留未提交 diff，含 M2A-1 全部未提交成果）：
- `app/investment_research/providers/__init__.py`：新增包（docstring 约定：客户端只管网络与解析，不写 DB；opener/clock/sleeper 可注入）。
- `app/investment_research/providers/sec.py`：新增。`SecClient`（HTTPS+官方主机白名单、UA 必配、429/503 有限线性退避重试、响应体上限、错误信息不含完整正文）、异常族（`SecClientError` 及 7 个子类）、`parse_recent_filings`（列式 JSON，只留 10-K/10-Q/8-K 含 /A）、`filing_url`（拒绝外部 URL/绝对路径/穿越/空文件名/非法 accession，CIK 去零填充）。
- `app/investment_research/source_sync.py`：新增。`sync_sec_documents(security, client, max_documents=20)`：仅 US+stock；CIK 缓存于 `ResearchSourceState.external_company_id`；按 accession 幂等 upsert；content_text 保持空；单文档失败不删已成功文档；致命拉取错误记 state 并保留既有 success/文档；last_error ≤2000 字符。
- `app/config/settings.py`：新增 `RESEARCH_SEC_USER_AGENT/TIMEOUT_SECONDS/MAX_RESPONSE_BYTES/RATE_LIMIT_PER_SECOND/MAX_RETRIES/BACKOFF_SECONDS`（env 可覆盖，默认 10s/10MB/5次/3次/0.5s）。
- `app/config/settings_research_test.py`：新增对应离线测试值（无真实凭据）。
- `app/investment_research/tests.py`：顶部 import 追加 json/urllib.error/date/timedelta/SimpleTestCase/timezone 与新模块；追加 4 个测试类共 **40 个用例**（SecClientTickerTests 15、SecFilingsParseTests 8、SecFilingUrlTests 7、SecSyncServiceTests 10），全部 mock opener/stub client，无真实网络。

命令与退出码（均 `--network none`，Docker 只读挂载 `app/`）：
- `manage.py test investment_research --settings=config.settings_research_test` → exit 0，**Ran 120 tests, OK**（既有 80 + 新增 40）。
- `manage.py check --settings=config.settings_research_test` → exit 0（仅 2 条既有 knowledge 警告）。
- `manage.py makemigrations --check --dry-run investment_research --settings=config.settings_research_test` → exit 0，`No changes detected`。
- `git diff --check` → exit 0。

日志：`outputs/hermes-m2a-validation/m2a2-test-sec.log`、`m2a2-check.log`、`m2a2-makemigrations-check.log`、`m2a2-git-diff-check.log`。

未验证项：
- SQLite 内存库，未验证 PostgreSQL 并发；无生产联网验收（真实 SEC 接口行为、rate-limit 头、MSFT 实际 submissions 结构由主代理后续单独执行）。
- 正文抓取（content_text/sha256）、Microsoft IR、统一命令、页面、AI 均未开始。
- `resolve_cik` 每次同步都拉全量 company_tickers.json（未做进程内缓存），M2A-4 统一命令时再优化。

## 下一步

精确下一批：`M2A-3-MICROSOFT-IR.md`（Microsoft IR 站点发现、正文抓取与离线测试）。把交接包 `M2A-3-START.md` 提示词复制给 Hermes，只执行 M2A-3，完成后停止。

## 固定身份

- 工作树：`C:/Users/Administrator/Documents/Codex/2026-06-16/nas-ai-ai-api-nas/outputs/hermes-investment-research-m2a`
- 分支：`codex/investment-research-m2a`
- 起始 HEAD：`a6de59a53b96af11e2b569152de2a217905dfb29`（本批未 commit，HEAD 不变）
- 当前批次：**M2A-2 已完成（待复审）**，下一步 M2A-3 Microsoft IR。

## 使用方式

每批使用全新 Hermes 会话。先读取工作树 `AGENTS.md`、本文件，以及主仓库交接包中的 `M2A-README.md`、`M2A-SPEC.md` 和当前批次文件。不要恢复旧聊天或一次执行全部批次。

完成每批后覆盖更新本文件顶部状态，控制在约 1,000 中文字：实际文件、命令与退出码、日志、未完成项、精确下一批。保留未提交 diff，不 commit/push，不访问 NAS。

## M2A-1 完成记录

本批只建立两个模型、迁移和模型级测试，未做来源客户端、SEC/微软解析、同步命令、页面或 AI。

修改文件（保留未提交 diff）：
- `app/investment_research/models.py`：新增 `OfficialResearchDocument`、`ResearchSourceState` 及模块常量 `SOURCE_CHOICES`、`DOCUMENT_TYPE_CHOICES`。严格按 M2A-SPEC：`security` FK PROTECT、`(source, external_id)` 唯一约束、`(security, -published_at)` 与 `(source, document_type)` 索引、`metadata`/`cursor` 用 `default=dict`、`ordering=["-published_at","-pk"]`、`__str__` 不发网络。
- `app/investment_research/migrations/0002_officialresearchdocument_researchsourcestate.py`：新增，仅含 investment_research 两个模型变更，依赖 `0001_initial` 与既有 portfolio 迁移。
- `app/investment_research/tests.py`：仅追加 `OfficialResearchDocumentModelTests`、`ResearchSourceStateModelTests` 两个测试类（19 个用例）；顶部 import 增加 `IntegrityError/transaction` 与新模型/常量。

命令与退出码（均 `--network none`，Docker 只读挂载 `app/`）：
- `manage.py test investment_research --settings=config.settings_research_test` → exit 0，**Ran 80 tests, OK**（既有 61 + 新增 19）。
- `manage.py check --settings=config.settings_research_test` → exit 0（仅 2 条既有 knowledge.W001/W002 警告，与本批无关）。
- `manage.py makemigrations --check --dry-run --settings=config.settings_research_test` → exit 0，`No changes detected`。
- `git diff --check` → exit 0，无空白错误。

日志：`outputs/hermes-m2a-validation/`（`test-investment-research.log`、`check.log`、`makemigrations-check.log`、`git-diff-check.log`）。

未验证项：
- SQLite 内存库，未验证 PostgreSQL 并发；无生产联网验收（由主代理后续用公开 MSFT 材料单独执行）。
- `source_url` 的 HTTPS 官方域名校验、`last_error` 2000 字符截断属服务层职责，本批未实现。
- 未开始来源客户端（SEC/微软）。
