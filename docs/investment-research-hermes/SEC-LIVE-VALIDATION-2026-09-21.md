# SEC 真实来源验收记录（2026-09-21）

范围：在隔离工作树 `codex/investment-research-sec-explore`，计划对 MSFT、AAPL 各取最近的 10-K、10-Q、8-K，核对“发现文件 → 取得主 HTML → 提取正文 → 固定引用及页面回跳”。本地失败后增加 NAS 上的只读 HTTP 检查；不写生产数据库、不部署、不调用云端 AI。

## 实际结果

- 使用项目 `SecClient` 访问官方 `https://www.sec.gov/files/company_tickers.json`，MSFT 与 AAPL 的首个 CIK 解析请求均返回 HTTP 403。新标的若尚未缓存 CIK，会在此处停止。
- 将 User-Agent 改为含本机 Git 配置的真实联系邮箱后，容器内同一请求仍返回 HTTP 403。联系邮箱未写入仓库、日志或报告。
- Windows 主机使用同一身份标识直接访问同一官方 URL，也返回 HTTP 403。不能把此次失败归因于 Docker 独有网络设置；原因可能是本机出口的 SEC 访问限制，但尚无证据确定具体原因。
- NAS 上只读 `curl` 对 `www.sec.gov/files/company_tickers.json` 和一份真实 AAPL Archives 主 HTML 也返回 403；对 `data.sec.gov/submissions/CIK0000789019.json` 返回 200。未在 NAS 写文件、运行数据库命令或改服务。
- 使用项目 `SecClient.get_filings()` 并直接传入已知 CIK，真实 submissions 可解析：MSFT 81 份、AAPL 149 份目标类型记录。最近 20 份中，MSFT 有 2 份 10-K、5 份 10-Q、13 份 8-K；AAPL 有 1 份 10-K、5 份 10-Q、13 份 8-K。两家公司最新 10-K、10-Q、8-K 的 accession 与主 HTML URL 均可构造。这里的数字是检查当时的结果，不保证后续不变。
- 随后在一次性内存数据库中尝试把 AAPL 真实 submissions 同步到档案并读取列表页，`data.sec.gov` 该次请求超时；脚本未执行到列表页。之前的 200 结果不能保证该网络路径持续可用。内存数据库随容器退出消失。
- 项目客户端对两家最新 10-K 的 `www.sec.gov/Archives` URL 均返回 HTTP 403；NAS 对 AAPL 10-K Archives URL 也返回 403。SEC 官方网页检索通道能打开 AAPL 的该 HTML，但这是另一条访问通道，不能替代项目客户端正文下载验收。
- 本地 403 HTML 页标题为 `SEC.gov | Request Rate Threshold Exceeded`。本次单客户端设置为 1–2 次/秒，但 SEC 可能按共享出口累计请求；无法据此断定是本项目自身触发。SEC 官网说明单一用户/应用总访问上限为每秒 10 次，降低至阈值以下 10 分钟后可恢复访问，见 https://www.sec.gov/about/privacy-information 。
- 本次没有抓取到真实正文，没有证据宣称苹果或微软的 10-K/10-Q/8-K 正文、引用及页面回跳已通过真实来源验收。没有修改代码或生产数据。

## 已有离线证据与剩余工作

项目离线测试已覆盖两家以上美股的元数据处理、SEC 10-K/10-Q/8-K 模拟 HTML 的正文快照、重复抓取、失败保留旧版本、固定引用和访问权限。这些测试不能替代真实 SEC HTML、网络边缘策略或页面视觉验收。

先停止 SEC 请求至少 10 分钟，再对 `www.sec.gov` 做一次低频复测；若仍为 403，应检查共享出口访问情况，并按 SEC 官方 FAQ 向 webmaster@sec.gov 提供错误文本与出口 IP 询问原因。不要改用伪装身份或绕过规则。该阻断同时影响新标的 CIK 解析和 Archives 正文获取；不能通过“已知 CIK 可读 submissions”推断整条链路可用。在具备正常官方访问的受控网络环境中，用已配置的合规 User-Agent、低于项目 5 次/秒上限的速率重试；随后各选 10-K、10-Q、8-K，检查正文长度、关键章节/表格可读性、官方 URL、版本哈希和引用回跳，再进行桌面及窄屏页面验收。生产试运行前另行核对模型数据用途和费用上限。

注意：SEC 官方说明 submissions 主 JSON 至少包含近一年或最近 1,000 份申报；更早历史可能位于附加 JSON。当前项目只处理主 JSON 的 `filings.recent`，因此“官方资料列表”不等于完整历史申报档案。参考：https://www.sec.gov/search-filings/edgar-application-programming-interfaces
