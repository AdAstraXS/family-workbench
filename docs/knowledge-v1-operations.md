# 家庭知识底座第一版配置与运行手册

> 更新日期：2026-07-30
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
- API Key 只通过环境变量提供，并在 `extra_data.api_key_env_var` 填写环境变量名称。

来源所有者必须在来源设置中勾选“允许向云端 AI 发送该来源正文”，否则 AI 任务拒绝运行。
同步、浏览和搜索不依赖此项授权。AI 审计只保存文档 ID、版本哈希、字符数、模型、提示词版本和
结果，不在审计表再次复制整篇原文。

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
5. 初次验证时先不勾选云端 AI，创建同步任务。
6. 在任务页面核对总数、新增、更新、跳过、失败和单项错误。
7. 运行后台处理命令后，抽查层级、正文、图片、网页链接、原始版本和权限。
8. 重复同步，确认未变化内容全部跳过且文档、版本、附件数量不增加。
9. 修改一个 OneNote 页面再同步，确认只新增该页面版本。
10. 完成原文验证后，如家庭成员同意，再开启云端 AI 并创建整理任务。
11. 在“待确认”页面对照原文，逐项修改/接受/拒绝；批量操作必须先经过预览页。

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

试点人工验收通过后，可在 DSM Task Scheduler 建议配置：

- 每日固定时间运行 `queue_knowledge_syncs`；
- 每 10 分钟运行 `process_knowledge_jobs --limit 5`；
- 每周运行一次 `queue_knowledge_syncs --full-reconcile`。

同一来源同一任务类型存在排队或运行任务时不会重复创建。任务失败、部分成功或来源不可访问时，
处理命令返回非零状态并保留数据库审计记录。

## 8. 备份与恢复

知识底座的同一恢复点必须同时包含：

- PostgreSQL 逻辑备份；
- `knowledge_files/` 原始页面和附件；
- 文件数量、总大小和抽样 SHA-256；
- 实际运行 commit；
- 加密保存且可取得的令牌加密密钥。

搜索投影可以删除后重建，不作为唯一备份。恢复演练至少核对文档数、版本数、附件数、来源数、
搜索权限以及抽样原文和附件哈希。

## 9. 当前生产部署门槛

本版本新增 `cryptography`、`msal` 和 `nh3` 依赖。在 NAS Python/Docker 镜像源和依赖安装能力
重新验证前，不得沿用旧镜像部署，也不得把“源码挂载成功”当成新依赖已经安装。

正式部署还必须：

1. 按 NAS 部署技能创建并验证生产数据库备份；
2. 联合备份或建立空的 `knowledge_files/` 持久目录；
3. 配置 Microsoft 回调域名、Client Secret 和独立令牌加密密钥；
4. 运行迁移和原有模块回归测试；
5. 先由一位成员验证一个笔记本，再创建正式 DSM 定时任务。

## 10. 本地验证记录

2026-07-30 已完成以下本地验证：

- 知识底座与随手记 30 项自动测试通过；
- 全项目 203 项自动测试通过；
- `manage.py check` 通过，仅保留 Microsoft 应用和生产加密密钥尚未配置的预期告警；
- `makemigrations --check --dry-run` 未发现遗漏迁移；
- 知识中心、搜索、来源管理和待确认页面完成浏览器验证；
- 375 px 宽度下完成移动端布局检查，浏览器控制台无错误。

尚未验证的项目包括真实 Microsoft OAuth、真实 OneNote 内容与大体量搜索、真实云端 AI
建议质量、NAS 镜像构建、生产备份恢复和 DSM 定时任务。这些项目必须在用户提供试点与生产
配置、且 NAS 依赖构建门槛解决后逐项验收，不能用本地模拟结果代替。
