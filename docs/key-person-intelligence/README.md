# 关键人物动态模块：设计文档索引

状态：M1–M4.1 已部署并完成首轮生产自动循环；DSM 每小时任务待创建
开发分支：`codex/integrate-key-person-intelligence`
更新时间：2026-08-22

本目录保存“关键人物动态”模块的需求与技术设计。专业的软件工程交付物可概括为：

- **产品需求文档（PRD / SRS）**：说明为什么做、为谁做、做什么以及如何验收。
- **高层设计与技术设计（HLD / TDD）**：说明模块边界、数据模型、处理流程和运行方式。
- **信息源治理规范（Source Governance）**：说明来源等级、版权边界、证据和可信度规则。
- **实施路线图（Implementation Roadmap）**：说明分阶段交付物、质量门槛和待确认事项。

## 文档

1. [产品需求规格](./product-requirements.md)
2. [技术设计方案](./technical-design.md)
3. [信息源与 AI 治理](./source-and-ai-governance.md)
4. [实施路线图](./implementation-roadmap.md)
5. [参考文章观点映射](./article-reference-mapping.md)
6. [轻量情报流水线与评分策略](./lightweight-pipeline.md)
7. [M2 自动采集设计与运维](./m2-collection-design.md)
8. [M3.1 事件结构化分析设计与验收](./m3-event-analysis-design.md)
9. [M3.2 真实文本分析边界与样本验收](./m3-2-real-analysis-validation.md)
10. [M3.3 跨来源事件聚合与人工复核](./m3-3-event-aggregation.md)
11. [M4 最小可用 AI 情报闭环与每日简报](./m4-minimum-ai-digest.md)
12. [M4.1 自动情报循环与公开证据摘录](./m4-1-automatic-intelligence.md)

## 当前设计结论

模块在代码中命名为 `intelligence`，第一期以“关键人物动态”为主。底层使用通用的
“关注主题—信源—来源条目—情报事件—证据”结构，为以后增加个股行业新闻和 AI 前沿频道保留扩展点，
但第一期不顺带开发这些频道。

首版继续使用 Django、PostgreSQL、Django 管理命令和群晖 DSM Task Scheduler，不引入
Celery、Redis、独立前端或常驻爬虫服务。外部平台必须通过允许的公开接口、RSS 或人工录入接入；
不以绕过登录、反爬或付费墙的方式采集内容。

## M1 实现结果

当前分支已实现不依赖任何外部 API 的人工闭环：

- `intelligence` Django app、首个迁移、Django Admin 和家庭权限隔离；
- 关注对象、对象关系、来源、来源条目、事件、证据、关注状态、成员已读/收藏和运行审计；
- 人工录入、事件编辑/忽略、对象关注、事件已读/收藏；
- 今日首页、全部动态、对象列表、人物时间线、事件证据详情和管理员运行状态；
- 默认 8 位人物及关联机构的幂等初始化命令；
- 针对家庭边界、viewer 只读、幂等录入、证据链和 GET 无隐式写入的测试。

初始化命令先试运行，再由管理员选择是否为指定家庭全部关注：

```bash
python manage.py seed_key_people --dry-run
python manage.py seed_key_people --follow-all --family-id <家庭ID>
```

M2 在不增加 Python 依赖的前提下接入 RSS / Atom 和 YouTube 官方频道元数据。M4.1 可对管理员逐个
启用的 RSS 信源提取少量公开证据段落并自动调用既有文本模型；不保存完整正文、不绕过登录或付费墙。
YouTube 仍不下载视频、音频或字幕，SEC 与 X 仍未启用。

用户验收后确认采用五段式轻量流水线，并将“关注主题”和“信源”拆开：人物只是关注主题的一种，
信源可以同时服务于人物、机构、行业、技术、政策和证券主题。详见
[`lightweight-pipeline.md`](./lightweight-pipeline.md)。

## 参考文章状态

用户已提供微信文章 PDF，全文与 14 页版面均已检查，并完成逐条观点映射。文章原文不复制到仓库，
仓库只保留对本项目可执行的设计结论。
