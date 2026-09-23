# 10-K Item 8 财务报表与附注目录（2026-09-23）

资料页在已保存的 10-K 正文版本上，先限定 Item 8 的范围，再识别财务报表标题及 `Notes to Financial Statements` 后连续编号的附注标题。每个链接保留原文版本 ID、字符起止位置及标题 SHA-256，点击后高亮原文。索引只提供阅读入口，不提取财务数值，也不把“找到附注”当作完成核查。未找到标题时不猜测位置；识别范围之外的内容仍需阅读官方完整文件。

对三份 SEC 官方 10-K 原文使用当前正文提取器和同一目录函数试跑，未把苹果或特斯拉正文写入生产数据库：

| 文件 | 主要报表 | 连续编号附注 | 重点核对 |
|---|---:|---:|---|
| [微软 FY2026](https://www.sec.gov/Archives/edgar/data/789019/000119312526323660/msft-20260630.htm) | 5 | 18 | Note 6 固定资产起点 257,073；Note 13 租赁起点 281,176 |
| [苹果 FY2025](https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/aapl-20250927.htm) | 5 | 13 | Note 5 固定资产；Note 8 租赁 |
| [特斯拉 FY2025](https://www.sec.gov/Archives/edgar/data/1318605/000162828026003952/tsla-20251231.htm) | 5 | 17 | Note 6 固定资产；Note 10 租赁 |

这些数字是**识别出的标题数**，不是独立于原文的完整性证明。特斯拉的资产负债表内有单独一行 `Stockholders’ equity`；最初试跑会将其误认成权益变动表，已改为要求标题包含 `Statements of ... Equity` 或 `... Equity Statements`，并识别其实际标题 `Consolidated Statements of Redeemable Noncontrolling Interests and Equity`。苹果和特斯拉使用标题式大小写，微软使用全大写；附注规则兼容两者。

[EdgarTools](https://github.com/dgunning/edgartools) 已有报表、附注和 XBRL 导航能力，[sec-parser](https://github.com/alphanome-ai/sec-parser) 能构建 HTML 语义树。这次借鉴了**报表与附注分层导航的产品功能**，没有复制或引入其代码：现有产品的引用指向已经保存的规范化正文字符位置，直接替换解析器会改变这些位置；而为了几种已验证的标题引入新的解析依赖也会扩大 NAS 镜像变更。以后需要跨文件的结构化财务事实或复杂 HTML 时，应以原文坐标可校验为条件，再评估这些库。

离线测试覆盖三种标题样式、Item 8 边界、附注序号及排除表内行标题，并检查页面链接能回到固定版本原文。投研、AI、IPO、组合与期权轮回归共 718 项通过、1 项跳过；迁移检查无变化。
