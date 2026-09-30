# 接口契约 v0.3 · 已实现

正式页面使用 Django 模板；同一路由通过 `?format=json` 或 `Accept: application/json` 返回 JSON。纯表单操作在 [OpenAPI](openapi.json) 中标明，成功跳转 302。业务校验复用服务层。

## 权限与副作用

- 前缀 `/research/watch/`，Django 会话认证，写操作要求 CSRF 与有效写入成员。
- 公共新闻按家庭范围；档案、候选、复核和备注仅本人可见，管理员无私人档案旁路。
- 不接受客户端 owner/family 字段；JSON 未知字段返回 400，越权对象 404，停用或无权限 403。
- GET 不联网、不写入、不调用模型。
- 关注和备注提交 expected_version；冲突 409。关联和检查提交 expected_revision。
- 关联、复核和排队使用 Idempotency-Key；HTML 表单使用隐藏字段。同键同体返回原结果，同键不同体 409。
- 新闻与动态列表使用签名 cursor，绑定成员、路径和筛选，24 小时失效。活跃新闻池不是冻结分页快照，刷新和新采集可能改变页面位置。
- 202 仅表示排队。没有授权或额度时保留候选和明确原因，不生成分析。

## 路由与页面

| 方法 / 后缀 | 内容 |
|---|---|
| GET news/ | 新闻池；topic、market、category、source、date_from、date_to、q、cursor |
| GET topics/ | 三组主题及可见事件数；合并事件计一次 |
| GET news/{id}/ | 当前材料；可用 version 指定历史版本 |
| POST news/{id}/associate/ | dossier_id、material_version、expected_revision，创建或返回本人候选 |
| GET items/ | 本人候选；dossier、assumption、direction、saved、cursor |
| GET items/{id}/ | 材料版本、逐假设证据、引文及追加复核历史 |
| GET / POST rules/ | 查看、创建或更新本人关注；更新同一路由，不另设 PATCH |
| POST events/{id}/annotation/ | expected_version、saved、read、note；按稳定事件保存个人记录 |
| POST evidence/{id}/review/ | direction、reason；追加复核，原结论保留 |
| POST items/{id}/select/ | 表单 selected；仅选择下一轮综合分析材料 |
| POST consent/{id}/ | 表单 provider、allow_personal_thesis；本人明确授予/撤回授权 |
| POST runs/ | dossier_id、expected_revision；幂等加入后台检查 |
| GET research-context/ | dossier_id；已有研究、最近分析入口与之后新增候选数 |
| GET coverage/ | 来源状态、共享模型额度、本人任务与已知覆盖缺口 |
| POST coverage/ | 管理员 source_id、enabled；启停来源，不启动网络 |
| POST events/{id}/organize/ | 管理员表单 target、action、reason、expected_updated；合并/拆分/后续 |
| GET saved/ | 本人收藏，HTML 页面 |

## 数据语义

- 新闻 id 是材料稳定 ID；material_version 是不可变版本 ID，version_number 是同材料内序号。
- published_precision 区分 time / day / unknown；published_at 不存在时保持 null，不用 found_at 冒充。
- assumption_key 为 pillar:0 / question:0，必须与 revision_id 组合理解。
- direction 为 support / weaken / mixed / unknown。纯规则或手动候选不自动生成方向性证据。
- 引文必须是当次材料文字的精确子串，并保存 locator；不足时只能为 unknown。人工复核是追加记录，筛选采用最新有效复核。
- 候选唯一范围为档案、材料版本、判断版本。判断修订后可重新关联同一新闻，旧证据仍是旧版本历史。
- 状态为 active / corrected / withdrawn。上游版本变更或判断改版使候选 stale，退出有效支持集合；历史详情、收藏与备注保留。
- 事件人工合并只影响分组，不把多个转载当成多份独立印证。不移动或删除原版本，合并可以撤销。
- 正式判断和综合分析仍走 investment_research。含新闻的新分析冻结选定版本；不改写任何已有分析。

同库运行，不建设双库同步协议。首版有限来源窗口、无法自动辨识所有上游撤回等边界见 [运行手册](07-operations.md)。
