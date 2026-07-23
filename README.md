# 家庭投资与记账工作台

面向家庭使用的 Django 工作台，当前主要包括家庭账本和港股打新模块，并使用 Docker Compose 部署到群晖 NAS。

## 首次启动

1. 复制环境变量文件，并填写强密码、随机密钥和 NAS 地址：

```bash
cp .env.example .env
```

生成 Django 密钥可以使用：

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

`.env` 只保存在部署设备，不应提交到 Git。AI API Key 也只通过环境变量配置。

图片识别支持在“新增新股资料”页面选择不同模型。智谱使用
`ZHIPU_API_KEY`；豆包使用火山方舟的 `ARK_API_KEY`。豆包默认配置调用
`https://ark.cn-beijing.volces.com/api/v3` 的
`doubao-seed-2-0-lite-260215`，可在 Django 后台的“AI 服务商”中调整模型。

2. 启动容器：

```bash
docker compose up -d --build
docker compose exec -T web python manage.py check
docker compose exec -T web python manage.py showmigrations
```

容器启动时会自动执行数据库迁移和静态文件收集。默认只把 PostgreSQL 映射到 NAS 的 `127.0.0.1`，不要把 5432 端口暴露到公网。

3. 新建空数据库时，创建管理员：

```bash
docker compose exec web python manage.py createsuperuser
```

只有需要演示数据时才运行 `bootstrap_first_data`。正式账本迁移不应先生成演示数据。

## 外网访问与 HTTPS

建议通过群晖反向代理和有效证书提供 HTTPS，不直接把容器端口暴露到公网。反向代理目标可以是 `http://127.0.0.1:8000`。

HTTPS 验证正常后，在 `.env` 设置：

```dotenv
DJANGO_ALLOWED_HOSTS=finance.example.com,nas-lan-ip
DJANGO_CSRF_TRUSTED_ORIGINS=https://finance.example.com
DJANGO_SECURE_PROXY_SSL_HEADER=True
DJANGO_SECURE_SSL_REDIRECT=True
DJANGO_SESSION_COOKIE_SECURE=True
DJANGO_CSRF_COOKIE_SECURE=True
DJANGO_SECURE_HSTS_SECONDS=31536000
```

先确认 HTTPS 和反向代理头正确，再启用 HSTS；配置错误可能导致浏览器在有效期内强制使用 HTTPS。

部署前检查：

```bash
docker compose exec -T web python manage.py check --deploy
docker compose ps
docker compose logs --tail=100 web
```

## 迁移现有数据

现有 PostgreSQL 数据、上传文件和代码是三类独立内容。Git 只用于代码；真实账本数据和密钥不进入仓库。

在旧设备导出数据库：

```bash
docker compose exec -T db pg_dump -U family_finance_user -d family_finance -Fc > family_finance.dump
```

把 `family_finance.dump` 和项目根目录的 `media/` 通过受信任的局域网或加密方式复制到 NAS。NAS 上启动数据库后恢复：

```bash
docker compose exec -T db pg_restore -U family_finance_user -d family_finance --clean --if-exists < family_finance.dump
docker compose exec -T web python manage.py migrate
```

恢复会改写目标数据库，执行前务必确认目标和备份文件。部署完成后核对家庭成员、账户数量、收支记录、资产快照和港股交易总数。

## 备份与升级

至少定期备份：

- PostgreSQL 逻辑备份（`pg_dump -Fc`）。
- `media/` 上传文件。
- NAS 上的 `.env`（加密保存）。

升级代码前先备份数据库，然后执行：

```bash
docker compose up -d --build
docker compose exec -T web python manage.py check
docker compose exec -T web python manage.py showmigrations
```

若 Docker Hub 无法访问，可在 `.env` 中把 `PYTHON_IMAGE` 改为 NAS 可访问的 Python 3.12 镜像。

## NAS 上运行 Futu OpenD

自选股实时查询需要 Futu OpenD。NAS 使用独立的 `opend` Compose profile，
与网站共享 Docker 私有网络，端口 `11111` 不映射到宿主机或公网。

1. 从富途官网下载 Ubuntu 命令行 OpenD，将命令行目录完整解压到：

```text
opend/runtime/
```

其中至少应包含 `FutuOpenD`、`FutuOpenD.xml` 和 `AppData.dat`。该目录包含
富途专有程序，不进入 Git。

2. 在 NAS 的 `.env` 中设置：

```dotenv
FUTU_OPEND_HOST=opend
FUTU_OPEND_PORT=11111
FUTU_LOGIN_ACCOUNT=数字牛牛号
FUTU_LOGIN_PWD_MD5=登录密码的32位小写MD5
```

`.env` 应设置为仅部署管理员可读，不要提交到 Git，也不要通过在线网站计算
密码 MD5。

3. 启动并检查：

```bash
docker compose --profile futu up -d --build opend
docker compose --profile futu ps
docker compose --profile futu logs --tail=100 opend
docker compose exec -T web python manage.py check
```

OpenD 的设备信息和日志保存在 `opend/state/`，升级或重建容器时应保留该目录，
否则可能再次触发设备锁验证。当前配置只供行情查询使用，且不主动抢占手机或
桌面端的最高行情权限。

## 每日投资组合估值

统一命令会依次刷新自动行情、刷新估值汇率、检查手工价格和到期期权，
然后生成账户、成员、家庭三级快照及明细：

```bash
docker compose exec -T web python manage.py run_daily_portfolio_valuation
```

同一估值日期重复运行会更新原快照及明细，不会生成重复快照。每次执行均在
Django 后台的“每日投资组合估值运行”中留下独立记录，包括行情批次、汇率
来源日期、缺价、过期价格、缺汇率和错误明细。

家庭管理员也可以在“投资组合”页面点击“刷新行情并生成今日快照”。页面会
显示最近一次运行的状态、统计数字和异常详情；该按钮用于首次验证和异常重试，
不能代替 NAS 定时任务。

可用参数：

```text
--date YYYY-MM-DD     指定估值日期，默认当天
--include-watchlist   同时刷新家庭自选标的
--require-complete    存在缺价、缺汇率或流水错误时不写入快照
--fail-on-warning     部分成功时保留快照，但以非零状态退出
```

NAS 定时任务建议在北京时间每天早晨、美国市场收盘后执行，并带上
`--fail-on-warning`，让任务中心能够对部分成功和失败状态告警。配置定时任务
前先手工执行一次，并在后台核对运行记录与当日家庭快照。

## 更换开发电脑

建议把代码推送到私有 Git 仓库，新电脑克隆仓库后单独创建 `.env`。NAS 上的正式数据库继续由网页使用，不通过 Git 同步；如需本地调试真实问题，使用脱敏后的数据库备份。
