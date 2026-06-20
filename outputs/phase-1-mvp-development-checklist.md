# 第一阶段 MVP 开发清单

项目：家庭投资与记账工作台  
版本：v0.1  
日期：2026-06-17  
目标：先做出一个可在群晖 NAS 上运行、能录入数据、查询汇总、展示基础图表、预留 AI 分析入口的最小可用版本。

---

## 1. MVP 范围

第一阶段只做“能用起来”的核心版本。

必须完成：

- 登录和用户管理。
- 家庭成员管理。
- 投资账户录入。
- 证券标的录入。
- 投资持仓录入。
- 投资交易记录录入。
- 银行账户录入。
- 收入分类和收入记录。
- 支出分类和支出记录。
- 首页仪表盘。
- 投资组合汇总页。
- 家庭账本汇总页。
- 基础图表。
- AI 分析模块占位。
- 港股打新、复盘、宏观数据模块保留基础入口。
- Docker Compose 可在群晖 NAS 部署。

第一阶段暂不做：

- 自动抓行情。
- 自动抓新闻。
- 自动抓港股新股数据。
- 自动同步银行流水。
- 复杂多级权限。
- 手机 App。
- 复杂前端框架。

---

## 2. 推荐项目目录结构

建议 Django 项目目录如下：

```text
family-finance/
  docker-compose.yml
  .env.example
  README.md
  app/
    Dockerfile
    requirements.txt
    manage.py
    config/
      __init__.py
      settings.py
      urls.py
      wsgi.py
      asgi.py
    family_core/
      models.py
      admin.py
      views.py
      urls.py
      forms.py
    portfolio/
      models.py
      admin.py
      views.py
      urls.py
      forms.py
    ledger/
      models.py
      admin.py
      views.py
      urls.py
      forms.py
    ipo/
      models.py
      admin.py
      views.py
      urls.py
    notes/
      models.py
      admin.py
      views.py
      urls.py
    macro/
      models.py
      admin.py
      views.py
      urls.py
    ai_analysis/
      models.py
      admin.py
      views.py
      urls.py
      services.py
    dashboard/
      views.py
      urls.py
    templates/
      base.html
      dashboard/
      portfolio/
      ledger/
      ai_analysis/
    static/
      css/
      js/
```

说明：

- `config`：Django 主配置。
- `family_core`：家庭、成员、币种、基础权限。
- `portfolio`：投资组合。
- `ledger`：收支账本。
- `ipo`：港股打新，第一阶段只做基础表和菜单入口。
- `notes`：投资笔记，第一阶段只做基础表和菜单入口。
- `macro`：宏观数据，第一阶段只做基础表和菜单入口。
- `ai_analysis`：AI 服务商、分析请求、分析结果。
- `dashboard`：首页仪表盘。

---

## 3. Django App 划分

### 3.1 family_core

职责：

- 家庭信息。
- 家庭成员。
- 币种。
- 汇率。
- 通用枚举。

第一阶段模型：

- `Family`
- `FamilyMember`
- `Currency`
- `ExchangeRate`

第一阶段页面：

- 家庭设置页。
- 家庭成员列表页。
- 家庭成员新增/编辑页。

后台录入：

- 家庭。
- 家庭成员。
- 币种。
- 汇率。

### 3.2 portfolio

职责：

- 投资账户。
- 证券标的。
- 持仓。
- 交易记录。
- 投资组合快照。

第一阶段模型：

- `InvestmentAccount`
- `Security`
- `InvestmentPosition`
- `InvestmentTransaction`
- `PortfolioSnapshot`
- `SecurityNews`

第一阶段页面：

- 投资组合概览页。
- 投资账户列表页。
- 投资账户新增/编辑页。
- 持仓列表页。
- 持仓新增/编辑页。
- 交易记录列表页。
- 交易记录新增/编辑页。
- 证券标的列表页。

后台录入：

- 投资账户。
- 证券标的。
- 持仓。
- 交易记录。
- 投资组合快照。
- 股票新闻缓存。

### 3.3 ledger

职责：

- 银行账户。
- 收入分类。
- 支出分类。
- 收入记录。
- 支出记录。
- 月度现金流汇总。

第一阶段模型：

- `BankAccount`
- `IncomeCategory`
- `ExpenseCategory`
- `IncomeRecord`
- `ExpenseRecord`
- `CashflowMonthlySummary`

第一阶段页面：

- 家庭账本概览页。
- 银行账户列表页。
- 银行账户新增/编辑页。
- 收入记录列表页。
- 收入记录新增/编辑页。
- 支出记录列表页。
- 支出记录新增/编辑页。
- 收支分类管理页。

后台录入：

- 银行账户。
- 收入分类。
- 支出分类。
- 收入记录。
- 支出记录。
- 月度现金流汇总。

### 3.4 ipo

职责：

- 港股新股资料。
- 打新账户。
- 打新策略。
- 打新结果。

第一阶段只做基础录入和菜单入口。

第一阶段模型：

- `HkIpoListing`
- `HkIpoMarketStat`
- `HkIpoAccount`
- `HkIpoStrategy`
- `HkIpoResult`

第一阶段页面：

- 港股打新占位首页。
- 新股列表页。
- 打新策略列表页。

后台录入：

- 港股新股。
- 市场表现。
- 打新账户。
- 打新策略。
- 打新结果。

### 3.5 notes

职责：

- 投资笔记。
- 交易复盘。
- 打新复盘。

第一阶段模型：

- `InvestmentNote`

第一阶段页面：

- 投资复盘占位首页。
- 笔记列表页。
- 笔记新增/编辑页。

后台录入：

- 投资笔记。

### 3.6 macro

职责：

- 中国和美国宏观指标。
- 宏观数据点。

第一阶段只做基础录入和菜单入口。

第一阶段模型：

- `MacroIndicator`
- `MacroDataPoint`

第一阶段页面：

- 宏观数据占位首页。
- 指标列表页。

后台录入：

- 宏观指标。
- 宏观数据点。

### 3.7 ai_analysis

职责：

- AI 服务商配置。
- AI 分析请求记录。
- AI 分析结果保存。
- 后续封装不同 AI API。

第一阶段模型：

- `AiProvider`
- `AiAnalysisRequest`
- `AiAnalysisResult`

第一阶段页面：

- AI 分析首页。
- 新建分析请求页。
- 分析历史列表页。
- 分析结果详情页。

后台录入：

- AI 服务商。
- AI 分析请求。
- AI 分析结果。

第一阶段实现方式：

- 页面可以先保存分析问题和分析范围。
- 暂不真正调用 AI API。
- 先预留 `services.py`，后续统一写 AI API 调用逻辑。

### 3.8 dashboard

职责：

- 首页仪表盘。
- 家庭汇总数据。
- 快捷入口。

第一阶段页面：

- 首页仪表盘。

展示内容：

- 家庭总投资资产。
- 家庭银行账户余额。
- 本月收入。
- 本月支出。
- 本月结余。
- 最近 5 条投资交易。
- 最近 5 条支出记录。
- 投资资产分布图。
- 支出分类图。

---

## 4. 第一阶段数据模型清单

### 4.1 必须优先创建的模型

第一批先做这些：

- `Family`
- `FamilyMember`
- `Currency`
- `InvestmentAccount`
- `Security`
- `InvestmentPosition`
- `InvestmentTransaction`
- `BankAccount`
- `IncomeCategory`
- `ExpenseCategory`
- `IncomeRecord`
- `ExpenseRecord`
- `AiProvider`
- `AiAnalysisRequest`
- `AiAnalysisResult`

### 4.2 第二批再创建的模型

这些可以在第一阶段后半段或第二阶段补上：

- `ExchangeRate`
- `PortfolioSnapshot`
- `SecurityNews`
- `CashflowMonthlySummary`
- `HkIpoListing`
- `HkIpoMarketStat`
- `HkIpoAccount`
- `HkIpoStrategy`
- `HkIpoResult`
- `InvestmentNote`
- `MacroIndicator`
- `MacroDataPoint`

### 4.3 每张业务表的通用字段

建议大多数业务表都包含：

```text
remark
extra_data
created_at
updated_at
is_active
```

其中：

- `remark`：普通备注。
- `extra_data`：JSONB 自定义字段。
- `created_at`：创建时间。
- `updated_at`：更新时间。
- `is_active`：是否启用。

涉及删除风险的数据，后续可以增加：

```text
deleted_at
```

---

## 5. 页面开发清单

### 5.1 公共页面

| 页面 | URL 建议 | 第一阶段功能 |
|---|---|---|
| 登录页 | `/accounts/login/` | 使用 Django 自带登录 |
| 退出登录 | `/accounts/logout/` | 使用 Django 自带退出 |
| 首页仪表盘 | `/` | 展示家庭汇总和图表 |
| 系统设置 | `/settings/` | 家庭和成员入口 |

### 5.2 家庭成员页面

| 页面 | URL 建议 | 第一阶段功能 |
|---|---|---|
| 家庭成员列表 | `/family/members/` | 查看成员 |
| 新增成员 | `/family/members/create/` | 新增成员 |
| 编辑成员 | `/family/members/<id>/edit/` | 编辑成员 |

### 5.3 投资组合页面

| 页面 | URL 建议 | 第一阶段功能 |
|---|---|---|
| 投资概览 | `/portfolio/` | 总资产、持仓、盈亏 |
| 投资账户列表 | `/portfolio/accounts/` | 查看账户 |
| 新增投资账户 | `/portfolio/accounts/create/` | 新增账户 |
| 编辑投资账户 | `/portfolio/accounts/<id>/edit/` | 编辑账户 |
| 证券标的列表 | `/portfolio/securities/` | 查看股票/基金 |
| 新增证券标的 | `/portfolio/securities/create/` | 新增标的 |
| 持仓列表 | `/portfolio/positions/` | 查看持仓 |
| 新增持仓 | `/portfolio/positions/create/` | 新增持仓 |
| 交易记录列表 | `/portfolio/transactions/` | 查看交易 |
| 新增交易记录 | `/portfolio/transactions/create/` | 新增交易 |

### 5.4 家庭账本页面

| 页面 | URL 建议 | 第一阶段功能 |
|---|---|---|
| 账本概览 | `/ledger/` | 收入、支出、结余 |
| 银行账户列表 | `/ledger/accounts/` | 查看银行账户 |
| 新增银行账户 | `/ledger/accounts/create/` | 新增银行账户 |
| 收入记录列表 | `/ledger/income/` | 查看收入 |
| 新增收入记录 | `/ledger/income/create/` | 新增收入 |
| 支出记录列表 | `/ledger/expenses/` | 查看支出 |
| 新增支出记录 | `/ledger/expenses/create/` | 新增支出 |
| 分类管理 | `/ledger/categories/` | 管理收入和支出分类 |

### 5.5 港股打新页面

| 页面 | URL 建议 | 第一阶段功能 |
|---|---|---|
| 港股打新首页 | `/ipo/` | 占位和入口 |
| 新股列表 | `/ipo/listings/` | 查看录入的新股 |
| 策略列表 | `/ipo/strategies/` | 查看策略记录 |

### 5.6 投资复盘页面

| 页面 | URL 建议 | 第一阶段功能 |
|---|---|---|
| 复盘首页 | `/notes/` | 占位和入口 |
| 笔记列表 | `/notes/list/` | 查看笔记 |
| 新增笔记 | `/notes/create/` | 写投资笔记 |

### 5.7 宏观数据页面

| 页面 | URL 建议 | 第一阶段功能 |
|---|---|---|
| 宏观首页 | `/macro/` | 占位和入口 |
| 指标列表 | `/macro/indicators/` | 查看指标 |

### 5.8 AI 分析页面

| 页面 | URL 建议 | 第一阶段功能 |
|---|---|---|
| AI 分析首页 | `/ai/` | 选择模块和问题 |
| 新建分析 | `/ai/requests/create/` | 保存分析请求 |
| 分析历史 | `/ai/requests/` | 查看历史 |
| 分析结果 | `/ai/results/<id>/` | 查看结果 |

---

## 6. Django Admin 后台录入表

第一阶段必须注册到 Django Admin：

### 6.1 family_core

- `Family`
- `FamilyMember`
- `Currency`
- `ExchangeRate`

### 6.2 portfolio

- `InvestmentAccount`
- `Security`
- `InvestmentPosition`
- `InvestmentTransaction`
- `PortfolioSnapshot`
- `SecurityNews`

### 6.3 ledger

- `BankAccount`
- `IncomeCategory`
- `ExpenseCategory`
- `IncomeRecord`
- `ExpenseRecord`
- `CashflowMonthlySummary`

### 6.4 ipo

- `HkIpoListing`
- `HkIpoMarketStat`
- `HkIpoAccount`
- `HkIpoStrategy`
- `HkIpoResult`

### 6.5 notes

- `InvestmentNote`

### 6.6 macro

- `MacroIndicator`
- `MacroDataPoint`

### 6.7 ai_analysis

- `AiProvider`
- `AiAnalysisRequest`
- `AiAnalysisResult`

Admin 优化建议：

- 列表页显示关键字段。
- 支持按成员、日期、币种、账户筛选。
- 支持搜索账户名、证券代码、证券名称、备注。
- 金额字段只读汇总可以后续再做。

---

## 7. 第一版前端页面要求

第一阶段页面以实用为主。

### 7.1 UI 风格

建议：

- 左侧或顶部导航。
- 表格为主。
- 表单尽量简单。
- 首页用少量指标卡片。
- 图表只做必要的 2 到 4 个。

### 7.2 图表清单

第一阶段图表：

- 家庭资产构成：投资资产 vs 银行现金。
- 投资资产按账户分布。
- 投资资产按币种分布。
- 本月支出分类饼图。
- 最近 6 个月收入/支出/结余柱状图。

图表库：

- 使用 ECharts CDN 或本地静态文件。
- NAS 内网使用时建议后续下载到本地，避免外网依赖。

---

## 8. 第一版 Docker 文件

### 8.1 docker-compose.yml

第一版建议先用两个服务：`db` 和 `web`。Nginx 第二步再加。

```yaml
services:
  db:
    image: postgres:16
    container_name: family_finance_db
    restart: unless-stopped
    environment:
      POSTGRES_DB: ${POSTGRES_DB}
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - ./postgres:/var/lib/postgresql/data
    ports:
      - "${POSTGRES_PORT}:5432"

  web:
    build: ./app
    container_name: family_finance_web
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - ./app:/app
      - ./media:/app/media
      - ./staticfiles:/app/staticfiles
    depends_on:
      - db
    ports:
      - "${WEB_PORT}:8000"
    command: >
      sh -c "python manage.py migrate &&
             python manage.py collectstatic --noinput &&
             gunicorn config.wsgi:application --bind 0.0.0.0:8000"
```

### 8.2 .env.example

```env
POSTGRES_DB=family_finance
POSTGRES_USER=family_finance_user
POSTGRES_PASSWORD=change_me_to_a_strong_password
POSTGRES_PORT=5432

WEB_PORT=8000
DJANGO_SECRET_KEY=change_me_to_a_random_secret
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,nas-ip

DATABASE_URL=postgres://family_finance_user:change_me_to_a_strong_password@db:5432/family_finance
```

实际部署时：

- 复制 `.env.example` 为 `.env`。
- 修改 `POSTGRES_PASSWORD`。
- 修改 `DJANGO_SECRET_KEY`。
- 把 `nas-ip` 改成你的群晖 IP。

### 8.3 app/Dockerfile

```dockerfile
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/
```

### 8.4 app/requirements.txt

```text
Django>=5.0,<6.0
psycopg[binary]>=3.1
dj-database-url>=2.1
gunicorn>=21.2
whitenoise>=6.6
python-dotenv>=1.0
```

---

## 9. Django settings 第一版配置要点

需要配置：

```python
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "family_core",
    "portfolio",
    "ledger",
    "ipo",
    "notes",
    "macro",
    "ai_analysis",
    "dashboard",
]
```

数据库使用 `DATABASE_URL`：

```python
import dj_database_url

DATABASES = {
    "default": dj_database_url.config(default="sqlite:///db.sqlite3")
}
```

静态文件：

```python
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"
```

登录：

```python
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"
```

---

## 10. 开发任务拆解

### 10.1 第 1 步：创建项目骨架

任务：

- 创建 Django 项目。
- 创建 8 个 app。
- 配置 settings。
- 配置 urls。
- 配置基础模板。

验收标准：

- 本地可以启动 Django。
- 浏览器可以打开登录页。
- Django Admin 可以访问。

### 10.2 第 2 步：创建核心模型

任务：

- 创建 `family_core` 模型。
- 创建 `portfolio` 核心模型。
- 创建 `ledger` 核心模型。
- 创建 `ai_analysis` 模型。
- 执行迁移。

验收标准：

- 数据库表创建成功。
- Django Admin 可以看到这些模型。

### 10.3 第 3 步：完善 Admin

任务：

- 注册所有模型。
- 配置列表显示。
- 配置筛选条件。
- 配置搜索字段。

验收标准：

- 可以在后台录入家庭成员。
- 可以在后台录入投资账户、持仓、交易。
- 可以在后台录入银行账户、收入、支出。

### 10.4 第 4 步：开发基础页面

任务：

- 首页仪表盘。
- 投资概览。
- 投资账户列表。
- 持仓列表。
- 交易记录列表。
- 账本概览。
- 银行账户列表。
- 收入记录列表。
- 支出记录列表。

验收标准：

- 登录后能从导航进入各页面。
- 页面能显示后台录入的数据。
- 基础筛选可用。

### 10.5 第 5 步：开发录入表单

任务：

- 投资账户新增/编辑。
- 持仓新增/编辑。
- 交易新增/编辑。
- 银行账户新增/编辑。
- 收入新增/编辑。
- 支出新增/编辑。

验收标准：

- 不进入 Admin 也可以完成日常数据录入。

### 10.6 第 6 步：开发基础统计

任务：

- 投资总资产统计。
- 投资账户分布。
- 持仓市值统计。
- 本月收入统计。
- 本月支出统计。
- 本月结余统计。
- 支出分类统计。

验收标准：

- 首页能看到家庭资产和收支摘要。
- 投资页能看到持仓和账户汇总。
- 账本页能看到月度收支。

### 10.7 第 7 步：接入图表

任务：

- 加入 ECharts。
- 首页资产构成图。
- 投资账户分布图。
- 支出分类图。
- 最近 6 个月收支图。

验收标准：

- 图表能正常显示。
- 数据来自数据库统计，不是写死的假数据。

### 10.8 第 8 步：AI 分析占位

任务：

- AI 服务商后台配置。
- AI 分析请求页面。
- AI 分析历史页面。
- AI 结果详情页面。
- `services.py` 预留调用接口。

验收标准：

- 可以保存一次分析请求。
- 可以在历史记录里看到请求。
- 可以手动录入或保存一条分析结果。

### 10.9 第 9 步：Docker 部署

任务：

- 编写 Dockerfile。
- 编写 docker-compose.yml。
- 编写 .env.example。
- 在本地或 NAS 上启动容器。

验收标准：

- PostgreSQL 启动成功。
- Django Web 启动成功。
- 可以访问网站。
- 可以登录 Admin。

---

## 11. 群晖 NAS 操作路线

### 11.1 群晖准备

在群晖 DSM 中：

1. 安装 Container Manager。
2. 创建目录 `/volume1/docker/family-finance/`。
3. 创建子目录：

```text
/volume1/docker/family-finance/app
/volume1/docker/family-finance/postgres
/volume1/docker/family-finance/media
/volume1/docker/family-finance/staticfiles
/volume1/docker/family-finance/backups
```

### 11.2 上传项目

把项目文件放到：

```text
/volume1/docker/family-finance/
```

确保有：

```text
docker-compose.yml
.env
app/Dockerfile
app/requirements.txt
app/manage.py
```

### 11.3 首次启动

在 Container Manager 或 SSH 中执行：

```bash
docker compose up -d --build
```

查看容器：

```bash
docker compose ps
```

查看日志：

```bash
docker compose logs -f web
```

### 11.4 创建管理员账号

进入 web 容器：

```bash
docker compose exec web python manage.py createsuperuser
```

然后访问：

```text
http://群晖IP:8000/admin/
```

### 11.5 首次录入顺序

建议按这个顺序录入：

1. 币种：CNY、HKD、USD。
2. 家庭。
3. 家庭成员。
4. 投资账户。
5. 证券标的。
6. 持仓。
7. 银行账户。
8. 收入分类。
9. 支出分类。
10. 收入记录。
11. 支出记录。

---

## 12. 备份方案第一版

### 12.1 手动备份命令

```bash
docker compose exec db pg_dump -U family_finance_user family_finance > backups/family_finance_backup.sql
```

注意：在群晖上使用时，重定向路径要根据实际执行目录确认。

### 12.2 推荐备份目录

```text
/volume1/docker/family-finance/backups
```

### 12.3 后续自动备份

第二阶段建议增加：

- 每日自动备份数据库。
- 保留最近 30 天。
- 每周保留一份长期备份。
- 备份目录加入群晖 Hyper Backup。

---

## 13. 第一阶段验收标准

MVP 完成时应达到：

- 能通过浏览器访问网站。
- 能登录和退出。
- 能在后台管理家庭成员。
- 能录入投资账户、证券、持仓、交易。
- 能录入银行账户、收入、支出。
- 首页能展示家庭资产和收支摘要。
- 投资页能展示账户、持仓、盈亏。
- 账本页能展示收入、支出、结余。
- 至少 3 个图表可正常显示。
- AI 分析模块有入口，可保存分析请求。
- 能用 Docker Compose 在群晖 NAS 上启动。
- 数据库数据保存在 NAS 挂载目录，而不是容器内部。

---

## 14. 建议下一步

下一步可以开始真正创建 Django 项目。

建议先实现：

1. 项目骨架。
2. Docker Compose。
3. `family_core`、`portfolio`、`ledger`、`ai_analysis` 的第一批模型。
4. Django Admin。

完成这一步后，你就可以先通过后台把家庭、账户、持仓、收入支出录进去，网站的“地基”就立起来了。

