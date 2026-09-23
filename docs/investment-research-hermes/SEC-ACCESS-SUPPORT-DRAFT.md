# SEC 访问问题说明（已发送）

2026-09-21 20:46（北京时间），由 `xuxiao1108@gmail.com` 发送至 `webmaster@sec.gov`。
发送时已将下文的两处公网 IP 占位符替换为实测出口 IP；仓库不保存实际 IP。

收件方：webmaster@sec.gov
主题：Request Rate Threshold Exceeded (HTTP 403) for low-rate EDGAR research requests

Hello SEC Webmaster,

I am testing read-only access to public EDGAR filings for a small personal research application. Requests to `data.sec.gov/submissions/CIK0000789019.json` and `CIK0000320193.json` have sometimes succeeded, but requests to `www.sec.gov/files/company_tickers.json` and filing HTML under `www.sec.gov/Archives/edgar/data/` return HTTP 403. The response page is titled “SEC.gov | Request Rate Threshold Exceeded.” A single request after more than ten minutes without accessing `www.sec.gov` still returned 403.

The application declares a User-Agent containing its name and a real contact email. The individual test ran at 1–2 requests per second, below the published threshold. The same result occurred from two distinct public network exits: Windows host `[insert public IP]` and NAS `[insert public IP]`. Please advise whether these exits are restricted and what we should correct to restore compliant access. We can provide the exact request timestamps, User-Agent and error response on request.

Example URLs:

- `https://www.sec.gov/files/company_tickers.json`
- `https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/aapl-20250927.htm`

Thank you.

用户已确认收件人及两条公网出口 IP 的披露。邮件未包含 API Key、NAS 内网地址、生产数据库或私密投研内容。
