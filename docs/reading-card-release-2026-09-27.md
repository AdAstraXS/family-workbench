# 阅读批注卡片与边缘点击 NAS 发布记录

## 运行版本

- 发布时间：2026-09-27 18:16–18:18（Asia/Shanghai）。
- 入口：https://adastrax.top/reading/ 。
- 实际运行：`a636a78674b1f2ca5fe620b1664bedf22b43ef8a`，已推送 GitHub `origin/codex/reading-nas-release`。
- 发布前版本：`dc5c31ed144763df467993445ed105c7f8bcc1af`，预检未发现额外生产提交。
- 功能提交 `5475f6b5246dcebfacf42383f2e8f3825fde2cd3` 调整批注卡片和边缘点击；随后独立追加 `a636a78`，用同步命中检测保护边缘批注，防止移动端合成点击导致误翻页。
- 本轮仅五个 reading 源码/测试文件变化；无模型、迁移、依赖或 Compose 变化。精确 Git 归档不含未跟踪的 `docs/prototypes/` 独立演示，演示目录保留未改动。

## 已验证恢复点

以下文件位于 NAS `/volume1/docker/family-workbench/backups/`：

| 文件 | SHA-256 |
| --- | --- |
| `family-workbench-pre-reading-card-20260927-1816.dump`（36 MB，18:15 创建；包装器完成 `pg_restore -l` 验证） | `c731ca135916df3eb2fa13a1afc5af5de3c42e97beb8d5d9c85d8e648d49366b` |
| `source-predeploy-dc5c31ed144763df467993445ed105c7f8bcc1af-20260927-181609.tar.gz`（覆盖前自动生成且验证可读） | `98c99f372cbe2b57a8dd5d119b24891049033f74d846bbf85ab41a9033f44f6c` |
| `family-workbench-reading-card-a636a78.tar.gz`（精确 Git 归档，4,119,314 字节） | `caecfc36498adfe1a9ef435429780699fe83a678a8ff76f115e8c1c348eb07df` |

发布包上传前后 SHA-256 一致；安装后五个文件与归档逐项 SHA-256 一致。使用受限包装器仅安装 `app/` 并 `restart-web`，复用当前镜像。

## 生产核验

- Django check 无问题；启动日志显示 `No migrations to apply`。本次未执行数据库迁移或数据修复。
- 静态文件收集完成（264 copied、127 unmodified），Gunicorn 正常启动。
- 内部 HTTP（正确 Host 与 HTTPS 转发头）和外部 HTTPS 的书库匿名入口均返回正常登录跳转 302；阅读器 JS/CSS 返回 200。
- 前后财务基线一致：账户 35、持仓 480、流水 1069、快照 2139、快照明细 13284、每日估值运行 74；最新快照日期 2026-09-27。
- `.env` SHA-256 保持 `78d7894b721698adf1d5a4ce3c969e4983b2ee079cdebdb5082db3aca985b51c`。
- DB、OpenD 创建时间和运行状态不变且健康；Web 仅重启。未修改数据库卷、media、OpenD 状态、权限或定时任务，未导入本地数据、触发每日估值或 AI 请求。
- 预检书库任务日志至 18:10 正常，失败/中断为零。
- 验证通过后 `mark-deployed`，复核运行提交为 `a636a78674b1f2ca5fe620b1664bedf22b43ef8a`。

## 发布前验收与限制

- 主代理在 `5475f6b` 运行 reading 39 项测试：37 通过、2 项 SQLite 并发测试跳过；Django check 与迁移检查通过。
- 最终 `a636a78` 仅追加 JavaScript 修正；`node --check`、`git diff --check` 通过。
- 手机宽度浏览器实测左边缘批注打开卡片而不翻页，旁边正文可正常翻页。
- 真实 iOS/Android 触摸事件仍待成员真机验收。

本记录为部署后的文档提交，实际运行源码仍以上述 `a636a78` 为准。保留长期部署密钥与受限包装器，未创建临时权限。
