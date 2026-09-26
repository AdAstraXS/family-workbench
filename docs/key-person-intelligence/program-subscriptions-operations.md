# 精选订阅：运行与验收

更新：2026-09-26。本文随实现更新；发布记录另行列出 NAS 实际 commit 与恢复点。

## 范围

用户确认同时开发四个来源：视野环球财经、Dwarkesh Podcast、In Good Company、Howard Marks / Oaktree Memos。
SEC 和公司 IR 继续由投研模块负责。旧泛新闻采集器与已有资料不删除，暂停生产泛新闻源需在发布配置中记录。

入口 `/intelligence/programs/`；管理员配置 `/intelligence/programs/settings/`。
日常页面以文章、节目和阅读为主；配置、检查时间和失败信息在次级管理页。

| 来源 | 发现更新 | 内容获取 |
| --- | --- | --- |
| 视野环球财经 | YouTube 官方 Atom，固定频道 UCFQsi7WaF5X41tcuOryDk8w | 公开字幕优先；缺失时受限 yt-dlp 获取 M4A，再交给 Fun-ASR |
| Dwarkesh | 官方 RSS https://www.dwarkesh.com/feed | RSS 完整文字稿或公开正文；不把节目简介当文字稿，不抓付费预览 |
| In Good Company | 官方 Acast RSS https://feeds.acast.com/public/shows/in-good-company-with-nicolai-tangen | 排除 Highlights、Friday Wrap-up、Trailer；公开音频交给 Fun-ASR |
| Oaktree Memos | 官方 Insights 页面内的公开 data-items 列表 | 仅 `/insights/memo/` 正文，排除播客副本和机构新闻 |

首次订阅收集最近三篇，仅最新一篇进入自动处理。后续出现的历史条目可以浏览标题、按需处理；不自动对历史批量转写。
取消订阅保留资料，停止抓取和后台处理。自动整理可按来源关闭，管理员仍可逐篇加入队列。

## 数据与安全

- 新模型按家庭隔离：ProgramSettings、ProgramSubscription、ProgramEntry、ProgramRevision、ProgramSummaryChunk。
- 文字稿按段落与时间戳计算 SHA-256，重复内容复用版本；人工补充形成新版本。
- 完整原文只在情报模块保存。成员点击“保存为知识”后，才建立知识待整理资料及不可变原始快照。
- AI 摘要随后完成时，再次保存会追加知识版本，保留旧版本；非所有者普通成员不能替他人更新。
- Key 使用既有 `KNOWLEDGE_TOKEN_ENCRYPTION_KEY` 加密；不回显、不开 Debug 记录、不把 Key 或远程错误体写入日志。
- 文本整理单独授权完整公开正文。它沿用已有文本提供商的密钥、输入限制、价格与单次费用上限；不会放大旧事件摘要的数据授权。
- 音频仅限指定公开频道，无登录 Cookie、无会员获取、无访问限制绕过。60 MB 上限，默认单集 180 分钟。
- YouTube 临时音频先加密写入媒体存储，仅通过绑定文件的六小时签名链接交给百炼；成功或到期后删除。播客直接使用出版方公开音频 URL。
- GET 阅读与配置页面不写库、不发网络请求。管理员才能修改来源和服务设置；viewer 不能保存、导入或重试。

## 转写和整理

北京模型固定 `fun-asr-2025-11-07`；可填写 Workspace ID 使用专属域名，也支持仍有效的 `dashscope.aliyuncs.com`。
参考 [官方 HTTP API](https://help.aliyun.com/zh/model-studio/fun-asr-recorded-speech-recognition-http-api)。

提交前持久化费用预留与提交意图；拿到 task_id 后仅轮询原任务。提交超时或进程中断后标记待核对，
不自动重复提交；管理员可从百炼控制台填写原任务 ID 继续。转写完成后及时保存原始句子与毫秒时间戳。

默认转写月预算 20 元，按整段时长与每秒 0.00022 元保守预估（实际以服务商账单为准）。
摘要月预算默认 2 美元，按既有提供商价格预留；失败或不确定请求保留预留费用。
家庭配置行在 PostgreSQL 中加锁，避免并发任务同时通过余额检查。

全文按模型上限分段处理，每个片段保存模型、提示词版本、费用预留、实际 token 数和结构化结果。
失败只重试未成功片段；重试需要管理员操作。每个要点包含主题、事实/作者观点/风险/方法和原文段落引用。
服务端验证引用确实存在，但语义准确性和语音数字识别仍需对照原文复核。
完整整理未完成前不发布“整期摘要”，原文始终可读。

## 定时任务

```sh
python manage.py run_program_subscriptions --family-id <实际家庭ID> --max-steps 12
```

建议 DSM 全天每 5 分钟运行，使用 `flock` 防重叠，日志追加 `logs/program-subscriptions.log`。
每个来源每小时检查一次；每轮最多 12 个处理步骤，每条最多连续三个步骤。每步最多一次付费调用。
`--collect-only` 只发现更新，不启动转写或摘要。真实失败返回非零；等待配置保留独立状态。
配置未完成不提交付费请求，配置后下一轮自动恢复。任务不能替代发布前的生产备份与验收。

## 本地验收与待办

- 实际官方来源验证：Dwarkesh 两期分别取得 291、537 段；In Good Company 取得完整音频 URL 和时长；Oaktree 列表与正文结构已验证。
- 用户 YouTube 样本 `OyiGHowGOSI`：频道与公开状态通过，24:46，无可用字幕，音频获取成功（24,036,621 字节）。
- SQLite 情报全量回归曾通过 101 项；随后新增归档版本与股票筛选等测试继续验证。
- Linux / PostgreSQL 情报与知识联合回归 172 项通过；最终段落边界固定后，27 项精选订阅测试再次全部通过，包含并发预算检查。
- 本地阅读列表、桌面原文页与 390px 手机布局已初验。
- 待完成：NAS 发布、正式 DSM 调度、用户配置百炼 Key、真实转写与真实中文整理验收。
- NAS 网络实测：Oaktree 可访问；YouTube、Dwarkesh、Acast 直连失败。现有电脑代理只监听本机；长期抓取需要另行确认 NAS 的联网路径。
- 暂未实现：跨期观点变化、自动识别图表中未口述的数字、付费会员节目、通用新增任意频道、自建语音模型。
- 当前补充入口支持粘贴完整文字稿；独立 SRT/VTT 文件导入尚未实现。

新依赖：beautifulsoup4 4.15.0、yt-dlp 2026.8.19。NAS 必须成功构建含新依赖的镜像后才能重启发布。
