# 期权车轮策略模块：技术设计

状态：Draft 0.1
更新时间：2026-08-30

## 1. 现有系统基线

- `portfolio.Security`、`OptionContract`、`InvestmentTransaction` 和 `InvestmentPosition` 已支持正股、
  期权、开平仓、到期处理和指派产生的正股交易。
- `portfolio` 已通过 Futu OpenD 获取股票行情，但尚无期权链、期权实时字段或订阅池。
- `SecurityMarketSnapshot` 适合估值用最新价格，不适合保存交易决策所需的不可变 Bid/Ask、Greeks、
  OI、IV 和盘口观察。
- `intelligence` 已有来源、事件、证据、版本化 AI 分析和引用校验，可复用其治理方法。
- `ai_analysis.AiProvider` 可管理文本模型，但现有公开情报授权不代表已授权发送私人投资数据。
- 交易流水是唯一会计事实源；策略建议和预估成交不得写入交易表。

## 2. 架构决策

### ADR-001：建立独立 `option_wheel` 应用

`option_wheel` 管理策略政策、暂停、周期、腿、资源占用、市场决策快照、候选、AI 解释和运行记录。
它通过外键引用 `portfolio` 和 `intelligence`，不复制交易、持仓或事件事实。

### ADR-002：建议、选择、订单和成交硬隔离

- `WheelCandidate` 是规则生成的候选。
- `WheelDecision` 保存人工选择。
- 券商订单不在第一版范围内。
- `InvestmentTransaction` 只在真实成交或实际结算被用户确认/导入后创建。
- `WheelTransactionLink` 只建立引用，不反向修改交易金额、数量或成本。

### ADR-003：全额现金担保是硬约束

系统不读取或使用融资、组合保证金放大值。Put 担保按最保守的全额行权资金计算。资格计算以券商
账户容量快照和真实未平仓义务为依据；候选不预留资源，保存选择也不是订单。只有真实成交/指派关联
形成硬占用，并使用事务和数据库锁保护，避免部分成交或并发关联导致重复占用。

### ADR-004：采用多目标候选集，不采用单一总分

硬门控后，分别计算净权利金效率、到期价内风险和指派后尾部风险。通过 Pareto 前沿或确定性分层
选择代表方案，AI 不计算最终资格或绕过门控。

### ADR-005：Futu 静态链与动态订阅分离

先获取正股到期日和静态期权链，再按 DTE、行权价和策略范围缩小合约集合；只对候选合约代码订阅
动态 QUOTE。订阅额度按到期链管理，并优先保护真实未平仓合约。

### ADR-006：AI 只解释冻结输入

AI 输入引用不可变决策快照、候选 ID 和证据 ID。默认只发送公开市场与证据数据，不发送账户名称、
现金、总资产或完整持仓。AI 无法取得实时环境，也不能生成交易或订单。

### ADR-007：第一版只支持标准实物交割美股股票期权

只有 provider 合约代码、乘数、交割股数、行权方式和调整状态均明确的标准合约进入候选。调整期权、
现金结算期权和交割物不明确合约进入人工复核。

可执行合约必须同时满足：标准、未调整、非指数、单一股票标的、美式行权、100 股交割且乘数为
100。结算证据只能来自以下两条路径之一：

1. provider 明确返回 `settlement_mode=PHYSICAL`，并将证据标记为 `PROVIDER_PHYSICAL`；
2. provider 返回 N/A、UNKNOWN 或空时，只有上述合约身份条件全部明确，且证据标记为
   `OCC_STANDARD_EQUITY`，才使用 OCC 标准美股期权规格兜底。

Futu 返回 N/A 本身不构成结算证据，不得据此推断为实物交割。官方依据为
[OCC 股票期权产品规格](https://www.theocc.com/clearance-and-settlement/clearing/equity-options-product-specifications)
（标准股票期权通常 100 股、美式、行权或指派交付正股）以及
[Cboe 交易所股票说明](https://www.cboe.com/exchange-traded-stock)
（股票/ETF 期权实物结算与指数期权现金结算的区分）。

### ADR-008：账户容量与 Futu 行情身份分离

Futu OpenD 只提供市场行情和合约能力，不作为致富证券（公户）或盈透证券的资金事实源。当前由只读
适配器读取 `/portfolio/` 中手工维护的对应账户；缺少完整估值、已结算现金、NAV、融资人工确认、
实际持仓、未平仓义务或 `as_of` 时 fail closed。以后券商 API 仅作为可替换的事实源。

## 3. 组件架构

```text
portfolio 交易/持仓事实 ───────────┐
投资组合账户容量适配器 ───────────┤
                                  ├─> option_wheel 规则与周期 ─> 决策快照 ─> 人工选择
Futu OpenD 行情/期权/日历 ────────┤             │
                                  │             └─> 版本化 AI 解释
intelligence 事件与证据 ──────────┘

真实成交/结算 ─> portfolio InvestmentTransaction ─> WheelTransactionLink
```

建议目录：

```text
option_wheel/
  models.py
  selectors.py
  policies.py
  services/
    cycles.py
    collateral.py
    candidates.py
    probabilities.py
    technicals.py
    market_regime.py
    ai_analysis.py
  providers/
    futu_options.py
  management/commands/
  tests/
```

M0 能力探测先放在现有 `portfolio` 管理命令中，因为此时尚未创建 `option_wheel` 业务表，也不应为了
探测接口先引入迁移。

## 4. 数据模型

### 4.1 `WheelPolicy`

粒度：`family + account + underlying`。

关键字段：

- 是否启用；权利金偏好范围；默认 DTE 桶；
- 财报跨越政策；Covered Call 行权价底线政策；
- 单标的最大账户净值比例；流动性与行情新鲜度要求；
- 允许的合约类型和市场；策略版本；创建/更新操作者。

全额现金担保、禁止融资/跨账户担保、标准实物交割合约和资源不可重复占用属于不可放宽的服务端安全
约束，仅管理员可以维护版本。普通成员只能保存更保守的偏好；单次跨财报例外只有在管理员策略允许时
由获授权账户成员明确确认，并保存范围、到期时间和审计记录。

不把约 12 万美元写成固定字段，账户上限从最新可用审计数据取得。

### 4.2 `WheelPause`

粒度可为全局、账户或标的；保存开始/结束、原因、来源（人工/规则）、状态、规则版本和操作者。任一
粒度暂停生效即阻止新卖出候选，解释优先级为全局、账户、标的。人工暂停只能由创建者或管理员显式
解除；规则进入“建议暂停”自动建立硬暂停，达到解除条件后仍需管理员确认恢复。

### 4.3 `WheelCycle`

字段包括账户、正股、来源（Sell Put/已有股票/历史导入）、状态摘要、开始/关闭时间和父子周期。
状态摘要由腿和资源分配派生，不作为单一事实覆盖部分成交或混合状态。

### 4.4 `WheelLeg`

保存 Put/Call、合约引用、计划数量、已成交数量、未平仓数量、已指派数量、已到期数量、已关闭数量、
原腿与 Roll 新腿关系。所有数量使用明确精度。

### 4.5 `WheelTransactionLink`

关联真实 `InvestmentTransaction`，角色包括 Put 开仓/平仓、Call 开仓/平仓、指派期权关闭、指派正股、
股票被收走等。交易值只从 `portfolio` 读取。

### 4.6 `WheelCollateralReservation`

只为已关联的真实未平仓腿按账户和币种保存现金/股票硬占用、数量、来源腿、状态和释放时间。候选及
人工保存选择不写入本表。写入服务使用 `select_for_update` 或等价数据库锁，原子汇总同账户、币种和
标的的已有义务；部分成交、部分关闭和部分指派按实际数量增减。

股票占用必须引用具体 assignment/buy lot；Call 分配数量不得超过对应股票交易的剩余可分配数量。
多个成本批次要么拆成多个 Call 腿，要么使用所覆盖批次中最高每股买入价作为统一行权价下限。

### 4.7 `WheelMarketSnapshot` 与 `WheelOptionQuoteSnapshot`

不可变保存：

- 正股价格、市场时段、来源时间、抓取时间、是否延迟；
- 合约 provider ID、到期日、行权价、类型、乘数、交割股数、行权方式和调整状态；
- Bid/Ask、盘口量、Last、成交量、OI、IV、Greeks、价内外程度；
- Futu 概率、波动率、返回字段和必要的脱敏原始元数据；
- 报价权限、订阅批次、数据质量状态。

估值用 `SecurityMarketSnapshot` 可以继续更新，但车轮决策必须引用自己的不可变快照，不能回退到期权
168 小时手工估值价。

### 4.8 `WheelTechnicalSnapshot` 与 `WheelMarketRegimeSnapshot`

保存使用的 K 线范围、复权方式、样本数、指标值、缺失项、规则版本和市场状态。新上市标的缺少长期
历史时保留缺失，不能填充为中性值。

### 4.9 `WheelDecision` 与 `WheelCandidate`

`WheelDecision` 冻结账户/标的匿名范围、策略版本、市场/技术/事件快照引用、输入指纹和状态。
`WheelCandidate` 保存确定性资格、排除原因、三个主要指标、辅助指标、代表类别和计算明细。

同一输入指纹和决策时点使用唯一幂等键；重新抓取形成新决策，不修改旧决策。

### 4.10 `WheelAiAnalysis`

保存 provider、model、prompt/schema/policy 版本、输入指纹、输入审计快照、结果 JSON、引用、Token、
费用、状态、错误和操作者。重跑新增版本，单个决策最多一个 `is_current` 成功版本。

### 4.11 `WheelRun`

记录链刷新、订阅、候选生成、技术计算和 AI 分析的阶段、锁、数量、额度、错误和耗时。单标的失败不
回滚其他标的，但最终状态必须区分成功、部分和失败。

### 4.12 `WheelBrokerAccountSnapshot`

不可变保存对应投资组合账户、适配器版本、币种、已结算现金、待结算资金、NAV、融资/借贷人工确认、
实际持仓摘要、未平仓义务、来源时点与抓取时点。当前来源固定为本地投资组合事实；Futu 行情连接身份
不得自动映射为投资账户，券商 API 留作以后替换。

候选容量计算只接受完整、未过期且融资/借贷状态明确为未使用的快照。M1 实现前必须确定两个账户的
适配器和时效阈值；无法确定时仅展示 `account data unavailable`，不生成可执行候选。

## 5. 合约身份与公司行动

现有 `OptionContract` 只按正股、类型、行权价和到期日唯一，不能长期支持调整合约。M1 前需要设计
兼容迁移，至少增加：

- 不可变 provider contract ID/code；
- 原始根合约/系列；
- 标准/调整状态；
- 行权方式和结算类型；
- 交割物快照及有效时间；
- 合约乘数与交割股数的版本证据。

历史结算使用当时冻结的合约版本，不使用后来被编辑的可变乘数反算历史。MVP 对非标准合约 fail closed。

## 6. 全额现金与覆盖股票算法

### 6.1 Put 担保

标准美股 Put：

```text
required_cash = strike_price × deliverable_shares × contract_count
```

权利金不从预留金额中扣除；同日未结算权利金不增加可用担保现金。可卖张数为：

```text
min(
  floor(unreserved_settled_cash / cash_per_contract),
  floor((account_nav - existing_underlying_assignment_notional) / assignment_notional_per_contract)
)
```

其中 `existing_underlying_assignment_notional` 汇总同账户同标的已有未平仓 Put 的潜在指派金额；组合
选择时还要加入本次所有拟选候选。账户净值、已结算现金、待结算资金、融资状态或其他占用未知时返回
`unknown/ineligible`。不同账户、币种或股票市值不得补足。

单个候选不锁定资金，同一决策中的候选默认互斥。组合选择、真实成交关联和并发重算必须在同一账户
锁内重新检查全部真实义务；过期的保存选择不能当作可用额度证明。

### 6.2 Covered Call 覆盖

```text
available_contracts = floor(unreserved_underlying_shares / deliverable_shares)
```

只计算同一账户内、按 assignment/buy lot 可追溯的实际股票。默认候选 Call 的行权价必须不低于对应
批次的每股买入价格；混合批次拆腿或采用最高批次成本门槛。历史权利金可显示策略调整成本，但不改变
该硬门槛，也不改写税务或会计成本。

### 6.3 Roll

Roll 是旧腿真实平仓和新腿真实开仓的组合：

```text
roll_net_credit = new_open_credit - old_close_debit - fees - taxes
exposure_days_to_new_expiry = new_expiration - decision_market_date
incremental_extension_days = new_expiration - old_expiration
```

`new_open_credit` 为正收入，`old_close_debit`、费用和税费为正支出；部分成交时两侧按实际匹配数量分别
保存。旧腿确认关闭前不释放担保。新腿必须独立通过现金/股票、财报、流动性和合约身份门控。累计
权利金、本次 Roll 净额、距离新到期日的暴露天数和相对旧到期日的延长天数分开展示。

## 7. Futu 期权数据设计

### 7.1 调用顺序

下列方法名是针对当前本地 Futu SDK 的待探测能力，不是未经验证的字段契约。M0 必须记录实际 SDK
版本、方法是否存在、返回字段、单位、来源和每个字段自己的 `as_of`；任一方法或字段不可用时保存
`unsupported/unknown`，不得用 Delta 或 Last 等字段静默替代。

1. `get_market_snapshot` 获取正股当前状态。
2. `get_option_expiration_date` 获取交易所返回的真实到期日。
3. `get_option_chain` 获取所选 DTE 的静态合约。
4. 按 Put/Call、DTE、行权价范围和标准合约状态预筛。
5. 对预筛合约代码订阅 `SubType.QUOTE`。
6. `get_stock_quote` 与 `get_market_snapshot` 按 M0 已验证的字段来源分别冻结 Bid/Ask、OI、IV、Greeks
   和各自报价时间；不得假设一次调用包含所有字段或具有同一时间戳。
7. 对代表候选调用 `get_option_exercise_probability`、`get_option_volatility`；
   `get_option_screen`/`get_option_seller_screener` 用作字段补充和交叉验证。
8. `request_history_kline` 获取正股复权日 K；`get_earnings_calendar` 获取财报日历线索。
9. 仅退订本运行实际新订阅且归本运行所有的代码，核对额度状态，再关闭连接并记录错误。

### 7.2 订阅优先级

1. 真实未平仓和临近到期合约；
2. 需要 Roll/指派处理的合约；
3. 当前用户查看或请求分析的标的；
4. 已有充足现金/股票资源的候选；
5. 其余观察标的。

调用前后使用 `query_subscription` 记录总额度、已用、剩余和本连接占用。动态探测和刷新使用跨进程锁，
并记录本次拥有的订阅集合；不能退订其他任务或原有会话的代码。额度不足时保存明确跳过原因，不能
静默缩小候选集。

### 7.3 连接与失败策略

- 一个运行批次复用单个 `OpenQuoteContext`；动态模式会改变 OpenD 外部订阅状态，必须显式触发并在
  `finally` 依照当前 SDK 的退订语义清理本次拥有的订阅，再关闭 context。
- 限制每个到期日的订阅合约数，遵循接口频率限制和退避。
- 断线、权限不足、延迟行情、缺字段、宽价差或报价异常时 fail closed。
- 测试使用固定 fixture 和 mock，不在测试时联网。

## 8. 概率与技术分析

### 8.1 概率

保存四类独立字段：

- Futu `strike_probability` / `itm_probability`；
- `otm_probability` / 卖方盈利概率（若接口可用）；
- 期间触及行权价估计；
- 历史类似情形到期价内率、样本数和置信区间。

Delta 仅作辅助。最终真实结果来自交易和结算，不以周五常规时段收盘价直接代替指派事实。

历史回测第一阶段只使用正股价格路径回答“相同行权价距离下是否收于价内”；没有历史期权 Bid/Ask 时
不伪造历史权利金或 Delta。系统从上线后积累每次真实期权快照，逐步建立可审计样本。

### 8.2 技术指标

代码计算而非 AI 计算：

- SMA/EMA 20、50、200；
- RSI 14、MACD 12/26/9；
- ATR 14、20/60 日已实现波动率；
- 20/60 日支撑压力、成交量相对 20 日均量；
- 距离行权价、支撑位和近期高低点的百分比与 ATR 倍数。

全部指标保存样本范围和复权方式。样本不足不计算。

### 8.3 市场状态

规则输入包括大盘/成长股趋势与波动、标的波动、跳空、IV 变化、财报和重大事件。AI 可提取事件严重
程度和不确定性，代码根据版本化进入/解除阈值输出“平静、正常、警戒、建议暂停”。“建议暂停”是新
卖出候选的硬门控；解除条件满足后仍需管理员确认，不能自动恢复。

## 9. AI Schema 边界

建议输出：

```json
{
  "preferred_candidate_id": "candidate-id-or-null",
  "analysis_outcome": "prefer_candidate|wait|manage_existing",
  "horizon_comparison": [],
  "rationale": [],
  "risks": [],
  "invalidation_conditions": [],
  "uncertainties": [],
  "evidence_refs": []
}
```

`preferred_candidate_id` 必须属于输入且已通过硬门控；`analysis_outcome` 只是解释标签，不是订单动作。
AI 结论与规则指标并列显示，不能覆盖规则排除原因，也不能改变候选资格或周期状态。

## 10. 权限与隐私

- 业务数据在家庭内共享，所有查询仍同时验证家庭和账户归属。
- `viewer` 仅 GET；`member` 可保存不放宽硬约束的偏好、人工选择和本人有权账户的交易关联；硬策略、
  provider、调度、规则暂停恢复和运行操作仅管理员。人工暂停可由创建者或管理员解除。
- 默认发送给外部 AI 的输入不含姓名、账户名、账户 ID、现金、总资产和完整持仓。
- URL、日志、错误和原始响应不保存密钥、Cookie、牛牛号或券商身份信息。
- GET 页面不调用 Futu 或 AI，不写数据库；刷新和分析使用明确 POST 或管理命令。

## 11. 调度、锁与幂等

- M0 只提供人工只读探测，不创建业务记录。
- M1/M2 采用 Django 管理命令与 DSM Task Scheduler，不引入 Celery/Redis。
- 运行按家庭、账户/标的和市场时间桶加数据库锁或 PostgreSQL advisory lock。
- 决策使用输入指纹、策略版本和 `as_of` 唯一键。
- AI 重跑新增版本；行情重新获取新增快照；不得最后写入者静默覆盖历史。
- 部分行情或 AI 失败保留成功标的，但运行状态为 partial；担保或关键事件数据失败则不生成可执行候选。

## 12. 测试策略

### 单元测试

- 全额现金担保、账户净值上限和跨账户隔离；
- 股票覆盖、重复占用、部分成交/关闭/指派；
- 权利金、Roll、累计成本、区间与年化收益的 Decimal 计算；
- 财报门控、暂停优先级、报价新鲜度和流动性门控；
- Pareto/分层候选的确定性；
- AI 候选/证据引用和未知 ID 拒绝。

### 集成测试

- Futu 静态链、订阅去重、额度不足、断线、退订和字段缺失；
- 多账户、重叠周期和真实交易关联；
- 并发资源预留、锁超时、重跑幂等；
- viewer/member/admin 权限和家庭隔离；
- 调整合约、提前指派、部分指派和数据未知 fail closed。
- 致富证券/盈透容量快照来源、身份映射、时效、待结算资金和融资状态；
- MSFT Roll 的旧腿平仓、新腿开仓、部分成交、交易关联及两种天数口径固定 fixture；
- 美东市场日期、节假日和交易所真实到期日，不按自然周五推导到期日。

### 回归

至少运行：

```text
docker compose exec -T web python manage.py test ipo portfolio option_wheel --keepdb
docker compose exec -T web python manage.py check
docker compose exec -T web python manage.py makemigrations --check --dry-run
git diff --check
```

M0 尚未创建 `option_wheel` app 时，先运行 `ipo portfolio` 及新增命令测试。

## 13. 分阶段实施

### M0：只读能力探测

- 实现无数据库写入的 Futu 期权能力探测命令；
- 验证 15 个首批标的、实际权限、订阅额度和关键字段；
- 使用 `m1-gate` 配置验证动态报价、历史、财报/除息能力、字段来源和订阅清理；
- 形成用户可核对的能力矩阵；
- 不访问 NAS，直至代码和本地测试稳定且主代理确认按照 NAS 技能执行。

### M1：确定性 MVP

- 建立 `option_wheel` app、政策、暂停、周期、腿、资源占用和不可变市场快照；
- 建立读取本地投资组合中致富证券和盈透账户的容量快照适配器；缺失时只展示数据不可用；
- 先试点 TSLA、MSFT 与一个用户选择的第三标的；
- 完成周度/其他 DTE 候选、技术分析、概率、基础市场状态、财报/除息硬门控和持仓管理；
- 不接外部 AI。

#### 2026-08-30 M1 基础实现状态

已在本地完成、尚未部署：

- 六个基础模型：`WheelPolicy`、`WheelBrokerAccountSnapshot`、`WheelMarketSnapshot`、
  `WheelOptionQuoteSnapshot`、`WheelDecision`、`WheelCandidate`；
- 初始迁移、仅超级管理员可见的只读证据后台、ORM 追加写证据保护，以及纯 Decimal 的 sell-put
  规则引擎；
- 规则严格 fail closed：账户要求 COMPLETE、USD、未过期、明确 `uses_margin=False` 且借贷余额为
  0；现金担保按 strike × 100 × 张数计算，使用 settled − reserved；单标的潜在指派金额包含已有
  暴露且不超过 NAV 比例；报价要求 COMPLETE、REAL_TIME、FRESH 且未过期；事件要求 CLEAR，
  技术状态要求 COMPLETE；
- execution gate 默认关闭，且 `OPTION_WHEEL_EXECUTION_ENABLED` 在本阶段硬编码为 `False`；只有后续
  代码评审明确移除总闸门后才可能持久化可执行决策。DTE 4–9 和每张 200–400 美元均为非阻断偏好；
- 40 个 `option_wheel` 定向测试通过。初始迁移已在本地开发数据库核对为已应用，生产数据库未改；
  `/option-wheel/` 家庭只读页面和导航已完成，GET 不抓行情、不写数据库、不提供交易操作。

尚未完成且当前不得产生可执行候选：美国期权正常交易时段 realtime/freshness 验证、事件 API
“无匹配”的语义确认、致富公户与 IBKR 的本地投资组合容量预演、规则结果落库服务、周期/腿/
占用、covered call、roll、AI、调度、生产迁移和部署。

### M2：公开事件证据与 AI 解释

- 在 M1 的确定性财报/除息硬门控上接入新闻、宏观日程、公开证据和语义事件特征；
- 建立本模块独立 AI 数据/费用授权；
- AI 只解释冻结候选和证据。

### M3：调度、复盘与扩展

- DSM 定时刷新和运行状态；
- 概率校准、周期收益和人工选择复盘；
- 扩展完整首批标的；
- 自动下单仍不在范围内。
