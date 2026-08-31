# M0：Futu 期权能力探测与验收矩阵

状态：Draft 0.1
更新时间：2026-08-29

## 1. 目的

在创建 `option_wheel` 业务表或修改生产数据前，确认当前运行环境、Futu SDK、OpenD 和行情权限能否
支持期权车轮模块。静态探测是纯读取；动态 QUOTE 订阅会短暂改变 OpenD 外部订阅状态，必须由参数
显式触发、只管理本运行拥有的订阅并在结束时清理。命令不得访问交易接口、提交订单、写数据库或输出
任何账户凭证。Futu 登录身份不作为致富证券或盈透证券的账户余额/持仓事实源。

## 2. 首批标的

```text
US.TSLA US.MSFT US.AAPL US.AMZN US.GOOG US.GOOGL US.META US.NVDA
US.TSM US.ASML US.AMD US.INTC US.MU US.SKHY US.SPCX
```

## 3. 建议命令

```text
python manage.py probe_futu_option_capabilities \
  --symbols US.TSLA US.MSFT US.AAPL \
  --max-expirations 2 \
  --max-contracts-per-expiration 2
```

默认执行静态能力探测。只有显式增加 `--subscribe-quotes` 才订阅少量候选合约并验证动态字段：

```text
python manage.py probe_futu_option_capabilities \
  --symbols US.TSLA US.MSFT \
  --max-expirations 1 \
  --max-contracts-per-expiration 1 \
  --subscribe-quotes \
  --include-option-analytics \
  --include-history \
  --include-earnings
```

进入 M1 前使用强门控配置；该配置等价于动态订阅、期权分析、历史、财报/除息探测和严格退出：

```text
python manage.py probe_futu_option_capabilities \
  --symbols US.TSLA US.MSFT US.SKHY US.SPCX \
  --profile m1-gate
```

建议参数：

| 参数 | 说明 |
| --- | --- |
| `--symbols` | 必填、显式的 Futu 正股代码；不静默扫描全部市场 |
| 全局上限 | 最多 20 个标的；动态模式最坏情况候选数最多 12，超限时在连接 OpenD 前拒绝 |
| `--max-expirations` | 每个标的最多探测的近期到期日，默认 1，上限 3 |
| `--max-contracts-per-expiration` | 每个到期日最多选择的代表合约，默认 1，上限 3 |
| `--profile` | `static` 或 `m1-gate`；默认 `static`，后者强制 M1 所需全部能力和严格退出 |
| `--subscribe-quotes` | 显式启用动态 QUOTE 订阅测试 |
| `--include-option-analytics` | 显式探测代表期权概率/波动率接口和原始字段 |
| `--include-history` | 显式调用正股历史 K 线 |
| `--include-earnings` | 显式验证未来财报、除息或等价公司日历能力 |
| `--format` | `table` 或 `json`；默认用户可读表格 |
| `--allow-partial` | 仅用于调查性探测；允许 `partial` 返回 0，默认 `partial/failed` 均为非零 |

命令不直接写输出文件。需要保存时由调用方把 stdout 重定向到不提交的 `outputs/`。

## 4. 探测步骤

### 4.1 环境级

- Futu SDK 版本；
- OpenD 主机/端口可连接性；
- `OpenQuoteContext` 建立与关闭；
- `query_subscription` 总额度、已用、剩余和本连接占用；
- 所需方法是否存在，并按实际安装版本记录方法签名、返回字段和能力状态；
- 任何返回错误的脱敏摘要。

不得输出牛牛号、登录状态细节、密码摘要、密钥、完整配置或其他账户身份信息。

### 4.2 标的级

| 能力 | 必需字段/结论 |
| --- | --- |
| 正股快照 | 代码、最新价、报价时间、是否延迟、市场状态 |
| 到期日 | 以美东自然日计算 DTE；解析失败或已经过期的日期只记录拒绝原因，不请求其期权链或订阅；每个标的最多扫描 50 个排序唯一日期 |
| 静态期权链 | 合约数、Put/Call 数、行权价范围、标准/调整状态可否识别 |
| 周度到期 | 以美东市场日期和接口返回为准，是否存在近期 4–9 DTE 到期；不得按自然周五构造 |
| 动态订阅 | 代表合约订阅成功/失败、错误、额度变化 |
| 动态报价 | Bid、Ask、盘口量、Last、成交量、OI、IV、Delta/Gamma/Theta/Vega/Rho；逐字段记录来源、原始字段、单位、`as_of` 和状态 |
| 合约身份 | provider code、到期日、行权价、类型、乘数、交割股数、行权方式 |
| 概率 | `strike_probability`/`itm_probability` 是否可用、时点和单位 |
| 波动率 | 当前/历史 IV、HV、波动率溢价字段是否可用 |
| 历史 K 线 | 正股复权日 K 是否可取、样本数和最后日期 |
| 财报/除息 | 未来日期、发布时间类型、除息日和数据状态；不得把缺失或接口不支持解释为没有事件 |

### 4.3 代表合约选择

- 默认只选最近允许到期日的 Put；
- 正股最新价可用时，选择最接近但不高于正股价格的一个 OTM/ATM Put；
- 正股价格不可用时，明确记录降级原因，并按 Futu 原始链顺序使用最早允许到期组中的第一个有效标准 Put；
- 不订阅全部期权链；同一到期日订阅数受参数硬限制；
- 调整合约不得作为代表合约，除非命令只能识别为 unknown，此时报告 partial。

## 5. 结果结构

顶层：

```json
{
  "status": "success|partial|failed",
  "sdk_version": "...",
  "fetched_at": "UTC ISO-8601",
  "subscription": {
    "total_used_before": 0,
    "remain_before": 0,
    "total_used_after": 0,
    "remain_after": 0,
    "owned_codes": [],
    "cleanup_status": "not_requested|restored|partial|failed"
  },
  "capabilities": {},
  "symbols": [],
  "errors": []
}
```

每个标的至少包含：

```json
{
  "symbol": "US.TSLA",
  "status": "success|partial|failed",
  "underlying_quote": {},
  "expirations": [],
  "chain_summary": {},
  "representative_contracts": [],
  "history": {},
  "earnings": {},
  "errors": []
}
```

JSON 数字必须说明 Futu 原始单位；不能在探测阶段擅自把百分比和小数混用。
每个关键动态字段都应使用 `{value, raw_field, unit, source_method, as_of, status}` 或等价结构，不能把
不同接口、不同时间戳的字段拼成一个看似同一时点的“完整报价”。接口不存在或字段缺失必须明确保存
`unsupported/unknown`，不得使用 Delta、Last 或静态链字段伪造替代值。

Futu 明确说明的 IV、行权概率和波动率分析值记录为百分号前数值（`percent_points`）；未明确的
Greek、盘口量单位和价格币种分别记录为 `unknown_greek_unit`、`provider_volume_unit_unknown` 和
`provider_price_unknown_currency`，不得自行换算。

## 6. 状态与退出码

- `success`：全部请求的必需能力成功，且动态模式下代表合约具有可用、未延迟的关键字段。
- `partial`：标的存在但权限、字段、历史、概率或财报能力部分不可用；继续其他标的。
- `failed`：OpenD 无法连接、全部标的失败或命令配置非法。
- 默认 `partial/failed` 返回非零；只有调查性运行显式 `--allow-partial` 时，`partial` 可以返回 0。
- `--profile m1-gate` 强制动态报价、期权分析、历史、财报/除息能力和严格退出，不接受
  `--allow-partial`。

动态模式使用跨进程锁，避免多个探针同时改变订阅。任何异常都必须先按 SDK 已验证的退订语义清理
本次拥有的代码并核对状态，再关闭 `OpenQuoteContext`；不得退订运行前已存在或其他任务拥有的代码。
Futu 要求新订阅至少保持一分钟；探针从每个本次拥有代码的订阅 API 返回时点起至少等待 61 秒，
才执行精确退订。等待期间不接收推送，并以短时间片等待；即使收到中断，也先完成退订、状态核对、
连接关闭和锁释放，再把中断交还调用方。测试不得真实联网。

## 7. 测试要求

至少覆盖：

- 参数必填、上限和代码格式校验；
- SDK 缺失和 OpenD 连接失败；
- `query_subscription` 成功/失败；
- 一个标的失败不阻塞其他标的；
- 无到期日、无 Put、只有调整/未知合约；
- 代表合约确定性选择；
- 不带 `--subscribe-quotes` 时不调用 subscribe；
- 订阅成功、权限不足、额度不足、关键字段缺失、字段时间戳不一致和延迟行情；
- 概率、波动率、历史 K 线和财报可选调用；
- 能力方法不存在时报告 unsupported，不调用替代值伪造；
- 只退订本次新订阅代码，保留运行前已有订阅；清理失败可见，`finally` 关闭 context；
- 并发运行锁和锁占用失败；
- JSON 可序列化且不含凭证、主机登录信息或账户 ID；
- 默认、`--allow-partial` 与 `--profile m1-gate` 退出状态。

建议使用固定 DataFrame/字典 fixture 和 mock `futu` 模块，不依赖本地或 NAS OpenD。

## 8. M0 验收门槛

进入 M1 前必须确认：

1. 使用 `--profile m1-gate`，TSLA、MSFT 至少能取得近期 Put 链和动态 Bid/Ask、OI、IV、Greeks，
   且逐字段来源、单位和 `as_of` 已确认。
2. 至少一个代表合约能取得 Futu 概率字段，或明确记录账户/版本不支持并设计可验证降级路径。
3. 能读取订阅额度；运行只管理自己新订阅的代码，退出后清理并核对，不会无界订阅、误退订或泄漏连接。
4. SKHY、SPCX 能否在当前 Futu 账户实际取链有明确结论。
5. 历史 K 线和财报/除息能力至少在 TSLA、MSFT 验证。任一关键事件能力不可用时，M1 可以实现
   “数据不可用”页面，但不得生成可执行的新卖出候选，直至有经批准的替代数据源。
6. 报告能区分权限不足、数据延迟、字段缺失、无合约和接口错误。
7. 命令和测试不写数据库，不访问交易接口，不产生订单。
8. 探针报告明确说明它不验证致富证券/盈透的已结算现金、NAV、融资状态和持仓；这些由独立的账户
   容量快照适配器在 M1 前验收。

若 M0 真实探测需要访问 NAS，必须先遵循项目 NAS 部署技能，仅执行只读命令并保存脱敏结果；不得顺带
部署、迁移或修改生产配置。

## 9. Windows OpenD 实测记录（2026-08-29）

- Futu SDK 10.09.6908 和 Windows 本机 OpenD 连接正常；TSLA、MSFT、SKHY、SPCX 均可读取正股
  快照和历史日 K，说明四个代码在当前 OpenD 中可识别。
- 开通美国期权行情权限后，四个标的均取得近期 Put 链和一张代表合约；Bid/Ask、盘口量、最新价、
  成交量、OI、IV、Delta、Gamma、Theta、Vega、Rho、行权概率与波动率分析均有可用值和来源时间。
- 四只代表合约的静态 `lot_size`、快照 `option_contract_size` 和动态 `contract_size` 三方一致为
  100，`option_standard_type` 为 `STANDARD`，快照 `option_area_type` 为 `AMERICAN`。因此本次样本的
  100 股交割单位和美式行权方式已由提供方字段交叉验证；`option_settlement_mode` 仍为 `N/A`，不能
  据此推断结算交割方式。
- 财报日历以首尾共七个自然日查询；派息日历按单日分页读取并在标的间共享缓存。当前未来七天未匹配
  这四个标的时，结果保持 `unknown`，不解释为确定没有事件。
- 首次动态实测发现 Futu 拒绝取消订阅不足一分钟的代码；探针按官方生命周期要求修正为每个代码
  至少保持 61 秒。修正后的真实运行准确清理四张代表合约，前后全部六项订阅额度一致，期权已用额度
  均为 0、剩余均为 60，本连接前后 QUOTE 代码均为空，状态核对结果为 `restored`。
- 本次实测发生在周末，Futu 返回最近收盘数据但未提供可判定的延迟标志，因此快照和动态报价的延迟、
  新鲜度仍为 `unknown`，`m1-gate` 按设计返回非零状态。需要在美国期权正常交易时段复测，并核实
  结算方式和事件日历无匹配语义后，才能判定 M0 强门禁通过。
