# 家庭知识底座第一版配置与运行手册

> 更新日期：2026-08-02
>
> 适用范围：家庭知识底座第一版的本地验证、Microsoft 配置、任务运行和生产部署前检查
>
> 产品与验收基线：[家庭知识底座架构决策与第一版验收标准](family-knowledge-base-architecture.md)

## 1. 当前实现边界

第一版代码提供：

- 每名家庭成员独立绑定自己的 Microsoft 账户；
- 从 OneNote 到 NAS 的只读、单向同步；
- 选择一个试点笔记本，并设置家庭可见或仅自己可见；
- 分区和页面层级、原始 HTML、正文图片、正文超链接、来源链接和不可变版本；
- OneNote 笔记本、分区和页面层级只作为来源位置保存，不自动成为内容分类；人工或 AI 确认分类后，
  后续同步只更新原始位置，不覆盖人工整理结果；
- 任务队列、单项错误、幂等重跑、失败重试、安全取消和来源删除对账；
- 不可信 HTML 净化、文件类型与文件头校验、受保护附件下载；
- AI 摘要、标签和分类建议、版本绑定、人工修改/接受/拒绝和批量确认预览；
- 现有随手记的可重建搜索投影；
- 按成员、来源、范围、状态和标签浏览及搜索。

第一版代码不包含 OneNote 回写、网页通用采集、人物动态、在线阅读、交易日志、财务报告发布、
向量数据库或 RAG 问答。

## 2. Microsoft 应用注册

OneNote API 不支持应用级后台身份，必须由每名家庭成员完成委托授权。第一版使用 Microsoft
官方 MSAL Python 和 OAuth 2.0 授权码流程。

在 Microsoft Entra 管理中心创建应用注册：

1. 按家庭实际账户类型选择支持的账户。需要同时支持个人 Microsoft 账户和工作/学校账户时，
   选择相应的多租户加个人账户类型，并使用 `common` Tenant。
2. 添加 **Web** 重定向 URI，必须与生产环境完全一致，例如：

   ```text
   https://家庭工作台域名/knowledge/microsoft/callback/
   ```

3. 添加 Microsoft Graph **委托权限**：

   ```text
   Notes.Read
   User.Read
   ```

   MSAL 在授权码流程中管理登录和离线访问所需的 OIDC 保留范围。不要添加 OneNote 写入权限。

4. 创建 Web 应用 Client Secret。它只保存到 NAS 的 `.env`，不进入数据库、Git、日志或页面。

官方参考：

- [Microsoft OAuth 授权码流程](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow)
- [Microsoft Graph OneNote API](https://learn.microsoft.com/en-us/graph/api/resources/onenote-api-overview?view=graph-rest-1.0)
- [OneNote 内容与分页](https://learn.microsoft.com/en-us/graph/onenote-get-content)

## 3. 环境变量

在 `.env` 中配置：

```dotenv
KNOWLEDGE_MICROSOFT_CLIENT_ID=应用ClientID
KNOWLEDGE_MICROSOFT_CLIENT_SECRET=应用ClientSecret
KNOWLEDGE_MICROSOFT_TENANT=common
KNOWLEDGE_MICROSOFT_REDIRECT_URI=https://家庭工作台域名/knowledge/microsoft/callback/
KNOWLEDGE_TOKEN_ENCRYPTION_KEY=Fernet密钥
KNOWLEDGE_FILE_ROOT=/app/knowledge_files
KNOWLEDGE_MAX_RESOURCE_BYTES=26214400
```

生成独立 Fernet 密钥：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

要求：

- 生产环境必须明确设置加密密钥；`DEBUG=True` 下的派生开发密钥只用于本机开发。
- 加密密钥与数据库备份配套保存，但使用独立的安全位置；丢失后不能恢复已有 Microsoft 连接。
- `knowledge_files/` 不通过 `/media/` 公开，只能经过登录和文档权限检查后下载。
- 单文件默认最大 25 MB；修改限制前先核对 NAS 容量、反向代理和备份时间。

## 4. AI 配置与成员同意

知识整理使用 Django 后台已有的“AI 服务商”配置。文本服务商要求：

- `provider_type` 为 `openai` 或 `openai_compatible`；
- `model_name` 为可用文本模型；
- `base_url` 为公网 HTTPS 的 OpenAI 兼容接口；
- API Key 只通过环境变量提供，并在 `extra_data.api_key_env_var` 填写环境变量名称；
- `extra_data.usage` 不得为 `ipo_image_recognition`；图片识别模型不会被知识正文整理复用；
- 建议在 `extra_data.data_retention_policy` 和 `extra_data.knowledge_cost_limit` 中填写发送确认页展示的
  服务商留存政策与费用上限。

单篇与批量 AI 整理统一在“待整理”发起，并且任务必须记录成员明确选择的文档 ID。首次发送时
成员可以仅授权本次所选正文，也可以开启来源级持续授权；持续授权不会自动处理整个来源。同步、
浏览和搜索不依赖云端 AI 授权。AI 审计只保存文档 ID、版本哈希、授权范围、字符数、模型、提示词
版本和结果，不在审计表再次复制整篇原文。

## 5. 数据库迁移与索引

依赖安装和镜像构建能力确认后执行：

```bash
docker compose up -d --build
docker compose exec -T web python manage.py migrate
docker compose exec -T web python manage.py rebuild_knowledge_search
docker compose exec -T web python manage.py check
```

`rebuild_knowledge_search` 会删除并重建派生搜索投影，不修改 OneNote 原文、知识版本或随手记
权威记录。指定家庭时使用：

```bash
docker compose exec -T web python manage.py rebuild_knowledge_search --family-id 家庭ID
```

## 6. 网页验证流程

1. 普通家庭成员登录并进入“知识中心 → 来源与同步”。
2. 点击“绑定我的 Microsoft 账户”，核对 Microsoft 同意页只申请读取权限。
3. 授权返回后刷新笔记本列表。
4. 选择一位成员的一个试点笔记本，确认家庭可见或仅自己可见。
5. 初次验证时先不启用来源持续云端 AI 授权，创建同步任务。
6. 在任务页面核对总数、新增、更新、跳过、失败和单项错误。
7. 运行后台处理命令后，抽查层级、正文、图片、网页链接、原始版本和权限。
8. 在知识列表切换“按分类”和“按来源”，确认分区默认分类、原始位置、目录数量和筛选结果；
   人工修改一个分类后再次同步，确认修改没有被 OneNote 分区覆盖。
9. 重复同步，确认未变化内容全部跳过且文档、版本、附件数量不增加。
10. 修改一个 OneNote 页面再同步，确认只新增该页面版本。
11. 完成原文验证后，把代表性资料加入待整理，在文章详情单篇发起或在待整理勾选后批量发起 AI
    整理；确认页核对实际文章、服务商、模型、留存和费用说明，并选择本次或来源持续授权。
12. 任务完成后在待整理的“等待确认”页面对照原文，逐项修改/接受/拒绝；批量确认必须先经过
    预览页。未经成员确认，AI 建议不能进入精选知识。

## 7. 任务命令与 DSM 建议

网页只创建任务，不在 Web Worker 内执行外部同步或 AI 调用。

处理最多五个排队任务：

```bash
docker compose exec -T web python manage.py process_knowledge_jobs --limit 5
```

为所有连接正常的 OneNote 来源创建日常同步任务：

```bash
docker compose exec -T web python manage.py queue_knowledge_syncs
```

创建包含来源删除识别的完整对账任务：

```bash
docker compose exec -T web python manage.py queue_knowledge_syncs --full-reconcile
```

生产环境当前已在 DSM Task Scheduler 配置：

- 任务名称：`family-workbench-knowledge-jobs`（任务 ID 6）；
- 运行身份：`root`；
- 时间：每天 00:00 至 23:55，每 5 分钟一次；
- 命令：在 Web 容器中运行 `process_knowledge_jobs --limit 5`，并追加写入
  `logs/knowledge-jobs.log`。

这个任务只是异步执行器：网页“同步”按钮负责创建任务，执行器负责在下一次轮询时处理；它
不会自动创建 OneNote 同步任务。因此没有用户操作时，轮询只做一次数据库空队列检查，不会
访问 Microsoft Graph。当前没有启用每日自动创建同步任务或每周完整对账；如后续需要自动
同步，再单独启用 `queue_knowledge_syncs` 或 `--full-reconcile`。

同一来源同一任务类型存在排队或运行任务时不会重复创建。任务失败、部分成功或来源不可访问时，
处理命令返回非零状态并保留数据库审计记录。

### 网页收藏（2026-10-02 已部署并完成首轮真实验收）

成员入口为“知识中心 → 网页收藏”。提交公开 HTTP / HTTPS 链接时需明确同意交给
Firecrawl Cloud；可另行选择是否把正文交给已配置的知识整理文本 AI。默认仅自己可见。
同一成员重复提交同一链接只打开原记录，不覆盖备注、可见性或授权。家庭共享只共享归档正文，
收藏备注、错误详情和任务控制记录仍归发起成员；管理员也不能越权读取私人正文、附件或任务。

执行器复用现有 `process_knowledge_jobs --limit 5` 和 DSM 定时任务，不新增队列系统。
网页 POST 只登记并排队，GET 不抓取、不触发 AI、不写数据库。部署时需由受限部署流程把
`KNOWLEDGE_FIRECRAWL_API_KEY` 配入生产运行环境并重建/重启 web 环境；禁止在聊天、数据库、
Git 或运行日志里填写真实密钥。文本 AI 复用现有知识整理默认服务商，无新增 Python 依赖。

每次抓取使用 Firecrawl v2 scrape，保存主体 HTML、原始 HTML、Markdown、原始元数据及最终
链接的受保护 JSON 快照；正文净化后可直接阅读、搜索并进入待整理。普通 PNG/JPEG/GIF/WebP
图片和静态图表（含受支持的内嵌 base64 位图）以独立附件保存在同一 `knowledge_files/` 目录；前端不热链源站图片。
源站提供的作者和带时区发布时间会写入文档，缺少时间或时区时显示未知，不用收藏日期代替。
摘要、分类和标签仍是独立待确认建议，自动处理不会转为精选或覆盖人工结果。

失败处理：

- 抓取失败：不创建空正文；显示失败说明，成员可重试。
- 图片失败：保留正文及对应位置提示，列出未保存图片；“仅补存失败图片”不会再次抓正文或调用 AI。
- AI 失败或正文超过 80,000 字符：原文与已存图片保留，不静默截断；可重试或人工整理。
- 取消/超时：已归档内容保留，旧执行器不得应用迟到的正文或建议；详情提供继续保存及历次任务记录。
- 手动重新抓取：保留旧版本，相同快照复用已有版本；新正文的旧待确认建议失效，已确认人工结果不被覆盖。

抓取只支持公开网页，不提交浏览器 Cookie、密码或登录凭据，不承诺突破付费墙、验证码或平台
反爬；不保存视频或交互图表，不做图片 OCR。正文单次响应上限 12 MB；每页最多 100 张图片，
单图 10 MB，总图片 50 MB，单轮图片保存最多 180 秒。所有本机图片请求校验公网 DNS、固定实际
连接 IP、保留 TLS 主机名验证，并逐跳检查重定向，禁止内网/本机、组播、非常规端口和危险图片类型。
公开链接及网页内容仍会被 Firecrawl 处理，关闭其结果缓存不等同于服务商零数据保留。

发布须应用 `knowledge.0014`，先按 NAS 技能创建验证过的备份，保留生产数据库及知识文件。
上线前需由用户确认部署范围并安全配置 Firecrawl Key；上线后用一篇公开文章实测正文、
普通图片和 AI 待确认流程，核对 DSM 执行器与费用。
接口参数参考 [Firecrawl 官方 scrape 文档](https://docs.firecrawl.dev/api-reference/endpoint/scrape)。

2026-10-02 发布与验收：

- NAS 正式运行提交 `48b21ce3676e27d00942cbe9d39d4e1dcea2937e`，已在用户明确授权后推送项目既有 GitHub 分支；仓库当前为公开仓库，仅推送源码与非敏感文档，密钥与真实数据不入 Git。
- 用户在 NAS 亲自配置新密钥并撤销此前在聊天中出现的旧密钥；此次仅重建 web 容器加载配置，不重建镜像、数据库或 OpenD，不修改 Compose 或额外增加任务。
- 发布前数据库备份经恢复目录校验，旧 app 由受限工具自动备份并验证；精确发布包校验一致。恢复点校验值、配置校验值与业务计数仅保存在本机未跟踪发布记录，不上传 GitHub。
- `knowledge.0014` 迁移成功，静态文件收集、Gunicorn、Django 系统检查正常，内外访问均正常。财务关键基线、最新快照日期与每日估值运行计数未变，生产只增加知识模型迁移及本次成员发起的收藏和 AI 审计数据。
- 既有 `family-workbench-knowledge-jobs`（DSM ID 6）于 21:35 自动领取真实验收文章；正文、原始 JSON 快照及 8 张位图流程图归档到 NAS。浏览器确认 8 张图均由受保护附件路径加载，未热链源站。
- 现有默认知识整理模型 `deepseek-flash` 生成摘要、分类及标签 3 项待确认建议，详情及左右对照入口可用；没有代替用户确认或自动精选。同链接加片段重复提交仍打开原收藏，不新增任务或重复付费调用。
- 源站的 SVG 头图不在支持白名单内，明确显示 1 张图片未保存，任务状态为部分成功；执行器按设计返回非零并保留日志，正文和其他图片仍可读。未对该不支持格式反复重试。Firecrawl 与模型服务的最终费用以各自账单为准。
- 本地 PostgreSQL 157 项回归及桌面／手机验收在发布前通过；生产浏览器完成真实正文、图片和待确认流程检查。当前 Chrome 扩展的尺寸覆盖未实际生效，生产手机尺寸未独立复测，未将桌面截图当作手机验收证据。

## 8. 备份与恢复

专题成果 HTML 由普通成员在“知识中心 → 关注人物 → 上传专题成果”提交，不需要命令行。上传后
系统立即保存不可变原文件和安全展示版本，并按文章标题与日期建立引用映射；成果详情页会列出
已匹配、重复候选和未匹配数量。首次上传默认待人工确认，成员核对后在详情页确认。部署或恢复时，
`KnowledgeArtifact`、版本和引用记录位于 PostgreSQL，原始/展示 HTML 位于 `knowledge_files/`，
两者必须进入同一个恢复点。

交互成果的展示端点使用无同源权限的 sandbox iframe 和独立 CSP。两份金渐成专题成果的展示版本会
自动套用家庭知识中心的浅色青绿色主题并统一无衬线字体；“下载 AI 原文件”仍保持原始样式，便于审计。
不得把上传 HTML 移到公开
`media/`，也不得在 Django 模板中直接使用 `safe` 渲染；原文件下载和成果展示都必须先检查当前
家庭成员权限。

知识底座的同一恢复点必须同时包含：

- PostgreSQL 逻辑备份；
- `knowledge_files/` 原始页面和附件；
- 文件数量、总大小和抽样 SHA-256；
- 实际运行 commit；
- 加密保存且可取得的令牌加密密钥。

搜索投影可以删除后重建，不作为唯一备份。恢复演练至少核对文档数、版本数、附件数、来源数、
搜索权限以及抽样原文和附件哈希。

## 9. 当前生产状态与后续门槛

第一版所需的 `cryptography`、`msal` 和 `nh3` 依赖已完成 NAS 镜像构建和验证。生产环境已配置
独立的知识文件持久目录、Microsoft 应用 Secret 和令牌加密密钥，并完成一名成员、一个笔记本
的真实内容试点。当前运行提交为 `1b2bd784b2ff1fc6a8455eb09229c201eaebe36a`。

后续发布仍必须：

1. 按 NAS 部署技能创建并验证生产数据库备份；
2. 保留生产 `.env`、数据库卷和 `knowledge_files/`，不得用本地数据覆盖；
3. 依赖清单变化时重新验证镜像构建，不得只替换挂载源码；
4. 运行迁移检查、Django 系统检查和原有模块回归测试；
5. 部署后核对任务日志、知识原文、金融关键表基线和实际运行 commit。

## 10. 验证记录

2026-07-30 已完成以下本地验证：

- 知识底座与随手记 30 项自动测试通过；
- 全项目 203 项自动测试通过；
- `manage.py check` 通过，仅保留 Microsoft 应用和生产加密密钥尚未配置的预期告警；
- `makemigrations --check --dry-run` 未发现遗漏迁移；
- 知识中心、搜索、来源管理和待确认页面完成浏览器验证；
- 375 px 宽度下完成移动端布局检查，浏览器控制台无错误。

截至 2026-07-30，本地环境尚不能验证真实 Microsoft OAuth、OneNote 内容、NAS 镜像与 DSM
任务；以下生产试点记录用于补充这些验证，不能把此前的模拟结果当作生产结论。

2026-08-02 已完成以下生产试点：

- 用户本人完成真实 Microsoft OAuth 绑定，选定一个包含 6 页的 OneNote 笔记本；云端 AI
  保持关闭；
- 首轮同步暴露 OneNote 图片偶尔返回 `application/octet-stream` 的兼容问题，修复后任务 #2
  成功完成（更新 1、跳过 5）；
- 修复 OneNote HTML 正文转换后重建 6 个文档，确认正文可以展示；
- 发现 Graph 页面列表的 `lastModifiedDateTime` 可能未随正文及时变化，因此从提交
  `1b2bd784b2ff1fc6a8455eb09229c201eaebe36a` 起，页面 HTML 哈希是增量判断的权威依据，
  列表修改时间只作为描述性元数据；
- 用户修改“金刚经-王路”后创建任务 #5，DSM 执行器自动处理成功：新增 0、更新 1、跳过 5、
  失败 0；未再修改内容后创建任务 #6，自动处理结果为新增 0、更新 0、跳过 6、失败 0；
- 未变化页面会读取一次 HTML 以规避时间戳滞后，但不会下载正文图片、创建知识版本或附件；
- 修复版本全项目 210 项自动测试通过，`makemigrations --check --dry-run` 无遗漏迁移，生产
  `manage.py check` 无问题；
- 部署前数据库备份为
  `backups/family-workbench-pre-onenote-content-hash-1b2bd78.dump`，SHA-256 为
  `3187dbc3a9531e3228370b0cecb4b2286f47406879ca7b90d7bab114c70a156d`；部署前源码恢复包为
  `backups/source-predeploy-a67f34b0f0d7257dd68fcf6b931be29425459d4f-20260802-214151.tar.gz`，
  SHA-256 为 `6df6cca566ab78b8e6d2b3afeaeadefda60a18343484c9a3374c2bb284adf87e`；
- 部署前后生产 `.env` 校验值和金融关键表基线一致，未使用本地数据库覆盖生产数据。

仍待验收：复杂附件与大体量搜索、第二名成员的账户和可见性隔离、授权过期后的重新绑定、
数据库与 `knowledge_files/` 联合恢复，以及用户明确同意后的云端 AI 建议质量。
