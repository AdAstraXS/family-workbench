# 官方 IR 首批覆盖范围

确认日期：2026-09-26。用户要求官方 IR 自动获取优先，手动导入为补充，并将
TSMC、SpaceX、SK 海力士、博通、AMD、Intel 全部加入首批范围。

## 已确认的 13 家公司

| 公司 | 识别代码 | 官方发现入口 |
| --- | --- | --- |
| Microsoft | MSFT | https://www.microsoft.com/en-us/investor |
| Apple | AAPL | https://investor.apple.com/ |
| Alphabet | GOOG / GOOGL，同一发行人 | https://abc.xyz/investor/ |
| Amazon | AMZN | https://ir.aboutamazon.com/quarterly-results/default.aspx |
| Meta | META | https://investor.atmeta.com/investor-events/default.aspx |
| NVIDIA | NVDA | https://investor.nvidia.com/financial-info/financial-reports/default.aspx |
| Tesla | TSLA | https://ir.tesla.com/ |
| TSMC | TSM（美股 ADR）；2330（台湾上市） | https://investor.tsmc.com/english/quarterly-results |
| SpaceX | SPCX | https://ir.spacex.com/financials/ |
| SK 海力士 | 000660（韩国上市） | https://www.skhynix.com/ir/UI-FR-IR01/ |
| Broadcom | AVGO | https://investors.broadcom.com/financial-information/quarterly-results |
| AMD | AMD | https://ir.amd.com/financial-information/financial-results |
| Intel | INTC | https://www.intc.com/financial-info/financial-results |

十三家公司共用发现、归档和固定引用流程已实现；开源复用、真实验收和已知限制见
[实现与验收说明](investment-research-ir-implementation.md)。特斯拉官方附件可读，
实时目录仍返回访问拒绝，必须明确区分已核实链接快照与自动更新成功。

## 本次新增六家的入口核对

2026-09-26 通过公开网页读取核对；这只能证明入口或链接存在，不代替本机/NAS
下载、解析、归档、引用与增量同步的真实验收。

- **TSMC**：已读取 [2026 Q2 结果页](https://investor.tsmc.com/english/quarterly-results/2026/q2)，
  页面列出财务报表、演示材料、管理报告、财报新闻稿、电话会文字稿和回放。
  季度总入口会跳转到最新季度（本次为 Q3），需要区分预告页与已发布结果。
- **SpaceX**：已读取 [IR 首页](https://ir.spacex.com/investors/default.aspx) 及金融资料页，
  确认 Quarterly Results、SEC Filings 和 Events 栏目；当前文本读取未展开季度资料列表。
  官方 [Q2 结果发布预告](https://ir.spacex.com/updates/releases-details/2026/SpaceX-to-Post-Second-Quarter-2026-Results-and-Host-Webcast-on-August-4-2026-2026-g8layJlbFm/default.aspx)
  可读取。下一步需验证动态列表与真实财报文件；不得把预告算成结果，也不要求凑齐上市前四季度资料。
- **SK 海力士**：IR 首页含 Earnings Release 下载链接；
  [IR Material](https://www.skhynix.com/ir/UI-FR-IR12_T1/) 文本读取未展开资料列表。
  [官方 Newsroom IR 栏目](https://news.skhynix.com/en/category/ir/) 可作为官方发现补充。
  自动读取方式、英文资料覆盖和附件归属仍待验证。
- **Broadcom**：季度页可读取按财年/季度排列的结果链接；本次季度页显示到 FY2026 Q2，
  但 [IR 首页](https://investors.broadcom.com/) 已列出 FY2026 Q3 发布链接。
  发现过程需交叉检查官方新闻稿，不能仅凭一个目录宣称“已同步最新财报”。
- **AMD**：财务结果页直接列出结果 HTML、电话会文字稿、演示 PDF、财务表 PDF/XLSX。
  页面显示最近四个已发布季度；附件下载及正文引用仍需程序验收。
- **Intel**：财务结果页直接列出结果 HTML/PDF、演示 PDF、Prepared Remarks 和电话会音频。
  Prepared Remarks 应标为“管理层准备稿”，不能标成含完整问答的电话会文字稿。

## 共同实现与验收边界

1. 共用发现、下载、正文提取、不可变版本、去重、来源引用和同步状态；公司差异集中于入口配置
   及必要的小型适配，不复制 13 套采集/存储程序。外部附件只接受经官方页面核实的链接和受限域名/路径。
2. 首批回溯最近四个**已发布**季度，并获取可用的最新投资者介绍或股东信。财报新闻稿、报表、
   演示材料、管理层准备稿、完整电话会文字稿分别标注。未提供的材料明确说明，不能让 AI 补造。
3. 保存公司身份、财年/季度、期间截止日、发布日期、原币种与单位、原始 URL 和内容版本；
   不把美元、新台币、韩元或自然年/财年混用。TSM/2330、GOOG/GOOGL 按发行人归一，避免重复下载。
4. 当前 `ExploreDossierForm` 仅允许 US 普通股。接入 SK 海力士时，需要让受支持的非美股公司
   能从“先了解一家公司”进入；公司 IR 采集不得依赖先有持仓、SEC CIK 或 10-K。
   TSMC 和 SK 海力士的资料格式应按实际披露处理，不强套现有 10-K 章节与 XBRL 指标规则。
5. 每家公司分别记录“入口已核实、发现已验收、正文已验收、引用已验收、增量已验收”。
   至少抽查一份真实结果正文；四季度覆盖按实际可得材料核对；重复同步不新增重复资料，失败保留旧版本。
6. 页面展示按公司/季度组织的资料、明确类型、可追溯原文及缺口；同步状态放在次级区域。
   官方自动获取失败时保留来源链接和短原因，再提供手动导入。GET 不联网、不写库。
7. 该范围不自动增加追踪指标、不改写个人判断、不触发云端 AI；材料接入后沿用现有私密草稿与人工确认流程。
   用户已要求 SEC 验收暂缓，本批不恢复 SEC 验收，也不修改已有 SEC 定时任务。

上方六家入口核对保留为实现前调查记录；当前进展以实现与验收说明为准。
