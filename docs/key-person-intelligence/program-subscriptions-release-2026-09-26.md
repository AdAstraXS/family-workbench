# 精选订阅发布记录 · 2026-09-26

## 应用发布

- 分支：`codex/intelligence-nas-release`，以 NAS 原运行版本 `7279ee4ac20f700cc60dcd77e8893f1d9a8857fd` 为基础。
- 已发布并推送 GitHub：`820775ef2b7141e31a29d0f0845b2d532c9b2737`。
- 镜像：`sha256:e488cf4ea3106359d05297b6e650c98fcaf98829eac71f2d827ddebe0486d953`，已构建并通过固定 `recreate-web` 子命令应用。
- 新迁移 intelligence 0009、0010 成功；Django check 无问题；外部设置页 200，内部 HTTP 301 转向 HTTPS。
- 配置入口：https://adastrax.top/intelligence/programs/settings/
- 四个来源启用；现有 DeepSeek 文本整理模型开启完整公开正文授权；百炼 Key 等待用户填写。

## 恢复点

数据库备份：`backups/family-workbench-before-programs-820775e.dump`（8.4 MB，pg_restore 目录验证通过）。
SHA-256：`5fd1de3bd67257b6790f824d566b3b9317543da3a9af0410ec80a904a3dcc529`。

旧源码：`backups/source-predeploy-7279ee4ac20f700cc60dcd77e8893f1d9a8857fd-20260926-223352.tar.gz`。
SHA-256：`8164a98375a548ddedca8854573192cef3b05ccce3e6fc5544b3e1a89c7d83c2`。

应用归档：`backups/family-workbench-820775e.tar.gz`。
SHA-256：`5853570e77180a175a463ad21c362b582afacc4315db39dd1e06878f82b47912`。

前后财务基线一致：账户 35、持仓 480、交易 1069、快照 2110、快照明细 13098、每日估值运行 73，
最新快照日期 2026-09-26。生产只新增功能表和用户授权的订阅配置，未导入本地数据库。
工作台 `.env` 哈希保持不变；DB 与 OpenD 未重建。

## 验收与继续工作

- Linux / PostgreSQL 情报与知识 172 项通过；后续最终精选订阅 27 项通过。
- 专用代理接入另增 4 项测试，31 项通过；桌面与 390px 阅读页及主动知识归档已验证。
- NAS Oaktree 直连可用；YouTube、Dwarkesh、Acast 直连失败。用户已选独立代理，正在单独配置。
- 待完成代理实际抓取、DSM 五分钟任务、百炼真实转写和真实摘要；目前不能标记完整自动流程验收完成。
