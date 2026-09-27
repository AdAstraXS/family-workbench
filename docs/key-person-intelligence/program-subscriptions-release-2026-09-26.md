# 精选订阅发布记录 · 2026-09-26

## 应用发布

- 分支：`codex/intelligence-nas-release`，以 NAS 原运行版本 `7279ee4ac20f700cc60dcd77e8893f1d9a8857fd` 为基础。
- 已发布并推送 GitHub：`820775ef2b7141e31a29d0f0845b2d532c9b2737`。
- 镜像：`sha256:e488cf4ea3106359d05297b6e650c98fcaf98829eac71f2d827ddebe0486d953`，已构建并通过固定 `recreate-web` 子命令应用。
- 新迁移 intelligence 0009、0010 成功；Django check 无问题；外部设置页 200，内部 HTTP 301 转向 HTTPS。
- 配置入口：https://adastrax.top/intelligence/programs/settings/
- 四个来源启用；现有 DeepSeek 文本整理模型开启完整公开正文授权；百炼 Key 等待用户填写。

## 恢复点

数据库备份：`backups/family-workbench-before-programs-820775e.dump`（8.4 MB，pg_restore 目录验证通过）。
SHA-256：`5fd1de3bd67257b6790f824d566b3b9317543da3a9af0410ec80a904a3dcc529`。

旧源码：`backups/source-predeploy-7279ee4ac20f700cc60dcd77e8893f1d9a8857fd-20260926-223352.tar.gz`。
SHA-256：`8164a98375a548ddedca8854573192cef3b05ccce3e6fc5544b3e1a89c7d83c2`。

应用归档：`backups/family-workbench-820775e.tar.gz`。
SHA-256：`5853570e77180a175a463ad21c362b582afacc4315db39dd1e06878f82b47912`。

前后财务基线一致：账户 35、持仓 480、交易 1069、快照 2110、快照明细 13098、每日估值运行 73，
最新快照日期 2026-09-26。生产只新增功能表和用户授权的订阅配置，未导入本地数据库。
工作台 `.env` 哈希保持不变；DB 与 OpenD 未重建。

## 验收与继续工作

- Linux / PostgreSQL 情报与知识 172 项通过；后续最终精选订阅 27 项通过。
- 专用代理接入另增 4 项测试，31 项通过；桌面与 390px 阅读页及主动知识归档已验证。
- NAS Oaktree 直连可用；YouTube、Dwarkesh、Acast 直连失败。用户已选独立代理，正在单独配置。
- 待完成代理实际抓取、DSM 五分钟任务、百炼真实转写和真实摘要；目前不能标记完整自动流程验收完成。

## 2026-09-27 继续验收

- 应用代码 `3b8377bb239fba8115117e9025ac79e5f51dc3a7` 已交由“投研模块2”合并统一发布，
  后续不得独立用本分支覆盖生产应用。该任务报告统一版本 `7ebb3a8592889edcec2c632fd24cdd0b9433e303` 已安装。
- 独立代理使用用户明确授权的电脑当前 Clash 订阅，经 SSH 标准输入传输；凭据不进入仓库或日志。
  标准 YAML 配置启用后，内核 v1.19.27 状态 running；三个官方信源的代理请求测试成功。
- DSM 正式任务 ID 12，`family-workbench-program-subscriptions`，root，每天 00:00–23:55 每 5 分钟，
  已由用户完成 DSM 密码确认。命令带家庭 1、最多 12 步、文件锁及追加日志。
- 首轮 10:45 运行早于代理修复时间 10:45:32，四个来源失败；旧逻辑按小时节流，
  本次补丁将失败来源改为 5 分钟重试，成功来源仍按小时检查，并保留安全的网络诊断信息。
- 10:40 首次抓取前备份 `backups/family-workbench-before-program-first-run-20260927.dump`，24 MB，验证通过；
  SHA-256 `3c74cb94937aa6a8376f8d36e2f706263f97571087d28b3ef8a7613c8abe05bc`。
  当时基线：账户 35、持仓 480、交易 1069、快照 2139、明细 13284、每日估值 74，最新日期 2026-09-27。
- 百炼 Key 页面仍显示尚未配置；真实音频转写验收仍等待用户填写，不能声称全流程已完成。
- 统一发布协调方已确认 NAS 运行 `5a2359f55491df5e3ac022c31cf46f0c718a262c`，包含失败重试补丁。
  本任务对该补丁的 33 项 PostgreSQL 测试全部通过；协调方报告合并回归 399 项通过、3 项跳过。
- 11:02 真实首次收集成功：Dwarkesh、In Good Company、Oaktree 各 3 条。Dwarkesh 和 Oaktree
  已有完整原文；In Good Company 等待百炼配置。NAS 代理的 HTTP 订阅自动更新成功。
- YouTube 官方 Atom 返回频道标识 `FQsi7WaF5X41tcuOryDk8w`（无 `UC` 前缀），已获得 15 条，
  修正仅接受同一频道这两种形式；情报全量 112 项回归通过、1 项跳过，提交 `b9474a8`。
- 两份真实摘要遇到输出截断。修正为项目已有的 `thinking: {type: disabled}` 参数，并按既有输出预算
  限制要点长度；不提高全局预算。34 项针对性测试通过、1 项跳过，提交 `de67d16`，待统一发布后重试验收。
- DSM 隐藏容器详情曾在诊断输出中带出代理管理令牌；已在 NAS 私有目录随机轮换两个令牌并重建独立代理。
  新令牌接口验证成功，内核 running。订阅链接及节点凭据未输出；工作台应用 `.env` 未改动。

## 2026-09-27 午间实际发布与验收

- “投研模块2”明确交还发布窗口后，本任务发布统一应用 `45d7011d2576cb57e257cf7c6045de8badeceab0`，
  包含该任务 `385343f` 的完整投研与阅读代码；其最终文档 `d41d2e2` 随后合并。合并回归 400 项通过，3 项跳过。
- 后续修复 `ac20f34553b920ef0ab4049bdfb1cdd1f06a19f5` 增加年份引用核对：无引用支持的年份不展示、不能归档；
  管理员继续处理时仅重新整理未通过核对的片段，保留原文和费用预留。情报/知识回归 181 项通过，1 项跳过。
- `22937f8d57adb80430201c953af226787c7e94e7` 已安装：NAS 对同一有效 YouTube Atom 地址实测交替返回 200 与 404，
  仅为该公开订阅请求增加最多 3 次尝试，不重试 403/429 或付费接口。6 项针对性网络测试通过。
- Dwarkesh 最新访谈与 Oaktree 最新备忘录已经完成真实中文整理。Oaktree 一处凭空补出的年份经核对发现，
  修补后只重做该片段，现保留原文“8月17日”，没有再补年份。标签与语义仍需人工核对，不能视为事实认证。
- 本轮未新增迁移或依赖。生产只发生已授权的订阅、正文、处理记录和摘要写入；未修改财务数据。
  发布前后基线均为：账户 35、持仓 480、交易 1069、快照 2139、明细 13284、每日估值 74、最新日期 2026-09-27。

恢复点（均已通过包装器验证）：

| 发布 | 数据库备份 | SHA-256 |
| --- | --- | --- |
| 45d7011 | `family-workbench-before-programs-45d7011.dump` | `5210c9f385057e5a8de852f4eb4222a6847c5a5ef4e3c6420817ddf316b5701b` |
| ac20f34 | `family-workbench-before-programs-ac20f34.dump` | `f63789b0e54e6f18bac8157507ed88936af94f4e169a7d54a1a608f255a1162f` |
| 22937f8 | `family-workbench-before-programs-22937f8.dump` | `7f15208c7ac5e2cedc0c365a4a728ed533aedaad41e9b51ee82125d0d09f0eea` |

最终源码包 `backups/family-workbench-program-feed.tar.gz`，SHA-256
`ad115443cb9dcd75b2b2deb3d998fea98f437589acccbddd833ec969889f4094`。
回滚源码 `backups/source-predeploy-ac20f34553b920ef0ab4049bdfb1cdd1f06a19f5-20260927-115503.tar.gz`，
SHA-256 `bcf5a049d1880f836a417b8d9568171e0f3d09fc50fb2d3c2e23893ee1fa3ccd`。

### 官方频道备用发现

- 实际最新运行提交 `d433d52f3e0cb6c17549561599e518245832b070`，已推送并标记。
  YouTube 官方 Atom 持续间歇 404/500 时，从同一已批准频道的公开 videos 列表最多取得 3 期，
  逐期验证频道、公开状态和发布日期；403/429 不触发备用路径。39 项针对性回归通过，1 项跳过。
- 备用路径在本地真实核对最近三期 `OyiGHowGOSI`、`6NOiINBIbew`、`h1-YBhMV3CA`，
  日期分别为 2026-09-26/25/24。用户指定节目已在 NAS 完成标题、时长和字幕检查，状态为等待百炼配置。
- 12:05 定时任务对已成功来源进行下一次小时检查，补充历史目录 71 篇；旧内容未自动进行付费整理。
- 本版本生产系统检查通过、财务基线和 `.env` 哈希不变。真实音频转写仍待用户配置北京地域 Key。
- 数据库恢复点 `backups/family-workbench-before-programs-channel-fallback.dump`，35 MB，验证通过，
  SHA-256 `e949897b07342cfb3b9550533d1895fb4a5c57fea1f872debc4dfbb3c96bbb61`。
- 源码包 `backups/family-workbench-program-channel.tar.gz`，
  SHA-256 `7703038e9845f403ca1e5bcd1e3fbca5f02a1ae432dbc573af15b0399671fff1`。
- 回滚包 `backups/source-predeploy-22937f8d57adb80430201c953af226787c7e94e7-20260927-120602.tar.gz`，
  SHA-256 `3827a65622082db5ac0ac61f3b93c6ac1c97340868194e6e29258d0dc126da1a`。

## 最终转写验收 · 2026-09-27

- 用户提供配置文件后，仅在内存读取 Key，经工作台表单加密保存；使用导出文件中的北京业务空间。
  Key、上传凭证与临时音频 URL 未输出或提交仓库。
- 首次两笔任务均返回 `FILE_DOWNLOAD_FAILED`。NAS 音频端点的匿名 HEAD 为 200，但百炼下载仍失败，
  因此改为 NAS 获取音频后使用百炼模型绑定的私有临时上传，48 小时有效；不依赖百炼跨网下载。
- 最新实际运行并标记：`ea889db1fc8eb5b82cc718512471870c8bf4ed9a`，已推送 GitHub。
  `intelligence.0011_program_audio_transfer_recovery` 执行成功，仅增加错误码、历史尝试、恢复标志。
  44 项 PostgreSQL 回归全部通过，包含家庭预算并发、失败恢复历史、不可重复提交及上传凭据隔离；迁移检查通过。
- 管理员显式恢复两笔确认下载失败的任务，后台再次核对服务商终态后重新传送。旧任务号与费用预留保留，
  当前两期累计预留 1.9636 元，含初次失败预留；不作为实际账单。月预算仍为 20 元/2 美元。
- 两期新任务均在百炼返回 SUCCEEDED，工作台已完成原文落库和中文整理：
  - `/intelligence/programs/10/`：用户指定 `OyiGHowGOSI`，269 段、12 条阅读要点；结尾时间戳 24:40。
    摘要“原文71”准确定位 07:41 对应的微软突破表述。
  - `/intelligence/programs/4/`：In Good Company / Alexander Stubb，495 段、34 条阅读要点，结尾 49:21。
  - Dwarkesh `/programs/1/` 和 Oaktree `/programs/7/` 的官方原文与中文整理已完成。
- 12:50、12:51、12:55 正式任务均无失败。NAS 加密音频临时目录剩余文件数为 0。
  系统检查正常，财务基线不变，`.env` SHA-256 仍为
  `78d7894b721698adf1d5a4ce3c969e4983b2ee079cdebdb5082db3aca985b51c`。
- 归档维持成员主动点击，未把所有订阅自动写入知识库。引用定位和桌面真实阅读页已验收；
  早期 390px 页面验证已通过，本轮 Chrome viewport 设置未改变实际宽度，未把该次截图算作新的移动端验收。
  转写的人名/数字和模型主题标签仍需核对；不能读取视频图表中未口述的信息。

最终恢复点：

- 数据库：`backups/family-workbench-before-programs-ea889db.dump`，36 MB，已验证。
  SHA-256 `9e602d7f585c43c02162c4f953db8adfeabaf583a5ddc8a72b5f242812aa4a5d`。
- 源码包：`backups/family-workbench-program-asr-upload.tar.gz`。
  SHA-256 `9790c30395982208afab39b222b2219373a12e993135e8a9b3ce661cc6ba90a4`。
- 回滚源码：`backups/source-predeploy-d433d52f3e0cb6c17549561599e518245832b070-20260927-124119.tar.gz`。
  SHA-256 `27bd9b3934f3653e5c54f8c8a4db7fc06abf4314bf4a766758e3f07fded7ddbc`。
