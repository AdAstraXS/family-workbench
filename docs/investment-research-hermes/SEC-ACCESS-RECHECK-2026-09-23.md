# SEC 访问复测（2026-09-23）

范围：只读、低频、无重试地检查本地 Docker 和 NAS 两条出口；使用有联系信息的 User-Agent 和 `Accept-Encoding: gzip, deflate`，不写生产数据库、不运行同步任务、不部署。

## 结果

- 本地 Docker 使用隔离分支的 `SecClient`：`www.sec.gov/files/company_tickers.json` 成功解析 MSFT CIK `0000789019`；`data.sec.gov/submissions/CIK0000789019.json` 返回 81 份目标类型申报；最近的 MSFT 10-K、10-Q、8-K 主 HTML 均成功下载，解压后分别为 8,585,611、7,732,058、28,669 字节。三份文件 accession 分别为 `0001193125-26-323660`、`0001193125-26-191507`、`0001193125-26-380280`。未保存真实正文到数据库。
- NAS 使用 `curl --compressed` 只读请求：ticker 列表、MSFT submissions 和最近 10-K 主 HTML 均返回 HTTP 200。该检查没有运行生产 Django 同步，也没有验证页面引用。
- NAS 检查时运行提交为 `2895dbd5fc35637859c2840d2b20256b4445f2cf`；该提交中的 `SecClient` 尚未包含 `Accept-Encoding` 修正。隔离分支修正提交 `a9e7a2651f3d4c0759426ff88c5fbf7c621f0a2a` 未由本次操作部署。

这说明今天所测的两条出口和三个地址可以访问 SEC，不能据此推断先前的 403 原因，也不能保证之后持续可用。当前仍欠缺生产代码的合规请求头部署，以及真实正文进入版本快照后的提取质量、固定引用和页面回跳验收。SEC 仍要求整个网络的自动请求总量不超过每秒 10 次；本次成功不改变该要求。
