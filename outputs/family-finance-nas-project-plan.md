# 家庭投资与记账工作台：项目需求说明书 + 数据库初版表结构 + NAS 部署路线图

版本：v0.1  
日期：2026-06-16  
部署目标：群晖 NAS  
建议技术栈：Django + PostgreSQL + Docker Compose + Nginx + ECharts

---

## 1. 项目目标

本项目目标是在群晖 NAS 上搭建一个家庭内部使用的网站，用于统一管理家庭投资、收支账本、港股打新、投资复盘、宏观经济数据，并在此基础上接入 AI API，辅助分析投资组合、收支结构、打新策略、股票买卖和复盘笔记。

系统应满足以下要求：

- 支持多个家庭成员分别录入和查看自己的数据。
- 支持家庭层面的汇总统计和图表展示。
- 支持后续逐步新增、删除或调整数据库字段。
- 支持部署在群晖 NAS 上，数据长期本地保存。
- 支持数据库定期备份，降低误删和硬盘故障风险。
- 支持预留行情、新闻、宏观数据、AI 分析等外部接口。
- 对编程初学者友好，优先选择稳定、资料丰富、易部署的方案。

---

## 2. 推荐技术架构

### 2.1 总体架构

建议采用：

- 前端页面：Django Templates + Bootstrap
- 后端框架：Django
- 数据库：PostgreSQL
- 图表库：ECharts
- 部署方式：Docker Compose
- 反向代理：Nginx
- 后台管理：Django Admin
- 定时任务：第一阶段可暂不启用，后续引入 Celery 或 Django 定时任务
- AI 接口：后端统一封装，不让前端直接调用 AI API

### 2.2 为什么推荐 Django

Django 适合本项目的原因：

- 自带用户系统、权限系统和后台管理页面。
- 数据库迁移机制成熟，后续增删字段比较方便。
- 适合做数据录入、查询、统计和管理后台。
- 中文资料较多，适合初学者长期维护。
- 可以先做简单页面，后续再升级为更复杂的前端。

### 2.3 为什么推荐 PostgreSQL

PostgreSQL 适合本项目的原因：

- 稳定可靠，适合长期保存家庭财务数据。
- 支持 JSONB 字段，便于预留自定义字段。
- 查询能力强，适合做统计和图表。
- 与 Django 配合成熟。
- 在群晖 Docker 环境中部署方便。

---

## 3. 分阶段建设计划

### 3.1 第一阶段：最小可用版本 MVP

目标：先做出一个可以录入、查询、汇总、画图的网站。

包含功能：

- 家庭成员管理
- 投资账户管理
- 投资持仓录入
- 投资交易记录录入
- 银行账户管理
- 收入记录
- 支出记录
- 个人资产概览
- 家庭资产概览
- 基础图表展示
- AI 分析接口占位
- 港股打新、投资复盘、宏观数据模块先预留菜单和基础表

不建议第一阶段做太复杂的内容：

- 自动抓取行情
- 自动抓取新闻
- 港股新股完整数据源接入
- 复杂权限流转
- 手机 App
- 复杂前端框架

### 3.2 第二阶段：港股打新与投资复盘

包含功能：

- 港股新股资料录入或导入
- 认购倍数、回拨、暗盘、首日涨跌幅、成交量等记录
- 多账户打新策略记录
- 中签、卖出、收益统计
- 打新复盘
- 投资笔记和交易复盘
- AI 总结复盘经验

### 3.3 第三阶段：外部数据与 AI 分析增强

包含功能：

- 股票新闻接口
- 行情数据接口
- 中国和美国宏观经济数据
- AI 分析投资组合
- AI 分析家庭收支结构
- AI 分析港股打新策略
- AI 辅助生成复盘总结
- AI 分析历史交易中的问题

### 3.4 第四阶段：体验优化与自动化

包含功能：

- 自动备份
- 定时更新行情、新闻、宏观数据
- 邮件或企业微信提醒
- 移动端适配优化
- 更精细的权限设置
- 数据导入导出
- 图表仪表盘增强

---

## 4. 权限与用户模型

### 4.1 用户类型

建议区分：

- 系统管理员：维护系统、管理家庭成员、查看全部数据。
- 家庭成员：录入和查看自己的数据。
- 家庭查看者：只能查看汇总或部分数据，不能修改。

### 4.2 数据可见范围

每条核心数据建议都有：

- 所属家庭
- 所属成员
- 可见范围

可见范围建议：

- private：仅本人和管理员可见
- family：家庭成员可见
- admin_only：仅管理员可见

### 4.3 初期简化建议

第一阶段可以先做：

- 一个家庭
- 多个成员
- 管理员能看全部
- 成员默认只能看自己的明细
- 家庭汇总页面展示合计数据

---

## 5. 数据库设计原则

### 5.1 字段可变的处理方式

你已经明确提出后续字段可能新增或删减，因此数据库建议采用“核心字段 + 扩展字段”的方式。

每张重要表建议包含：

- 核心字段：稳定、经常查询、需要统计的字段。
- remark：备注。
- extra_data：JSONB 类型，用于保存临时字段、自定义字段、特殊券商字段等。
- created_at：创建时间。
- updated_at：更新时间。
- deleted_at：软删除时间，可选。
- is_active：是否有效，可选。

### 5.2 为什么使用 JSONB

例如某个券商账户有特殊字段：

```json
{
  "broker_risk_level": "R3",
  "account_manager": "张三",
  "custom_tag": "长期账户"
}
```

这类字段不一定每个账户都有，也不一定会长期保留，就可以放在 `extra_data` 中。

### 5.3 不建议什么都放 JSONB

以下字段不建议放 JSONB：

- 金额
- 日期
- 成员 ID
- 股票代码
- 账户 ID
- 分类 ID
- 交易方向
- 币种

这些字段经常查询、统计、排序，应使用正式字段。

---

## 6. 数据库初版表结构

下面是初版逻辑表结构，后续实际开发时会转成 Django Model 和 PostgreSQL 表。

### 6.1 通用基础表

#### families 家庭表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| name | varchar | 家庭名称 |
| base_currency | varchar | 默认本位币，例如 CNY |
| remark | text | 备注 |
| extra_data | jsonb | 扩展字段 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

#### family_members 家庭成员表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| family_id | bigint | 所属家庭 |
| user_id | bigint | 关联 Django 用户 |
| display_name | varchar | 显示名称 |
| role | varchar | 角色：admin/member/viewer |
| is_active | boolean | 是否有效 |
| remark | text | 备注 |
| extra_data | jsonb | 扩展字段 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

#### currencies 币种表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| code | varchar | CNY/HKD/USD |
| name | varchar | 币种名称 |
| symbol | varchar | 符号 |
| is_active | boolean | 是否启用 |

#### exchange_rates 汇率表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| base_currency | varchar | 基准币种 |
| quote_currency | varchar | 目标币种 |
| rate | decimal | 汇率 |
| rate_date | date | 日期 |
| source | varchar | 数据来源 |
| extra_data | jsonb | 扩展字段 |
| created_at | datetime | 创建时间 |

---

## 7. 投资组合模块

### 7.1 功能需求

支持：

- 成员录入证券账户。
- 成员录入持仓。
- 成员录入交易记录。
- 查询个人账户余额、持仓、市值、成本、盈亏。
- 查询家庭总资产、总持仓、总盈亏。
- 按成员、账户、市场、币种、股票代码筛选。
- 生成资产分布、行业分布、币种分布、收益曲线等图表。
- 预留股票新闻接口。

### 7.2 数据表

#### investment_accounts 投资账户表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| family_id | bigint | 所属家庭 |
| member_id | bigint | 所属成员 |
| broker_name | varchar | 券商名称 |
| account_name | varchar | 账户名称 |
| account_no_masked | varchar | 脱敏账号 |
| market_scope | varchar | A股/港股/美股/基金/综合 |
| currency | varchar | 主要币种 |
| cash_balance | decimal | 现金余额 |
| visibility | varchar | private/family/admin_only |
| is_active | boolean | 是否有效 |
| remark | text | 备注 |
| extra_data | jsonb | 扩展字段 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

#### securities 证券标的表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| symbol | varchar | 代码，例如 00700.HK |
| name | varchar | 名称 |
| market | varchar | HK/US/CN |
| asset_type | varchar | stock/fund/etf/bond/cash/other |
| currency | varchar | 交易币种 |
| industry | varchar | 行业 |
| is_active | boolean | 是否有效 |
| extra_data | jsonb | 扩展字段 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

#### investment_positions 投资持仓表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| account_id | bigint | 投资账户 |
| security_id | bigint | 证券标的 |
| quantity | decimal | 持仓数量 |
| avg_cost | decimal | 平均成本 |
| current_price | decimal | 当前价格 |
| market_value | decimal | 当前市值 |
| unrealized_pnl | decimal | 浮动盈亏 |
| pnl_ratio | decimal | 盈亏比例 |
| position_date | date | 持仓日期 |
| remark | text | 备注 |
| extra_data | jsonb | 扩展字段 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

#### investment_transactions 投资交易记录表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| account_id | bigint | 投资账户 |
| security_id | bigint | 证券标的 |
| trade_date | date | 交易日期 |
| trade_type | varchar | buy/sell/dividend/fee/transfer |
| quantity | decimal | 数量 |
| price | decimal | 价格 |
| amount | decimal | 成交金额 |
| fee | decimal | 手续费 |
| tax | decimal | 税费 |
| currency | varchar | 币种 |
| realized_pnl | decimal | 已实现盈亏 |
| remark | text | 备注 |
| extra_data | jsonb | 扩展字段 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

#### portfolio_snapshots 投资组合快照表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| family_id | bigint | 所属家庭 |
| member_id | bigint | 所属成员，可为空表示家庭汇总 |
| account_id | bigint | 投资账户，可为空 |
| snapshot_date | date | 快照日期 |
| total_cash | decimal | 现金 |
| total_market_value | decimal | 持仓市值 |
| total_asset | decimal | 总资产 |
| total_cost | decimal | 总成本 |
| total_pnl | decimal | 总盈亏 |
| pnl_ratio | decimal | 盈亏比例 |
| currency | varchar | 币种 |
| extra_data | jsonb | 扩展字段 |
| created_at | datetime | 创建时间 |

#### security_news 股票新闻缓存表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| security_id | bigint | 证券标的 |
| title | varchar | 新闻标题 |
| summary | text | 摘要 |
| url | text | 链接 |
| source | varchar | 来源 |
| published_at | datetime | 发布时间 |
| sentiment | varchar | 情绪：positive/neutral/negative |
| raw_data | jsonb | 原始数据 |
| created_at | datetime | 创建时间 |

---

## 8. 家庭收支账本模块

### 8.1 功能需求

支持：

- 成员录入银行账户。
- 成员录入工资收入。
- 成员录入日常支出。
- 支持收入和支出分类。
- 查询个人银行余额、工资收入、支出结构。
- 查询家庭总收入、总支出、结余。
- 按成员、账户、分类、月份、币种筛选。
- 生成收入趋势、支出分类、月度结余、家庭现金流图表。

### 8.2 数据表

#### bank_accounts 银行账户表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| family_id | bigint | 所属家庭 |
| member_id | bigint | 所属成员 |
| bank_name | varchar | 银行名称 |
| account_name | varchar | 账户名称 |
| account_no_masked | varchar | 脱敏账号 |
| account_type | varchar | debit/credit/savings/cash/other |
| currency | varchar | 币种 |
| balance | decimal | 当前余额 |
| visibility | varchar | private/family/admin_only |
| is_active | boolean | 是否有效 |
| remark | text | 备注 |
| extra_data | jsonb | 扩展字段 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

#### income_categories 收入分类表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| family_id | bigint | 所属家庭 |
| name | varchar | 分类名称 |
| parent_id | bigint | 父分类，可为空 |
| is_active | boolean | 是否有效 |
| extra_data | jsonb | 扩展字段 |

#### expense_categories 支出分类表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| family_id | bigint | 所属家庭 |
| name | varchar | 分类名称 |
| parent_id | bigint | 父分类，可为空 |
| is_active | boolean | 是否有效 |
| extra_data | jsonb | 扩展字段 |

#### income_records 收入记录表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| family_id | bigint | 所属家庭 |
| member_id | bigint | 所属成员 |
| bank_account_id | bigint | 入账账户 |
| category_id | bigint | 收入分类 |
| income_date | date | 收入日期 |
| amount | decimal | 金额 |
| currency | varchar | 币种 |
| source_name | varchar | 来源，例如工资、奖金、利息 |
| is_recurring | boolean | 是否周期收入 |
| visibility | varchar | private/family/admin_only |
| remark | text | 备注 |
| extra_data | jsonb | 扩展字段 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

#### expense_records 支出记录表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| family_id | bigint | 所属家庭 |
| member_id | bigint | 所属成员 |
| bank_account_id | bigint | 支出账户 |
| category_id | bigint | 支出分类 |
| expense_date | date | 支出日期 |
| amount | decimal | 金额 |
| currency | varchar | 币种 |
| merchant | varchar | 商户或对象 |
| payment_method | varchar | 支付方式 |
| visibility | varchar | private/family/admin_only |
| remark | text | 备注 |
| extra_data | jsonb | 扩展字段 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

#### cashflow_monthly_summaries 月度现金流汇总表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| family_id | bigint | 所属家庭 |
| member_id | bigint | 所属成员，可为空表示家庭汇总 |
| year | int | 年 |
| month | int | 月 |
| total_income | decimal | 总收入 |
| total_expense | decimal | 总支出 |
| net_cashflow | decimal | 净现金流 |
| currency | varchar | 币种 |
| extra_data | jsonb | 扩展字段 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

---

## 9. 港股打新模块

### 9.1 功能需求

支持：

- 记录即将上市的新股信息。
- 记录招股价、上市日期、保荐人、行业等基本信息。
- 记录超额认购倍数、回拨情况、一手中签率。
- 记录暗盘涨跌幅、首日涨跌幅、成交量等。
- 录入多个账户的打新策略。
- 记录最终中签、卖出、收益。
- 支持复盘和策略总结。

### 9.2 数据表

#### hk_ipo_listings 港股新股表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| stock_code | varchar | 股票代码 |
| company_name | varchar | 公司名称 |
| industry | varchar | 行业 |
| sponsor | varchar | 保荐人 |
| offer_price_min | decimal | 招股价下限 |
| offer_price_max | decimal | 招股价上限 |
| final_offer_price | decimal | 最终定价 |
| lot_size | int | 每手股数 |
| application_start_date | date | 招股开始日 |
| application_end_date | date | 招股截止日 |
| listing_date | date | 上市日期 |
| status | varchar | upcoming/listed/cancelled |
| remark | text | 备注 |
| extra_data | jsonb | 扩展字段 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

#### hk_ipo_market_stats 港股新股市场表现表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| ipo_id | bigint | 新股 ID |
| oversubscription_rate | decimal | 公开发售超购倍数 |
| one_lot_success_rate | decimal | 一手中签率 |
| grey_market_price | decimal | 暗盘价格 |
| grey_market_change_pct | decimal | 暗盘涨跌幅 |
| first_day_open | decimal | 首日开盘价 |
| first_day_close | decimal | 首日收盘价 |
| first_day_change_pct | decimal | 首日涨跌幅 |
| first_day_volume | decimal | 首日成交量 |
| first_day_turnover | decimal | 首日成交额 |
| order_book_summary | jsonb | 盘口信息摘要 |
| raw_data | jsonb | 原始数据 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

#### hk_ipo_accounts 港股打新账户表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| family_id | bigint | 所属家庭 |
| member_id | bigint | 所属成员 |
| broker_name | varchar | 券商 |
| account_name | varchar | 账户名称 |
| account_no_masked | varchar | 脱敏账号 |
| financing_available | boolean | 是否支持融资 |
| is_active | boolean | 是否有效 |
| remark | text | 备注 |
| extra_data | jsonb | 扩展字段 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

#### hk_ipo_strategies 港股打新策略表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| ipo_id | bigint | 新股 ID |
| account_id | bigint | 打新账户 |
| apply_lots | int | 申购手数 |
| apply_amount | decimal | 申购金额 |
| financing_ratio | decimal | 融资比例 |
| expected_risk_level | varchar | 预期风险等级 |
| strategy_reason | text | 策略理由 |
| final_decision | varchar | apply/skip/watch |
| remark | text | 备注 |
| extra_data | jsonb | 扩展字段 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

#### hk_ipo_results 港股打新结果表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| strategy_id | bigint | 对应策略 |
| allocated_lots | int | 中签手数 |
| allocated_shares | int | 中签股数 |
| sell_price | decimal | 卖出价 |
| sell_date | date | 卖出日期 |
| gross_profit | decimal | 毛收益 |
| fees | decimal | 费用 |
| net_profit | decimal | 净收益 |
| review_note | text | 复盘 |
| extra_data | jsonb | 扩展字段 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

---

## 10. 投资经验记录与复盘模块

### 10.1 功能需求

支持：

- 记录投资笔记。
- 记录交易复盘。
- 给笔记打标签。
- 关联股票、交易记录、打新记录。
- 支持 AI 总结经验教训。

### 10.2 数据表

#### investment_notes 投资笔记表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| family_id | bigint | 所属家庭 |
| member_id | bigint | 作者 |
| title | varchar | 标题 |
| content | text | 内容 |
| note_type | varchar | general/trade_review/ipo_review/macro/other |
| visibility | varchar | private/family/admin_only |
| related_security_id | bigint | 关联证券，可为空 |
| related_transaction_id | bigint | 关联交易，可为空 |
| related_ipo_id | bigint | 关联新股，可为空 |
| tags | jsonb | 标签 |
| ai_summary | text | AI 总结 |
| remark | text | 备注 |
| extra_data | jsonb | 扩展字段 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

---

## 11. 中美宏观经济数据模块

### 11.1 功能需求

第一阶段只预留基础结构，后续再细化。

未来可支持：

- 中国宏观指标。
- 美国宏观指标。
- 利率、通胀、就业、PMI、GDP、社融、货币供应等。
- 数据图表展示。
- AI 解读宏观趋势。

### 11.2 数据表

#### macro_indicators 宏观指标表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| country | varchar | CN/US |
| code | varchar | 指标代码 |
| name | varchar | 指标名称 |
| category | varchar | 分类 |
| frequency | varchar | daily/monthly/quarterly/yearly |
| unit | varchar | 单位 |
| source | varchar | 数据来源 |
| is_active | boolean | 是否启用 |
| extra_data | jsonb | 扩展字段 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

#### macro_data_points 宏观数据点表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| indicator_id | bigint | 指标 ID |
| period_date | date | 数据日期 |
| value | decimal | 数值 |
| revised_value | decimal | 修正值 |
| release_date | date | 发布日期 |
| raw_data | jsonb | 原始数据 |
| created_at | datetime | 创建时间 |

---

## 12. AI 协助分析模块

### 12.1 功能需求

支持：

- 选择分析模块：投资组合、收支账本、港股打新、投资复盘、宏观数据。
- 选择分析范围：个人、家庭、某账户、某股票、某时间段。
- 选择 AI 服务商。
- 保存 AI 分析请求和结果。
- 支持后续对比不同 AI 的分析结果。

### 12.2 关键安全原则

AI 不应直接连接数据库。

推荐方式：

1. 用户在页面选择分析范围。
2. 后端根据权限读取必要数据。
3. 后端脱敏和汇总数据。
4. 后端构造 Prompt。
5. 后端调用 AI API。
6. 保存分析结果。

这样可以避免：

- 泄露全部数据库。
- 泄露账号、姓名等隐私字段。
- AI 误读或越权读取数据。

### 12.3 数据表

#### ai_providers AI 服务商表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| name | varchar | 服务商名称 |
| provider_type | varchar | openai/anthropic/google/local/other |
| base_url | varchar | API 地址 |
| model_name | varchar | 默认模型 |
| is_active | boolean | 是否启用 |
| extra_data | jsonb | 扩展字段 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

#### ai_analysis_requests AI 分析请求表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| family_id | bigint | 所属家庭 |
| member_id | bigint | 发起成员 |
| provider_id | bigint | AI 服务商 |
| module | varchar | portfolio/ledger/ipo/notes/macro/other |
| analysis_type | varchar | 分析类型 |
| scope | jsonb | 分析范围 |
| prompt | text | 提交给 AI 的提示词 |
| sanitized_input | jsonb | 脱敏后的输入数据 |
| status | varchar | pending/success/failed |
| error_message | text | 错误信息 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

#### ai_analysis_results AI 分析结果表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| request_id | bigint | 请求 ID |
| result_text | text | 分析结果 |
| result_json | jsonb | 结构化结果 |
| tokens_used | int | Token 用量 |
| cost_estimate | decimal | 费用估算 |
| created_at | datetime | 创建时间 |

---

## 13. 其他模块预留

#### custom_modules 自定义模块表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| family_id | bigint | 所属家庭 |
| name | varchar | 模块名称 |
| description | text | 描述 |
| config | jsonb | 配置 |
| is_active | boolean | 是否启用 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

#### custom_records 自定义记录表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| module_id | bigint | 所属模块 |
| member_id | bigint | 所属成员 |
| title | varchar | 标题 |
| record_date | date | 日期 |
| data | jsonb | 记录内容 |
| remark | text | 备注 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

---

## 14. 关键索引建议

为保证 NAS 上查询速度，建议后续建立以下索引：

### 14.1 投资模块

- investment_accounts：family_id, member_id
- investment_positions：account_id, security_id, position_date
- investment_transactions：account_id, security_id, trade_date
- portfolio_snapshots：family_id, member_id, snapshot_date
- securities：symbol, market

### 14.2 账本模块

- bank_accounts：family_id, member_id
- income_records：family_id, member_id, income_date
- expense_records：family_id, member_id, expense_date
- income_records：category_id
- expense_records：category_id
- cashflow_monthly_summaries：family_id, member_id, year, month

### 14.3 港股打新模块

- hk_ipo_listings：stock_code, listing_date, status
- hk_ipo_strategies：ipo_id, account_id
- hk_ipo_results：strategy_id

### 14.4 AI 模块

- ai_analysis_requests：family_id, member_id, module, created_at
- ai_analysis_results：request_id

---

## 15. NAS 数据库优化建议

### 15.1 PostgreSQL 容器设置

建议：

- 数据目录挂载到 NAS 固定目录。
- 不把数据库放在临时容器内部。
- 定期备份数据库。
- 控制日志大小。
- 初期不需要复杂集群。

建议目录：

```text
/volume1/docker/family-finance/postgres
/volume1/docker/family-finance/media
/volume1/docker/family-finance/backups
/volume1/docker/family-finance/nginx
```

### 15.2 数据规模预估

家庭场景下数据量不会特别大：

- 收支记录：每天 20 条，10 年约 73,000 条。
- 投资交易：每年 1,000 条，10 年约 10,000 条。
- 持仓快照：每天 100 个标的，10 年约 365,000 条。

PostgreSQL 可以轻松承载。

### 15.3 性能建议

- 金额字段使用 decimal，不使用 float。
- 日期字段单独建索引。
- 家庭 ID 和成员 ID 常用筛选字段要建索引。
- 统计汇总可以用月度汇总表，避免每次都扫描明细。
- 新闻、行情、宏观数据原始返回可以放 raw_data。
- 附件、图片不建议存数据库，建议存文件系统，数据库只保存路径。

### 15.4 备份建议

至少配置：

- 每日数据库备份。
- 每周完整备份。
- 每月长期备份。
- 备份目录纳入群晖 Hyper Backup。
- 重要升级前手动备份一次。

---

## 16. NAS 部署路线图

### 16.1 准备工作

需要准备：

- 一台群晖 NAS。
- 已安装 DSM。
- 已安装 Container Manager 或 Docker。
- 一个用于网站的共享文件夹或 Docker 目录。
- 一个局域网访问地址，例如 `http://nas-ip:8000`。

### 16.2 推荐目录结构

在 NAS 上创建：

```text
/volume1/docker/family-finance/
  app/
  postgres/
  media/
  static/
  backups/
  nginx/
  docker-compose.yml
```

说明：

- app：网站代码。
- postgres：数据库数据。
- media：上传附件。
- static：静态文件。
- backups：数据库备份。
- nginx：反向代理配置。

### 16.3 Docker Compose 初版结构

后续正式开发时可以使用类似结构：

```yaml
services:
  db:
    image: postgres:16
    container_name: family_finance_db
    restart: unless-stopped
    environment:
      POSTGRES_DB: family_finance
      POSTGRES_USER: family_finance_user
      POSTGRES_PASSWORD: change_me_to_a_strong_password
    volumes:
      - ./postgres:/var/lib/postgresql/data
    ports:
      - "5432:5432"

  web:
    build: ./app
    container_name: family_finance_web
    restart: unless-stopped
    environment:
      DATABASE_URL: postgres://family_finance_user:change_me_to_a_strong_password@db:5432/family_finance
      DJANGO_SECRET_KEY: change_me_to_a_random_secret
      DJANGO_DEBUG: "False"
      DJANGO_ALLOWED_HOSTS: localhost,127.0.0.1,nas-ip
    volumes:
      - ./media:/app/media
      - ./static:/app/staticfiles
    depends_on:
      - db
    ports:
      - "8000:8000"

  nginx:
    image: nginx:stable
    container_name: family_finance_nginx
    restart: unless-stopped
    volumes:
      - ./nginx:/etc/nginx/conf.d
      - ./static:/static
      - ./media:/media
    depends_on:
      - web
    ports:
      - "8080:80"
```

注意：

- 密码和密钥后续必须修改。
- 端口可以根据群晖实际占用情况调整。
- 第一阶段可以先不用 nginx，直接访问 web 的 8000 端口。

### 16.4 部署步骤

第一轮部署建议：

1. 在群晖安装 Container Manager。
2. 创建 `/volume1/docker/family-finance/` 目录。
3. 放入项目代码和 `docker-compose.yml`。
4. 启动 PostgreSQL 容器。
5. 启动 Django Web 容器。
6. 执行数据库迁移。
7. 创建管理员账号。
8. 在浏览器访问网站。
9. 先通过 Django Admin 录入基础数据。
10. 再开发正式页面。

### 16.5 后续正式上线建议

局域网自用：

- 可以只在家庭内网访问。
- 不建议一开始暴露到公网。

如需外网访问：

- 优先使用 Tailscale、ZeroTier、群晖 VPN 等方式。
- 不建议直接把管理后台暴露到公网。
- 必须开启 HTTPS。
- 必须使用强密码和二次验证。

---

## 17. 初版页面规划

### 17.1 导航菜单

建议菜单：

- 首页仪表盘
- 投资组合
- 家庭账本
- 港股打新
- 投资复盘
- 宏观数据
- AI 分析
- 系统设置

### 17.2 首页仪表盘

展示：

- 家庭总资产
- 投资资产
- 银行现金
- 本月收入
- 本月支出
- 本月结余
- 投资盈亏
- 最近交易
- 最近支出
- AI 分析入口

### 17.3 投资组合页面

展示：

- 账户列表
- 持仓列表
- 股票/基金搜索
- 盈亏统计
- 资产分布图
- 币种分布图
- 行业分布图

### 17.4 家庭账本页面

展示：

- 银行账户列表
- 收入记录
- 支出记录
- 月度收支图
- 支出分类饼图
- 家庭结余趋势

### 17.5 AI 分析页面

展示：

- 分析模块选择
- 时间范围选择
- 分析问题输入框
- AI 服务商选择
- 分析结果
- 历史分析记录

---

## 18. 开发顺序建议

推荐按以下顺序开发：

1. 创建 Django 项目。
2. 配置 PostgreSQL。
3. 创建用户和家庭成员模型。
4. 创建投资账户、证券标的、持仓、交易记录模型。
5. 创建银行账户、收入、支出模型。
6. 接入 Django Admin。
7. 完成基础录入。
8. 完成首页仪表盘。
9. 完成投资组合统计。
10. 完成账本统计。
11. 接入 ECharts。
12. 增加 AI 分析请求和结果表。
13. 增加 AI API 调用封装。
14. 增加港股打新模块。
15. 增加投资复盘模块。
16. 增加宏观数据模块。
17. 做 NAS 备份和安全加固。

---

## 19. 风险与注意事项

### 19.1 数据安全

这是家庭财务系统，数据非常敏感。

必须注意：

- 不要把数据库密码写到公开仓库。
- 不要把 AI API Key 写到前端。
- 不要让 AI 直接访问数据库。
- 不要一开始直接公网开放。
- 定期备份。

### 19.2 数据准确性

投资系统容易出现：

- 汇率问题。
- 分红问题。
- 拆股合股问题。
- 手续费问题。
- 多币种换算问题。
- 持仓和交易记录不一致问题。

第一阶段建议先用手动录入和简单统计，后续再逐步增强。

### 19.3 AI 分析边界

AI 可以辅助分析，但不应作为自动决策工具。

建议页面明确：

- AI 分析仅供参考。
- 投资决策由用户自行承担。
- AI 可能出现错误或遗漏。

---

## 20. 下一步执行计划

建议下一步做《第一阶段 MVP 开发清单》，内容包括：

- Django 项目目录结构。
- 每个 app 的划分。
- 第一批 Django Models。
- 第一批后台管理页面。
- 第一批 URL 和页面。
- Docker Compose 文件。
- 本地启动步骤。
- 群晖部署步骤。

建议第一阶段 Django app 划分：

```text
family_core     家庭、成员、权限、币种
portfolio       投资账户、证券、持仓、交易、快照
ledger          银行账户、收入、支出、分类
ipo             港股打新
notes           投资笔记和复盘
macro           宏观数据
ai_analysis     AI 服务商、分析请求、分析结果
dashboard       首页仪表盘
```

完成 MVP 后，再逐步加入行情、新闻、宏观数据源和更完整的 AI 分析能力。

