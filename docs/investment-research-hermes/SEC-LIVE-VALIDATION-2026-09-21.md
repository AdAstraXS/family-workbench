# SEC 真实来源验收记录（2026-09-21）

范围：在本地隔离工作树 `codex/investment-research-sec-explore`，计划对 MSFT、AAPL 各取最近的 10-K、10-Q、8-K，核对“发现文件 → 取得主 HTML → 提取正文 → 固定引用及页面回跳”。不连接 NAS、不写生产数据库、不调用云端 AI。

## 实际结果

- 使用项目 `SecClient` 访问官方 `https://www.sec.gov/files/company_tickers.json`，MSFT 与 AAPL 的首个 CIK 解析请求均返回 HTTP 403；因此后续 submissions、Archives 正文未执行。
- 将 User-Agent 改为含本机 Git 配置的真实联系邮箱后，容器内同一请求仍返回 HTTP 403。联系邮箱未写入仓库、日志或报告。
- Windows 主机使用同一身份标识直接访问同一官方 URL，也返回 HTTP 403。不能把此次失败归因于 Docker 独有网络设置；原因可能是本机出口的 SEC 访问限制，但尚无证据确定具体原因。
- SEC 官方网页检索通道能读取公司代码映射，但这是另一条访问通道，不能替代项目客户端对 `data.sec.gov` 和 `www.sec.gov/Archives` 的端到端验收。
- 本次没有抓取到真实正文，没有证据宣称苹果或微软的 10-K/10-Q/8-K 已通过真实来源验收。没有修改代码或生产数据。

## 已有离线证据与剩余工作

项目离线测试已覆盖两家以上美股的元数据处理、SEC 10-K/10-Q/8-K 模拟 HTML 的正文快照、重复抓取、失败保留旧版本、固定引用和访问权限。这些测试不能替代真实 SEC HTML、网络边缘策略或页面视觉验收。

在具备正常 SEC 访问的受控网络环境中，先用已配置的合规 User-Agent、低于项目 5 次/秒上限的速率重试官方 ticker 映射与两家公司的 submissions。若仍返回 403，应停止并查明出口限制，不改用伪装身份或绕过规则。随后各选 10-K、10-Q、8-K，检查正文长度、关键章节/表格可读性、官方 URL、版本哈希和引用回跳，再进行桌面及窄屏页面验收。生产试运行前另行核对模型数据用途和费用上限。

注意：SEC 官方说明 submissions 主 JSON 至少包含近一年或最近 1,000 份申报；更早历史可能位于附加 JSON。当前项目只处理主 JSON 的 `filings.recent`，因此“官方资料列表”不等于完整历史申报档案。参考：https://www.sec.gov/search-filings/edgar-application-programming-interfaces
