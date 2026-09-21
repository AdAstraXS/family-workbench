# 投研 M2B：Hermes 64K 顺序任务包

基线：`a5204c9f56da5956273960229bc407c81f1a5164`。本文由主代理在隔离分支
`codex/investment-research-sec-explore` 制定；以下 Hermes 任务包保留为原始边界记录，实际进度以本段为准。

**2026-09-21 执行变更：** Hermes 在 B1 遇到 `Context length exceeded: max compression attempts (3) reached`。
按项目 `AGENTS.md` 的一次失败即主代理接管规则，本轮不再次委派或重试。主代理已直接在隔离
分支实现 B1–B3 并进行本地离线验证；下文的 Hermes 提示词保留为原始规格记录，不再作为发送指令。
用户随后选择已配置的云端文本模型和逐次确认。主代理据此继续实现 B4：仅在服务商单独启用投研用途、核对环境变量密钥和费用上限后显示入口；成员每次选择已存 SEC 正文版本并勾选本次发送范围，才可生成只属于自己的带原文引用草稿。草稿不会写入正式判断。此实现仍需本地回归验收；没有配置或调用真实生产模型，也没有部署。

## 要交付的用户流程

1. 已有持仓/判断的公司：成员选一份重要 SEC 资料，按需取得正文；任何研究结论能回到当时的原文版本和准确片段。
2. 新感兴趣的美股：成员仅选证券即可建立私密探索档案；不必编造持有理由。阅读资料后，可由本人确认第一版正式判断。
3. 成员主动选择资料并发起 AI 分析时，得到带来源的探索或判断更新草稿。AI 不能自动改变正式判断。

验收至少使用 MSFT 与另一个非微软美股的离线样例，证明不依赖逐公司 IR 抓取器。不得以“有 SEC 标题/链接”冒充已读取正文，也不得以测试替身冒充真实模型可用。

## 委派及上下文规则

- 四批严格顺序：B1 正文快照 → B2 引用与页面 → B3 探索入口 → B4 AI 草稿。每批由主代理审核并复跑验证后，才从获准提交开始下一批；不要并行写同一工作树。
- 每批使用独立 worktree 与 `codex/` 分支；保护主仓库未提交/未跟踪数据。当前主仓库中的 `docs/investment-research-hermes/` 是未跟踪的旧交接资料，不得清理或覆盖。
- 每次发送给 Hermes 的上下文以本批任务段落、指定源码和必要测试片段为限。建议输入不超过约 48K token，为实现与回传留至少 16K；禁止整仓扫描、粘贴全量 `tests.py`、生产日志或私人 MSFT 笔记。若实际上下文将超过 64K，停止并报告准确的缺失文件，不自行改用更大窗口。
- Hermes 不单独决定架构、权限、模型外发、费用、生产迁移或部署。遇到未写明的高判断问题，列出选项并停止该处，不猜测；其余已写明的机械实现和离线测试继续完成。
- 不访问 NAS，不运行默认 `docker compose up`，不读取 `.env`、密码、备份或用户私人研究材料，不调用真实 AI，不推送、不部署。所有外部 HTTP 在测试中 mock。
- 回传：worktree 绝对路径、分支、基线与 HEAD、改动文件（含未跟踪）、每条验证命令及退出码、未执行项、剩余风险，摘要不超过约 1500 中文字。未运行不得写“通过”。

主代理每批审核权限、来源与版本、安全边界、迁移和用户流程，复跑 `investment_research` 离线测试、`check`、`makemigrations --check --dry-run`、`git diff --check`。最终集成还需按 `AGENTS.md` 跑 `ipo portfolio` 回归，真实浏览器检查桌面/窄屏，并在任何生产操作前读取 NAS 部署技能与 runbook、备份和核对基线。

## B1：SEC 正文的按需获取与不可变快照（首个可执行任务）

**目标。** 对已有档案内已归档的 SEC 10-K、10-Q、8-K，成员点击后端 POST 操作才获取该文件的主 HTML 正文。保存不可变原始响应及可读纯文本版本；再次抓到相同原文不得增加版本。现有“同步 SEC 元数据”仍只同步元数据，不在批量同步中下载所有正文。

**输入文件。** `AGENTS.md`；`app/investment_research/{models.py,source_sync.py,views.py,urls.py,permissions.py}`；`app/investment_research/providers/sec.py`；`app/templates/investment_research/{documents.html,document_detail.html}`；`app/config/{settings.py,settings_research_test.py}`。`tests.py` 只阅读 SEC 客户端、同步和文档视图相关测试段落；迁移只阅读本 app 的 `0002`。可只读查阅 `docs/family-knowledge-product-principles.md` 第 2、6、7 节。工作目录用 Hermes 自己从上述基线创建的隔离 worktree 绝对路径。

**允许修改。** 上述本 app 的服务、客户端、视图、URL、两张模板，以及两份 settings 中仅 SEC 内容大小配置。可新增本 app 迁移、`sec_content.py`、`tests_sec_content.py`。不得改其他 app、依赖、Compose、全局 AI 或项目长期原则。

**冻结契约。**

- 新增 `OfficialResearchContentVersion`：FK 到 `OfficialResearchDocument`；每文档递增版本号；当次取得的来源 URL、原始字节 SHA-256、原始 HTML 的 gzip 字节、规范纯文本、纯文本 SHA-256、提取器版本、获取时间。`(document, version_number)` 唯一。原始字节有界，不能把未压缩超大正文无限存入数据库。旧版本只追加，不提供网页修改/删除。
- `OfficialResearchDocument.content_text/content_sha256/fetched_at` 保持现有“当前可读正文”兼容语义；仅在成功解析并保存版本后更新。原文哈希未变时不建新版本；新版本创建和当前正文更新同一事务完成。微软 IR 原有路径不因新模型改变。
- 仅允许数据库中 `source=sec`、类型为 10-K/10-Q/8-K、属于档案证券的文档。获取 URL 必须由已存 CIK、accession、primary_document 经现有 `filing_url()` 重建，并与官方 HTTPS Archives URL 匹配；不接受用户 POST URL。禁止通过重定向离开 SEC 官方主机或 Archives 路径。沿用明确 User-Agent、限速、有限重试、超时与响应体上限；另设独立的正文大小上限，超限显示短错误。
- 本批只支持主 HTML 文件；PDF、非 HTML、空正文、解码失败、无法可靠识别正文时显式失败并保留既有版本。跳过脚本、样式、隐藏的 inline XBRL 标签等非可读内容；保留章节/段落换行和表格行的可读顺序，不虚构表格数字。错误及日志不保存完整响应、凭据或正文。
- 页面只向本档案 owner 提供“提取这份正文”POST；viewer 不可执行，匿名/未绑定/停用按既有规则处理，其他成员档案/文档 ID 返回 404。GET 不访问网络、不写库。页面显示“仅元数据 / 已有正文 / 提取失败”，失败可重试；保留官方原文链接。
- 同步元数据后，已有 SEC 正文不得被清空；失败不覆盖旧正文。重复 POST 相同正文不产生新版本。并发对同一文件用事务锁或等价唯一约束保持单一版本序列。

**验收。** 离线模拟 10-K、10-Q、8-K 的 HTML；至少一份含标题、表格、脚本和隐藏标签。验证源站重定向越界、非官方 URL、过大、PDF/非 HTML、重复、内容更正、失败保留旧版本、并发/冲突路径、权限及 GET 无副作用。运行：

```text
cd app
python manage.py test investment_research --settings=config.settings_research_test
python manage.py check --settings=config.settings_research_test
python manage.py makemigrations --check --dry-run --settings=config.settings_research_test
git diff --check
```

若现有测试预期“SEC 永远无正文”，只调整受新 POST 行为影响的断言；元数据同步测试仍必须证明批量同步不下载正文。完成后停止，等待主代理审核，不自行开始 B2。

## B2：精确引用和原文对照（B1 审核后执行）

**目标。** 从 B1 的不可变纯文本版本生成稳定可回查的片段引用。研究页面能点击引用回到同一版本的原文位置；正文更新后旧引用仍指向旧版本，不静默漂移。

**输入/允许修改。** 只读 B1 已通过的模型、服务、模板及相关测试；可改本 app 的 `models.py`、`views.py`、`urls.py`、`permissions.py`、文档相关模板；可新增迁移、`citations.py`、`tests_citations.py`。不改 SEC 抓取器和其他 app；发现 B1 缺陷先报告，由主代理决定返修。

**冻结契约。** 引用标识为内容版本 ID + 半开字符区间 `[start,end)` + 引文文本哈希；服务层检查区间、长度上限及文本匹配，服务端从版本正文生成展示片段，不信任客户端传入引文。可用版本正文字符偏移作为首版定位，不造不可核查的 PDF 页码或公司原站锚点。原文详情支持 `?version=<id>&start=<n>&end=<n>`，只接受属于当前档案证券/文档的版本；模板安全转义且突出片段，含前后文。URL 参数伪造、跨成员访问、过期/不存在版本不泄露内容。B1 的任何新版本不改旧片段目标。

**验收。** 测试中创建 v1 引用后获取 v2，回看仍出现 v1 原句；位置错误、越权、恶意 HTML、长区间被拒绝；GET 无写入/外部请求。运行 B1 相同四条离线检查。完成后停止。

## B3：无需先写判断的探索入口（B2 审核后执行）

**目标。** 成员选择现有美股 `Security` 就能建立私密“正在了解”档案，在里面查看官方资料；有了自己的判断后，明确保存首版正式判断。原有“直接创建正式档案”路径与版本历史保持兼容。

**输入/允许修改。** 本 app 的 `models.py`、`services.py`、`forms.py`、`views.py`、`urls.py`、`permissions.py`；首页、新建、详情、编辑、历史模板及相关测试；可新增迁移和 `tests_exploration.py`。不改证券、持仓、知识或全局 AI 模型，不重做页面框架。

**冻结契约。** 一证券/owner 仍只有一份 `ResearchDossier`。探索态明确由 `current_revision is None` 表示，`initial_thesis` 为空；如需让 Django 字段校验允许空串，迁移只调整该字段 `blank`，不得放宽已有正式判断的非空校验。新建探索档案的服务以当前登录 member 为 actor，从后端赋 family/owner；直接正式创建仍沿用现有事务与首版规则。探索态可以看资料，但不在页面伪装成已形成判断；列表显示“正在了解”。点击“保存第一版判断”时服务锁档案，校验 owner/family、非 viewer、判断及列表字段，原子建立 revision 1、设置 `initial_thesis` 与 `current_revision`。重复/过时提交只能成功一次，后续走原有追加版本路径。探索中选定的 SEC 正文版本不因此丢失；不引入新的自选股表或自动持仓关联。

**页面。** 首页同时给出“先了解一家公司”和“已有判断，直接建档”两个清楚入口；探索表单只要求标的。已有相同标的档案跳转本人档案。详情主线为“先看官方资料 → 形成自己的判断”，来源管理放次级；桌面与窄屏均能完成。私密提示、空状态、错误、viewer 只读、历史无版本的显示均需覆盖。

**验收。** 新标的无持仓、无假设也能创建；同证券重复、家庭管理员/superuser 非 owner、viewer、匿名/停用、POST 篡改、两个窗口同时提交首版、旧 M1 直接创建与修改回归。GET 不下载、不写库。运行 B1 相同四条离线检查。完成后停止。

## B4：成员主动发起的带引用 AI 草稿（由主代理直接实现）

**目标。** 探索态生成“先了解”草稿；已有正式判断时生成“这份资料如何支持/削弱/不足以判断”草稿。仅使用成员明确选择、已有不可变内容版本且仍可访问的资料。草稿和正式 `ResearchThesisRevision` 分开，必须由成员编辑/确认后才保存判断。

**交付边界先固定。** 输出至少包含：核心事实、支持与反证、证据不足处、待验证问题、引用、资料覆盖范围/截止时间。引用只允许返回本次提供的内容版本和片段，并由服务端逐条验证；无效引用不能作为证据展示。无资料正文、模型未配置、超时/费用上限/格式错误应显示明确失败，不回固定答案。请求记录只存必要的范围、模型与提示词版本、状态、用量；不在日志或数据库 `sanitized_input` 中复制私密全文。POST 触发；GET 只读。默认仅本人可见；云端发送私密判断前必须有本次明确确认及范围展示。AI 不拥有 ORM 写入、shell、交易或任意 URL 工具。

**实际调用契约。** 复用 `AiProvider`、`AiAnalysisRequest`、`AiAnalysisResult`，但不启用未实现的通用 `ai_analysis.services`。服务商 `extra_data` 必须显式设置 `allow_research_analysis=true`、`research_policy_version=research-document-v1`、`research_policy_reviewed_on`、`api_key_env_var`、`research_max_input_chars`、`research_max_output_tokens`、`research_input_usd_per_million`、`research_output_usd_per_million`、`research_max_estimated_usd`；API Key 仅从环境变量读取。这些值需在生产启用前按实际模型价格、数据处理策略核对。页面只列出策略完整且 Key 已配置的模型。正文最多发送前 16000 字，长文显式标为部分覆盖；输出引用只能指向本次提供的 300 字片段，服务端验证版本、位置和哈希。模型推论仍需成员逐条对照原文，不等于已核实的投资事实。现阶段不支持 IR 正文、多个文件合并分析或整份超长 10-K 自动逐章覆盖。

## 每批交回本任务的最短消息

```text
Hermes 已完成 M2B-B<序号>，请审核。
Worktree：<绝对路径>
报告：<绝对路径>
分支 / HEAD：<实际值>
主要限制：<没有则写无>
```

主代理收到后直接读 diff、未跟踪文件和报告，复跑关键验证，给出通过或需修复的具体清单；未通过不合并、不进入下一批。生产部署是独立决策，不包含在 Hermes 任务中。
