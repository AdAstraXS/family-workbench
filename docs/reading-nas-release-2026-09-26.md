# 在线书库 NAS 发布记录

## 运行版本

- 时间：2026-09-26 22:07（Asia/Shanghai）。
- 入口：https://adastrax.top/reading/ 。使用已有家庭成员账户。
- 实际运行：`7279ee4ac20f700cc60dcd77e8893f1d9a8857fd`。
- 发布分支：`codex/reading-nas-release`，已推送既有项目 GitHub origin。
- 以 NAS 原版本 `363883c3d4b822e6215c4511c1dc152835e8a509` 为基础合入书库提交，保留同期其他模块功能。
- 新 Web 镜像：`sha256:6f7712e560de6c8011076c65a01cd2bb1695896fc1f6a4665491e6df074f257f`，构建包含 `pypdf==6.19.0`。

## 备份与迁移

恢复点均位于 NAS `/volume1/docker/family-workbench/backups/`：

| 文件 | SHA-256 |
| --- | --- |
| `family-workbench-pre-reading-20260926-2140.dump`（8.3 MB，pg_restore 目录校验通过） | `2b706cc8775bae9aafe6e466e4cce3623fe0ee3cce8103a087146f751bc383f5` |
| `source-predeploy-363883c3d4b822e6215c4511c1dc152835e8a509-20260926-214253.tar.gz`（归档可读） | `c60c485dc8bb3c561e689d09bbf645a18d7c8d5acb0544153b80dc43c1ebcd07` |
| `family-workbench-reading-7279ee4.tar.gz`（精确 Git 归档） | `610f3df173e72f0f06d17df29f0bd5d56182a3be7123577d97c917bbb8d02176` |

应用成功：`knowledge.0013_alter_knowledgesource_kind`、`reading.0001_initial`、
`reading.0002_bookfile_page_count_annotation_annotationcomment_and_more`。生产数据库仅执行这些结构迁移；
未导入本地数据库、样本图书或演示数据。390 个静态文件收集完成。

前后财务基线一致：账户 35、持仓 480、流水 1069、快照 2110、快照明细 13098、
每日估值运行 73，最新快照日期 2026-09-26。`.env` 哈希未变化；DB 和 OpenD 容器保持原创建时间且健康。
未修改 Compose、密码、长期部署权限或数据库卷。

## 后台处理

DSM 任务 ID **11**：`family-workbench-reading`，root，启用，每日 00:00–23:55 每 5 分钟运行。
使用 `flock` 避免重叠；每轮导入一本书、处理一个成员已逐次确认的 AI 请求。
不会自行生成 AI 请求，不会重试已失败的付费请求。失败返回非零；日志：`logs/reading-jobs.log`。

已核对任务内容与 `/etc/crontab` 登记。22:05:01 自动触发和 22:05:42 手动验收均运行完成，
AI 队列为零、失败为零；未向服务商发送测试书籍。DSM 命令行仍有已知的状态数据库读取提示，
任务配置、下次触发、cron 登记及实际日志均正常。

## 验收与限制

- 合入生产基础后的独立 PostgreSQL 联合回归：102 项通过。
- 生产 Django 系统检查无问题；迁移与 Gunicorn 启动日志正常。
- 内部书库未登录返回 302；外部 HTTPS 登录态可打开书架，匿名返回 302。
- 阅读 JS 和 PDF.js 模块返回 200，`.mjs` 类型为 `text/javascript`。
- 正式页面已检查 390px 布局，临时视口已恢复。
- 真机阅读、触摸划线、跨设备续读及外网大文件上传由成员下一步验收；生产书架初始为空。
- OCR、MOBI 转换、整书总结仍按首版范围暂缓。真实 AI 质量和延迟尚未验收。

本记录为部署后的文档提交，实际运行代码仍以以上 `7279ee4` 为准。
