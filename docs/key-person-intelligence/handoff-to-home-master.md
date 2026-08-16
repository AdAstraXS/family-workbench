# 关键人物动态模块：家中电脑合并交接单

更新时间：2026-08-13
交付分支：`codex/key-person-intelligence`
分支基线：`origin/master` @ `38d9002`
适用场景：在家中电脑的最新 `master` 上整合本分支；不是 NAS 生产部署操作单。

## 1. 本次交付内容

本分支包含 M1、M1.5 和 M2 的完整增量：

- 新增通用 `intelligence` Django app、3 个迁移、Admin、家庭权限和运行审计；
- 今日精选、全部动态、关注主题、信源管理、流水线、运行状态和事件详情页面；
- 人工事件录入、证据链、关注、已读、收藏、忽略和确定性 `people-v1` 评分；
- RSS / Atom 与 YouTube 官方频道元数据适配器；
- 安全 HTTP、三层去重、ETag / Last-Modified 游标、有限重试和故障隔离；
- 规则相关性门控、噪音箱、候选事件聚类及待复核保护；
- 首批关注主题、关系和 7 个官方信源的幂等初始化命令；
- M0–M2 产品、技术、来源治理和运维文档。

没有新增 Python 依赖，没有修改现有财务计算口径，也没有包含 `.env`、API Key、本地数据库、日志或
生产数据。

## 2. 已完成验证

- 情报模块 35 项测试通过；
- 全项目 217 项测试通过；
- `manage.py check` 通过；
- `makemigrations --check --dry-run` 无遗漏；
- `git diff --check` 通过；
- 运行状态、信源管理、流水线完成桌面与手机宽度真实浏览器验收；
- 办公电脑真实读取 OpenAI 官方 RSS 10 条，生成 10 个待复核候选；
- 同一固定样本连续采集 3 次不产生重复条目或事件。

## 3. 家中电脑推荐合并流程

先确保家中电脑当前工作没有未提交修改：

```bash
cd <家中电脑的 family-workbench 项目目录>
git status
```

如果 `git status` 不是干净状态，先提交或暂存自己的修改，不要覆盖。然后：

```bash
git fetch origin
git switch master
git pull --ff-only origin master
git switch -c codex/integrate-key-person-intelligence
git merge --no-ff origin/codex/key-person-intelligence
```

先在新的整合分支解决冲突、迁移和测试，不建议直接在 `master` 上边合并边调试。确认完成后再合回：

```bash
git switch master
git merge --no-ff codex/integrate-key-person-intelligence
```

如果希望让家中电脑的 Codex 接手，可把本文档路径和下面这段要求交给它：

```text
请读取 AGENTS.md 和 docs/key-person-intelligence/handoff-to-home-master.md，
在家中电脑最新 master 建立整合分支，合并 origin/codex/key-person-intelligence。
保留 master 上更新的现有模块实现，不使用 ours/theirs 整体覆盖；逐文件解决冲突，
完成迁移检查、intelligence 测试和全项目测试。不要连接或部署 NAS，不要修改生产数据库。
```

## 4. 重点冲突文件

家中 `master` 如果已经继续开发，以下文件最可能发生冲突：

- `app/config/settings.py`：保留最新应用配置，并确保 `intelligence` 在 `INSTALLED_APPS`；
- `app/config/urls.py`：保留现有 URL，并加入 `/intelligence/`；
- `app/templates/base.html`：保留最新导航，并加入“AI 情报”和 `extra_head` 模板块；
- `app/family_core/context_processors.py`：保留最新返回逻辑，并加入 intelligence 页面返回层级；
- `docs/pending-tasks.md`：以家中最新项目进度为主体，合入本模块 M1–M2 状态。

不要对上述文件直接选择“全部使用一方”。`app/intelligence/`、`app/templates/intelligence/`、
`app/static/css/intelligence.css` 和 `docs/key-person-intelligence/` 在基线中是新增目录，通常可以整体保留。

## 5. 合并后的本地检查

先确认当前 `DATABASE_URL` 指向家中电脑的本地开发数据库，而不是 NAS 生产 PostgreSQL，然后运行：

```bash
python manage.py makemigrations --check --dry-run
python manage.py migrate
python manage.py seed_key_people --dry-run
python manage.py seed_key_people
python manage.py seed_intelligence_sources --dry-run
python manage.py seed_intelligence_sources
python manage.py test intelligence
python manage.py test
python manage.py check
git diff --check
```

`seed_key_people` 和 `seed_intelligence_sources` 均为幂等命令。它们只登记主题和来源；默认不会让所有家庭
自动关注全部主题。需要为某个家庭批量关注时，必须先确认家庭 ID，再显式运行：

```bash
python manage.py seed_key_people --follow-all --family-id <家庭ID>
```

## 6. 环境变量和真实来源状态

M2 不需要新增 API Key。若家中电脑的全局代理把公网域名解析到 `198.18.0.0/15` Fake-IP，可只在
家中本地环境设置：

```text
INTELLIGENCE_ALLOW_PROXY_FAKE_IP=True
```

普通网络和 NAS 保持默认 `False`。该开关不会允许直接填写 `198.18.x.x` 作为信源地址。

首批 3 个 RSS 信源默认启用。4 个 YouTube 信源已登记但默认停用：2026-08-13 实测时，官方频道页面
仍声明 Atom 地址，但 OpenAI、NVIDIA、ARK Invest 和 Ray Dalio 的无登录 Atom 请求均返回 HTTP 404。
不要为了“显示成功”而关闭错误检查；待端点恢复或确定改用 YouTube Data API 后再启用。

## 7. NAS 部署边界

本次推送只完成 GitHub 分支交付，未部署 NAS、未迁移生产数据库、未创建 DSM 定时任务。家中电脑完成
合并和验收后，如要部署 NAS，必须另开部署步骤：先按项目约定备份并验证生产数据库，再基于已提交的
精确 commit 部署、执行迁移、小流量验证 RSS，并最后配置 DSM Task Scheduler。

不得把任何本地 `db.sqlite3`、dump、`postgres/`、`.env` 或测试数据上传到 NAS。

## 8. 合并完成后的页面

本地服务启动后：

- AI 情报首页：`/intelligence/people/`
- 信源管理：`/intelligence/sources/`
- 运行状态：`/intelligence/operations/`
- 流水线：`/intelligence/pipeline/`

完整 M2 技术与运维说明见 `docs/key-person-intelligence/m2-collection-design.md`。
