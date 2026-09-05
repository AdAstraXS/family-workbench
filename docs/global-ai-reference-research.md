# 全局 AI：开源参考项目调研与选型建议

日期：2026-09-05。状态：第一轮调研完成；下述架构与原型范围为建议，尚未进入实现或生产部署。

后续进展：本地隔离原型已完成一轮验证，见 [原型结果与限制](global-ai-spike-results.md)。
该结果更新下文的“未执行”计划状态；仍未生产部署。

## 结论

建议保留 Django 作为身份、财务事实、知识权限与长期记忆的所有者，首版以普通模型 API 为基准，
Codex App Server 仅作为可替换的候选引擎继续验证。借鉴 LibreChat 的对话与结构化记忆、Open WebUI 的记忆管理与索引维护、
Dify 的检索测试设计；暂不整套部署这些平台，也暂不引入 Mem0 为首版必需依赖。

这不是对各项目整体优劣或安全性的排名。目标是服务两名家庭成员、复用已有 NAS 与 Django，
避免为了一个 AI 入口重复建设身份、资产库和知识库。

关键新发现：

- 固定版本 Codex App Server README 将 WebSocket 标为 experimental / unsupported，并明确
  不建议生产依赖。原型优先验证服务端进程通过 stdio 连接；浏览器由 Django 的鉴权入口接入。
- Mem0 的查询范围参数不等于网站身份鉴权。源码中按 memory_id 的 get/update/delete 不接受
  当前登录者参数，宿主必须先验证归属；这不是已确认的 Mem0 产品漏洞。
- 对话模型切换与向量模型切换是两件事。前者不必重建索引；后者即使维度相同也可能不兼容，
  需要独立索引版本和重建机制。
- 当前项目已经有账本资产余额快照，不能把银行资产需求误判为完全缺少数据模型。

## 已确认需求与待定口径

- 你与妻子都能使用。财务共享，默认按本人范围分析，可选全家。
- 知识资料沿用私人／家庭共享权限。私人对话和个人记忆不能因选择全家而对另一人开放。
- 云端为主，允许相关财务明细发送；保留本地模型选项。该偏好不授权在调研时实际发送生产数据。
- 首个场景拆为模块内问题：账本回答全部账户资产，投资模块回答具体投资账户持仓与大类配置；
  默认不比较、对账或合并两组金额，极少量跨模块问题另行显式处理。
- 用户提出长期复合年化 12%，不排除新增投入，以及不超过 30% 的回撤承受要求。
  年化目标是否剔除投入尚待确认；不能擅自解释为含投入的资产规模增长，也不修改现有投资目标公式。
- 剔除出入金后的回撤口径是此前助手建议，尚未作为用户明确确认的计算定义。
- 大额用钱计划、未来投入金额／时间、房产和负债覆盖、家庭共同目标仍待明确。

约束依据：[产品原则](family-knowledge-product-principles.md)、
[知识架构](family-knowledge-base-architecture.md)、[系统架构](architecture-baseline.md)。

## 调研方法与证据强度

读取官方文档、GitHub 官方仓库元数据，以及固定 commit 的少量源码、许可证和部署清单。
仓库 main 版本不是推荐部署版本；以下哈希仅保证调研可复现。五个仓库查询时均未归档，
对应提交时间为 2026-09-04 或 09-05，说明近期仍有提交，不构成维护质量或稳定性的保证。

未运行第三方服务、未调用付费模型、未访问 NAS 或生产数据库、未执行第三方代码。
源码抽样不等于完整安全审计；没有把文档宣称的能力当作已通过端到端验收。
公开源码副本在本地 outputs/global-ai-research/，不纳入提交；长期证据使用下面的固定链接。

| 项目 | 固定 commit | 本轮深度 | 建议 |
| --- | --- | --- | --- |
| Codex | ddf04ad26789d040f9ef6a96736f76602e35a6cc | 平台文档、仓库协议 README、许可证；未审运行时实现 | 原型候选引擎 |
| LibreChat | 000349681e4d80b7c7d2fc4a1f6b68f775bc943d | 记忆路由、分区授权、Compose、许可证及产品文档 | 重点借鉴交互与记忆设计 |
| Open WebUI | 0a7c15832fb30b1903753e83f81dc7d27e5b0944 | 记忆与知识路由、许可证、记忆文档 | 借鉴管理与索引维护 |
| Mem0 | dae67f74f5cc7bf138c7d7d6f9cec5ce4b4373b3 | Memory 同步方法、范围辅助函数、许可证 | 后续语义记忆候选 |
| Dify | 00e578606715a9da34488608edee8c68d4ef4893 | 检索服务、检索测试服务、Compose、许可证 | 借鉴检索调试与评测 |

## 1. Codex：运行引擎与业务嵌入

官方平台文章把应用业务上下文、工具和操作边界交给宿主，App Server 提供持续任务与事件交互。
Relay 是文章中的虚构物流数据示例：在原业务页面旁发起分析，通过应用工具查询，重要写入需确认。
它证明一种集成模式，不证明我们的财务场景已经可靠。

对家庭工作台的设计建议：

- 全局对话和模块内“问 AI”共用入口能力；页面可提供当前成员范围、快照日期等上下文。
- 提供少量按模块隔离的受控工具：账本资产快照、投资账户持仓、现金流汇总、知识检索、精确原文读取。
- 金额与比例由现有服务计算，AI 只解释、比较和请求更多证据。
- 不把生产数据库凭据、宿主文件系统或部署能力交给业务 Agent。
- 每名成员隔离运行目录、会话映射与凭据；所有恢复、读取、取消请求都由 Django 核对会话归属。
  这属于拟议隔离方案，尚未验证进程数量与 NAS 开销。
- 初期使用已有 MCP 扩展路径；动态工具接口在本轮文档中仍有实验标记，不作为必要依赖。

部署修正：先采用内部适配进程通过 stdio 启动／管理 Codex，Django 负责外部鉴权与事件展示。
是否同容器、是否需要独立容器及进程通信，应由原型结果决定，不先建立新的任务集群。

许可证为 Apache-2.0；模型服务与订阅另行提供。ChatGPT 登录与 API key 是不同接入方式，
Plus 的 Codex 权益不能等同于夫妻共用的通用 API 服务。多人使用与 NAS 无人值守登录适用性未确认。

证据：[平台文章](https://developers.openai.com/blog/codex-as-a-platform)、
[固定协议文档](https://github.com/openai/codex/blob/ddf04ad26789d040f9ef6a96736f76602e35a6cc/codex-rs/app-server/README.md)、
[许可证](https://github.com/openai/codex/blob/ddf04ad26789d040f9ef6a96736f76602e35a6cc/LICENSE)。

## 2. LibreChat：首版记忆最值得借鉴的对象

官方 User Memory 将长期记忆描述为按用户保存的键值记录，与单次对话历史分开，可人工管理，
自动抽取可选。因此首版不必先把全部历史聊天向量化。

源码核对：memories.js 使用 JWT 中间件，列表读取使用 req.user.id；authorization.ts 对 Agent
记忆分区验证资源是否存在及使用／查看权限。其 Agent 分区不等于我们的夫妻共享空间。
未沿全部模型层和缓存路径审计，不能声称已证明整个项目不存在越权。

可借鉴：记忆管理面板、启停控制、容量预算，以及把记忆来源和适用范围显式呈现。
我们应增加候选／已确认状态、来源消息、生效时间和版本；资产余额仍实时查询，不能保存成静态记忆。

不建议整套嵌入：所读 Compose 包含 MongoDB、Meilisearch、RAG API 与向量库等服务，
会重复引入应用与存储体系。MIT 允许在满足声明保留条件下复用代码，但 React/Node 代码迁入 Django
的成本仍需判断，本轮没有复制业务实现。

证据：[记忆文档](https://www.librechat.ai/docs/features/memory)、
[路由](https://github.com/danny-avila/LibreChat/blob/000349681e4d80b7c7d2fc4a1f6b68f775bc943d/api/server/routes/memories.js)、
[分区授权](https://github.com/danny-avila/LibreChat/blob/000349681e4d80b7c7d2fc4a1f6b68f775bc943d/packages/api/src/memory/authorization.ts)、
[Compose](https://github.com/danny-avila/LibreChat/blob/000349681e4d80b7c7d2fc4a1f6b68f775bc943d/docker-compose.yml)、
[许可证](https://github.com/danny-avila/LibreChat/blob/000349681e4d80b7c7d2fc4a1f6b68f775bc943d/LICENSE)。

## 3. Open WebUI：记忆索引与可管理性

记忆路由使用已验证用户，将向量写入 user-memory-{user.id}；更新流程携带来源、chat_id、message_id
及模型元数据，并同步更新／删除向量。代码包含维度不匹配后的按用户重建逻辑。
知识路由中也有用户／群组范围处理；这里只确认所读路径存在控制，未核对完整 RAG 召回链路。

可借鉴：记忆有独立管理界面；权威记录与派生向量分开；索引可重建。
我们需进一步处理同维度不同嵌入模型的版本切换，以及数据库写入后嵌入失败的补偿与重试。
自动记忆提取、嵌入和重排也可能调用外部模型，应一并纳入外发范围与费用统计。

许可证包含品牌保留附加条件，但固定 LICENSE 存在滚动 30 天不超过 50 名最终用户等例外。
两人家庭不能简单判定为必须商用授权；也不能笼统宣称它就是标准 MIT。
实际复用具体文件时，应同时核对其历史许可证与保留声明要求。

证据：[记忆源码](https://github.com/open-webui/open-webui/blob/0a7c15832fb30b1903753e83f81dc7d27e5b0944/backend/open_webui/routers/memories.py)、
[知识路由](https://github.com/open-webui/open-webui/blob/0a7c15832fb30b1903753e83f81dc7d27e5b0944/backend/open_webui/routers/knowledge.py)、
[许可证](https://github.com/open-webui/open-webui/blob/0a7c15832fb30b1903753e83f81dc7d27e5b0944/LICENSE)。

## 4. Mem0：后续语义记忆，而非首版身份或事实库

固定 main.py 的 _build_filters_and_metadata 构造 user_id/agent_id/run_id 等范围，
Memory.get(memory_id)、update(memory_id, ...) 和 delete(memory_id) 则按 ID 操作。
因此接入时必须由宿主验证当前用户对该 ID 的权限，不能把这些方法原样暴露给模型或浏览器。
这是库接口与应用鉴权的职责差异，不是对托管 Mem0 服务的漏洞结论。

可借鉴：记忆与模型调用分离，以及范围元数据。首版两名成员、少量明确目标与偏好，
用 Django/PostgreSQL 保存可确认记录更容易维护。只有历史经验检索需求明显增长、现有方法效果不足，
才对 Mem0 做对照评测；不预先增加图数据库或独立记忆服务。

许可证 Apache-2.0。若后续复用，需核对嵌入、模型、向量后端、遥测与删除传播的配置。

证据：[固定 Memory 实现](https://github.com/mem0ai/mem0/blob/dae67f74f5cc7bf138c7d7d6f9cec5ce4b4373b3/mem0/memory/main.py)、
[许可证](https://github.com/mem0ai/mem0/blob/dae67f74f5cc7bf138c7d7d6f9cec5ce4b4373b3/LICENSE)。

## 5. Dify：借鉴检索测试，不引入整套工作流平台

检索服务显式接受 top_k、score_threshold、reranking_model、document_ids_filter；
检索测试服务支持元数据过滤，存在“过滤条件成立但没有文档时返回空结果”的分支。
这启发我们严格区分“没有限制”和“有权限限制但无可读资料”，不能把空集合解释为搜索全部。

建议做一个小型检索评测入口：问题、实际命中段落、精确版本、相关性、无答案结果。
检索分数不是答案可信度；有引用也不证明引用支持结论。权限过滤必须先于内容发给模型。
这里只核对检索接口与测试路径，未把元数据过滤认定为完整成员权限。

所读 Compose 包含 API、worker、worker_beat、Redis、sandbox、plugin_daemon 等，另有多种
可选数据库 profile；不能把所有可选服务当作必开服务。整体引入对当前两人项目仍增加明显维护面。

许可证为附加条件的 Apache 版本：多租户按 workspace 定义，前端 LOGO 有限制，纯后端有对应例外。
两名用户不自动等于两个租户。优先借鉴设计，复制源码前按具体用途核对条件。

证据：[检索服务](https://github.com/langgenius/dify/blob/00e578606715a9da34488608edee8c68d4ef4893/api/core/rag/datasource/retrieval_service.py)、
[检索测试](https://github.com/langgenius/dify/blob/00e578606715a9da34488608edee8c68d4ef4893/api/services/hit_testing_service.py)、
[Compose](https://github.com/langgenius/dify/blob/00e578606715a9da34488608edee8c68d4ef4893/docker/docker-compose.yaml)、
[许可证](https://github.com/langgenius/dify/blob/00e578606715a9da34488608edee8c68d4ef4893/LICENSE)。

## 项目落地建议

已核对本仓库：

- app/ai_analysis/models.py：已有服务商、分析请求与结果、Token 和费用字段，尚不能当作完整多轮对话模型。
- app/ledger/models.py：AssetBalanceSnapshot/AssetBalanceEntry 已保存日期、成员、账户、币种与金额。
- app/knowledge/search.py：搜索投影保留 owner、visibility、正文等，文档索引使用 current_revision。
  仍需在业务读取服务中复核权限，未来引用须冻结具体 revision，不能只引用会变化的当前正文。

拟议分工：Django 管理身份、权威对话、确认记忆和证据；Codex 仅保存其必要的运行状态，
使用映射关联业务对话。普通 API 后端未来也复用同一组数据工具，不要求经 Codex 转发所有模型。

特别关注两种边界混淆：

1. 账本总资产和投资持仓属于两个模块级问题。默认根据问题只调用一个模块；不建立自动去重、
   对账或合并关系。用户明确要求跨模块时再走独立流程，首版不实现通用合并器。
2. “全家”是财务分析范围，不是私人资料解锁开关。本人有权引用私人资料时生成的回答仍保持私密；
   分享需检查证据权限。撤销资料权限后，历史答案与缓存也要有明确处置规则。

## 最小原型与退出条件（已完成离线边界验证，模型对照未完成）

只做一个原型，避免同时部署多个完整平台：

1. 本地隔离环境准备两名虚构成员、少量资产和三篇有版本的资料，不使用生产数据。
2. 后端通过 stdio 连接固定发布版本 Codex，提供受控工具；具体版本需另选，不能直接部署本轮 main。
3. 用同一批数据对比 Codex 多步分析与普通模型 API 的简单流程，记录完成率、引用、延迟及用量。
4. 验证恢复、取消、超时及不重复提交；不得因重连自动产生新的付费请求。

| 检查 | 通过条件 |
| --- | --- |
| 身份 | 篡改 member_id 或 thread_id 不能访问另一人的私人资料、记忆或对话 |
| 全家范围 | 财务可跨成员汇总；私人知识和对话不随范围切换泄露 |
| 数字 | 账本和投资各自与所属模块程序一致；不跨模块相加；缺行情／汇率明确报告 |
| 引用 | 文档 ID、版本与段落可回查；无匹配时不伪造引用 |
| 记忆 | 提取先作候选，确认后生效；修改、删除及换模型后保持正确 |
| 数据外发 | 聊天、嵌入、重排、自动记忆路径均受相同授权范围约束 |
| 上下文 | 本地转云端检查历史与摘要，不只检查新问题 |
| 注入 | 原文中要求扩大权限、改账或外发内容的文字不能获得执行权 |
| 生命周期 | 取消与重连不重复计费；失败可定位且不回显密钥或私密原文 |

如果 Codex 的隔离、协议或兼容成本超过收益，就采用 Django 受控工具加普通模型 API；
不为保留 Harness 而改变财务、知识或成员权限结构。
本轮没有真实模型对照数据，不能提前断言 Harness 更便宜、更准确或已适合 NAS 生产运行。
