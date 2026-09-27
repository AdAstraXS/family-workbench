# 阅读行距、划线管理与选区弹框 NAS 发布记录

## 运行版本与范围

- 发布时间：2026-09-27 16:22–16:24（Asia/Shanghai）。
- 入口：https://adastrax.top/reading/ 。
- 实际运行提交：`5015985df8d4c31fb68ad31d9a1ac61bb737f819`，已推送 GitHub `origin/codex/reading-nas-release`。
- 阅读器功能提交：`150d5573209250ee92e82f3f67c77b574b9b88f0`。
- 预检发现生产已从上轮 `282d885` 更新为 `03aedaab1661a4561052b7fe76879552a9d9a526`。该提交的独立模块更新已无冲突合入，未覆盖已上线的关注人物、自助来源和投资研究功能。
- 相对实际生产基础，本轮只变更 11 个阅读器应用文件及发布文档；依赖、Dockerfile 与 Compose 无变化。受限包装器安装 `app/` 后执行 `restart-web`，复用现有镜像。

本轮上线：

1. EPUB 行距可选紧凑、标准、宽松。
2. 纯划线可取消与恢复，保留原记录和稳定引用；有批注或回复的记录不能取消。
3. 选区操作框与批注框靠近所选文字末尾，并避让页面边界与底栏。

## 数据库备份与源码恢复点

以下文件保存在 NAS `/volume1/docker/family-workbench/backups/`：

| 文件 | SHA-256 |
| --- | --- |
| `family-workbench-pre-reading-highlights-20260927-1620.dump`（36 MB，受限包装器完成 `pg_restore -l` 验证） | `93789c3916eea04115d723d35e194473e17628796328e9e9af614f1f5629a57d` |
| `source-predeploy-03aedaab1661a4561052b7fe76879552a9d9a526-20260927-162122.tar.gz`（覆盖前自动生成，归档可读） | `3dfcc741468ffb9249089edf6f0a27069e860639119151be3a2b7232cb908127` |
| `family-workbench-reading-highlights-5015985.tar.gz`（精确 Git 归档，4,086,591 字节） | `8326ebafc8624c7934173af45b59d3b14914163eea2859590eb8e90dbacfce0f` |

上传前后归档哈希一致；部署后 11 个应用文件与归档内容逐项 SHA-256 一致。

## 迁移与生产验证

- 16:22 启动日志确认 `reading.0003_annotation_highlight_visible... OK`。新增 `Annotation.highlight_visible` 布尔字段，默认 `True`，使已有划线按原样显示。此次启动没有执行其他迁移。
- 未恢复或导入本地数据库，未上传测试书籍、制造 AI 请求或回补财务数据。
- Django 系统检查无问题；静态文件收集完成（264 copied、127 unmodified），Gunicorn 正常启动。
- 内部 HTTP（正确 Host 与 HTTPS 转发头）及外部 HTTPS 匿名访问均返回正常登录跳转 302；阅读器 JS、CSS 返回 200。
- 主代理通过认证 Chrome 只读打开正式书库，三本书正常显示；未进入图书阅读页，保留成员阅读进度。
- 财务基线前后完全一致：账户 35、持仓 480、流水 1069、快照 2139、快照明细 13284、估值运行 74，最新快照日期 2026-09-27。与上轮记录的差异在本次部署前已存在，本次未触发每日估值。
- `.env` SHA-256 前后均为 `78d7894b721698adf1d5a4ce3c969e4983b2ee079cdebdb5082db3aca985b51c`。
- DB、OpenD 保持原创建时间和运行状态，均健康；Web 仅重启。
- 未修改 DSM 任务、部署权限、Compose、数据库卷、上传文件或 OpenD 状态。部署前书库任务日志至 16:15 正常，每轮失败或中断均为零；未手动触发任务。
- 检查通过后执行 `mark-deployed`，复核为 `5015985df8d4c31fb68ad31d9a1ac61bb737f819`。

## 发布前测试与限制

- 最终合并提交复跑 reading 36 项：34 通过、2 项 SQLite 并发测试跳过。并发测试此前在 PostgreSQL 上通过，本轮未改并发实现。
- `reading` 的 `makemigrations --check --dry-run` 无变化；`node --check`、`git diff --check` 通过。本地 Django check 只有两条缺少 OneNote 配置的已知警告，生产 check 无问题。
- 本地 Chrome 手机宽度验收通过：三档行距、纯划线取消与恢复、有批注记录不显示取消按钮、EPUB 选区弹框靠近文字末尾并避让底栏。
- 真 PDF 文字页在 390×844 视口拖选后，操作框出现在选区末尾右下方；页面截图保留本地，未提交。
- 尚未完成真实 iOS/Android 设备触摸验收。

本文件是部署后的文档提交，实际运行源码仍以上述 `5015985` 为准。长期部署密钥与受限包装器保留，未创建临时权限。
