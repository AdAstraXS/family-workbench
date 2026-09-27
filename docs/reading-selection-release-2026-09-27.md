# 阅读划线与移动选区修复 NAS 发布记录

## 版本与部署范围

- 发布时间：2026-09-27 17:33–17:35（Asia/Shanghai）。
- 入口：https://adastrax.top/reading/ 。
- 实际运行提交：`dc5c31ed144763df467993445ed105c7f8bcc1af`，已推送 `origin/codex/reading-nas-release`。
- 功能提交：`728b0b5561a7fc5fc6b7f6820734e868e32d866a`，修复阅读划线及移动端选区操作。
- 预检时 NAS 已运行 `2ccb77855f920307400e1fdb71150cb3078b427a`。发布分支已无冲突合入其独立投资研究更新，保留现有生产功能。
- 相对实际生产基础只修改 10 个 reading 应用文件及既有发布文档；依赖、Dockerfile 和 Compose 无差异。使用受限包装器安装 `app/`，随后仅 `restart-web`，复用当前镜像。

## 备份与源码恢复点

文件均保存在 NAS `/volume1/docker/family-workbench/backups/`：

| 文件 | SHA-256 |
| --- | --- |
| `family-workbench-pre-reading-selection-20260927-1810.dump`（36 MB；实际创建于 17:32，名称后缀是标签；受限包装器完成 `pg_restore -l` 验证） | `d44da3d2fb535721a95346d01bcded9c445cb6c143eb879af95398246c34ba2c` |
| `source-predeploy-2ccb77855f920307400e1fdb71150cb3078b427a-20260927-173248.tar.gz`（安装前自动生成，归档可读） | `5d8f28b0fa8a0469c9b9b884d78b7f95c88e2421b47fed19f1c29e3ca5206a35` |
| `family-workbench-reading-selection-dc5c31e.tar.gz`（精确 Git 归档，4,117,305 字节） | `6aa25401d53862248794e027f9f020bb928bffacf75069cb048d078b8ff2431f` |

发布包上传前后哈希一致；安装后 10 个变更文件与 Git 归档逐项 SHA-256 一致。

## 数据迁移及其验证边界

17:33 启动日志确认 `reading.0004_remove_hidden_plain_highlights... OK`，本次启动未执行其他迁移。

迁移仅遍历 `highlight_visible=False` 的旧划线：

- `note` 为空且没有 `AnnotationComment` 的记录被删除。
- 有批注正文或评论的记录恢复为可见。
- 其余可见记录不由该迁移处理。

现有受限包装器未提供阅读批注的只读统计命令，因此部署前影响数量及部署后准确删除/恢复行数未取得。
未扩大 sudo 或 Docker 权限，也未把财务基线检查当成阅读记录逐行验证。部署依据已确认迁移条件、
经过验证的生产数据库备份、测试结果及成功迁移日志完成。该数据迁移的反向操作为空，不能靠反向迁移
自动还原已删除记录；恢复生产数据需另行明确授权并使用本次数据库恢复点。

## 生产验收

- Django `check` 无问题；静态文件收集完成（264 copied、127 unmodified），Gunicorn 正常启动。
- 内部 HTTP（正确 Host 与 HTTPS 转发头）和外部 HTTPS 的书库匿名访问均返回正常登录跳转 302。
- `reader.js`、`reader.css`、`vendor/foliate/paginator.js` 外部访问均为 200。
- 财务基线部署前后完全一致：账户 35、持仓 480、流水 1069、快照 2139、快照明细 13284、每日估值运行 74，最新快照日期 2026-09-27。
- `.env` SHA-256 前后均为 `78d7894b721698adf1d5a4ce3c969e4983b2ee079cdebdb5082db3aca985b51c`。
- DB、OpenD 创建时间和运行状态保持，均健康；Web 只重启。未导入本地数据库、测试书籍或演示数据，未触发每日估值或 AI 请求。
- 未修改定时任务、权限、Compose、环境配置、数据库卷、上传文件或 OpenD 状态。书库任务日志至 17:30 正常，每轮失败或中断为零。
- 全部可用检查完成后执行 `mark-deployed`，复核运行提交为 `dc5c31ed144763df467993445ed105c7f8bcc1af`。

## 发布前测试与限制

- 主代理在最终合并提交复跑 reading：38 项，36 通过、2 项 SQLite 环境并发测试跳过。
- 两个 JavaScript 文件的 `node --check`、`git diff --check` 通过。
- 真实 iOS/Android 触摸行为仍需成员真机验收；本轮生产部署验收未操作成员划线或阅读进度。

此文件为部署后的文档提交，实际运行源码仍以上述 `dc5c31e` 为准。长期部署密钥与受限包装器保留，未创建临时权限。
