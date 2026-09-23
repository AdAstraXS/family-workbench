# 真实 SEC 文件验收（2026-09-23）

在隔离 Docker 容器和一次性内存 SQLite 数据库中，使用项目客户端以每秒 0.5 次、零重试请求 SEC。没有读取或写入 NAS PostgreSQL，也没有调用云端 AI。测试结束后内存数据库消失；只为排查表格提取问题在本机临时目录缓存过一份公开的 MSFT 10-K HTML，未放入仓库。

## 真实来源流程

| 公司 | 元数据 | 正文版本与引用回跳 |
| --- | --- | --- |
| MSFT | 成功归档 81 条目标类型申报 | 最近 10-K、10-Q、8-K 均成功：正文分别为 341,483、221,584、3,937 字；每份建立 `sec-html-v2` 版本，固定引用回到同一版本并高亮原句。 |
| AAPL | 成功归档最近 100 条目标类型申报 | 最近 10-K、10-Q、8-K 均成功：正文分别为 214,766、88,775、5,224 字；每份建立 `sec-html-v2` 版本，固定引用回到同一版本并高亮原句。 |

首次真实检查发现：微软文件的财务表格虽保留数字，却把单元格标题和各期数字拆成多行，无法直观看出列关系。提取器现将同一表格行保留在一行，并把源码格式化空白折叠为空格。修正后 MSFT 10-K 的关键行可读为 `Total revenue | … | 331,839 | … | 281,724 | … | 245,122`，`Total assets | … | 758,376 | … | 619,003`，`Net cash from operations | … | 182,935 | … | 136,162 | … | 118,548`；AAPL 10-K 的 `Total assets` 行同样保留当期与上期数字。空白表格单元格仍以分隔符占位，不猜测其含义。

提取器标识从 `sec-html-v1` 升为 `sec-html-v2`；同一原始文件若曾由旧提取器建立版本，再次主动提取会追加新版本，旧正文和旧引用不被改写。离线回归已覆盖该行为。这里验证的是文件正文与引用链路，不代表 AI 对整份长报告的分析质量：当前 AI 草稿仍只取正文前 16,000 字并明确标注部分覆盖。

## 生产部署与单文件试运行

投研分支已合并 NAS 原运行提交 `2895dbd5fc35637859c2840d2b20256b4445f2cf`，保留其中的期权车轮改动。合并后 `investment_research ipo portfolio option_wheel` 共 693 项测试通过、1 项跳过；Django `check`、迁移检查及 `git diff --check` 通过。`app/requirements.txt`、`app/Dockerfile` 和 `docker-compose.yml` 未变化。目标提交 `422a0e753eda836077fa8feb883cb98882acfcf6` 已推送 GitHub。

部署前 NAS PostgreSQL 备份为 `/volume1/docker/family-workbench/backups/family-workbench-pre-research-20260923-1055.dump`，SHA-256 为 `20eb18a85542aabf707c1a212405740339a690dc525a6e9c8a54551c03f94ae3`；受限部署包装器验证了非空及 `pg_restore -l`。源码回滚包为 `/volume1/docker/family-workbench/backups/source-predeploy-2895dbd5fc35637859c2840d2b20256b4445f2cf-20260923-105345.tar.gz`，SHA-256 为 `7f18cdb00d5e13b92c7b34aaf5dfa86e00f7cddc88bfb50c2d62683b8f39da82`。上传归档哈希与本地精确提交归档一致。只重启 Web，启动日志显示 `investment_research.0003` 和 `0004` 均迁移成功，生产 Django 检查无问题。

部署前后业务基线一致：InvestmentAccount 35、InvestmentPosition 474、InvestmentTransaction 1061、PortfolioSnapshot 2023、PortfolioSnapshotPositionLine 12534、DailyPortfolioValuationRun 69，最新快照日期 2026-09-23。数据库和 OpenD 容器保持健康，`.env` 哈希未变化；内外网研究入口正常跳转登录页。NAS 已标记运行提交为 `422a0e753eda836077fa8feb883cb98882acfcf6`。

在已登录的生产研究页面，仅对现有微软档案的 2026-07-29 10-K（accession `0001193125-26-323660`）点击一次“提取这份正文”。页面显示“SEC 正文已保存”和正文版本 1；`Total revenue` 表格行保持 `331,839 / 281,724 / 245,122` 的列顺序。用该行生成的固定引用回到版本 1，并高亮完全相同的原句。没有同步其他公司、调用生产云端 AI 或修改正式判断。

限制：这次生产试运行只覆盖一份 10-K。10-Q、8-K 以及苹果的真实内容已在隔离数据库通过，未批量写入生产。长文 AI 草稿仍只使用前 16,000 字，真实云端模型质量和费用配置未在本次验收。
