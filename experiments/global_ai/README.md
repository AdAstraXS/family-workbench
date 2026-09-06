# 全局 AI 隔离原型

这是源码级实验，不是网站功能。仅依赖 Python 标准库；不导入 Django、不连接生产数据库。
数据是 Alice/Bob 两名虚构成员、彼此独立的账本快照与投资持仓，以及三篇资料，不代表真实家庭财务。

## 运行

从仓库根目录运行：

```powershell
python -m unittest experiments.global_ai.test_spike -v
python -m experiments.global_ai.run
```

第二条命令验证 Codex 0.153.4 的 stdio 握手、会话创建／读取和 MCP 工具发现，无模型请求。
使用独立临时 CODEX_HOME，不读取原有 Codex 登录文件或复制其凭据；关闭 shell、多 Agent、网页搜索。
仅当前版本通过实验验证，其他版本返回 version_not_validated。

真实调用需显式 --live，会从环境或根目录 .env 读取 KNOWLEDGE_TEXT_AI_API_KEY，只送虚构数据：

```powershell
python -m experiments.global_ai.run --live --run-id synthetic-v2
```

已有运行号会被 SQLite 拦截，不自动重发；不要通过换运行号重试结果不明的请求。
两轮实验 synthetic-v1、synthetic-v2 已完成。新运行号表示新的、可能收费的实验。
每轮直接 API 最多 4 次请求、每次 max_tokens=1200、请求体最多 40 KB、响应最多 256 KiB，
每次 HTTP 超时 45 秒，不自动重试且禁止重定向；这不是供应商账单的美元硬上限。
Codex provider 检查不调用 turn/start，当前 chat 协议在 thread/start 阶段已被拒绝。

## 文件与边界

- core.py：模块独立的虚构数据、Decimal 汇总、固定调用者范围、只读工具；SQLite 记忆和对话边界实验。
- mcp_server.py：将同一工具边界暴露为本地 stdio MCP。CLI actor 由可信宿主指定，不是公开登录接口。
- run.py：普通 API 工具循环、Codex 协议与 provider 兼容性探针、限次与结果记录。
- test_spike.py：资产与权限针对性测试、实际 MCP 子进程、超时和进程清理。

输出位于 outputs/global-ai-spike/，不纳入源码提交；包含 probe.sqlite3、只含虚构内容的 JSON 结果。
生成的协议 schema 也只存放在该目录，不修改安装的 Codex。

账本工具只回答全部账户资产，投资工具只回答投资账户持仓；原型不比较、对账或合并两组金额。
当前原型没有网页登录、流式前端、家庭共享记忆编辑、语义检索、历史答案撤权、真实模块读取服务、
云端发送授权管理或正式任务队列。数据层测试通过不证明这些尚未实现的功能通过。
SQLite 的 run 标记只保证本地重复提交被拒绝，不代表供应商侧 exactly-once 计费保证。
文档注入测试验证工具不能越权，不是模型对抗评测。

本地测试的 delete/confirm 是可信宿主操作，不作为模型工具提供；对话切换范围要求新建会话。
迁入 Django 时 actor 必须来自登录 session，不能信任浏览器、提示词或模型提供的成员 ID。

本轮模型建议两次均未通过人工质量验收，禁止把运行状态 complete 理解为建议合格。
详见 [原型结果](../../docs/global-ai-spike-results.md)。

## A01–A03 模型评测

`evaluate_answers.py` 用固定虚构数据评测三个必须调用模型的案例，每个案例独立运行三次，另带一条
“外部注资不能清除历史回撤”的留出案例。它支持DeepSeek Flash、DeepSeek Pro与GLM三个
OpenAI兼容候选。
它不导入 Django、不读取网站或生产数据库，也不比较账本快照与投资账户余额。先检查发送清单：

```powershell
python -m experiments.global_ai.evaluate_answers --provider glm
```

确认清单后才能显式运行；运行号会在第一次请求前落盘，同一运行号不能重用：

```powershell
python -m experiments.global_ai.evaluate_answers --provider glm --live --run-id glm-5-3-flash-YYYYMMDD-r1 --env-file <本地.env路径> --budget-usd 1.00
```

正式运行最多 36 次 HTTP 请求，每次请求体不超过 20,000 字符、输出不超过 2,000 token。
DeepSeek按公开峰值单价估算；GLM-5.3 Flash发布日的公开价格表尚未同步，暂用每百万输入1.5美元、
输出9美元的安全单价控制实验，不能把该数值当作智谱报价。请求不重试、不跟随重定向；
网络结果不明时保留运行记录并停止。程序只做客观风险词检查，最终是否通过仍需人工审阅九条核心
轨迹及留出案例。
