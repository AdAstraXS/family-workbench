# 微软跨期指标 XBRL 试验（2026-09-23）

使用隔离环境中的 EdgarTools `5.58.0` 读取微软 [FY2024](https://www.sec.gov/Archives/edgar/data/789019/000095017024087843/msft-20240630.htm)、[FY2025](https://www.sec.gov/Archives/edgar/data/789019/000095017025100235/msft-20250630.htm) 和 [FY2026](https://www.sec.gov/Archives/edgar/data/789019/000119312526323660/msft-20260630.htm) 三份 10-K 的完整 XBRL。针对先前保存在本地 `outputs/msft-2026-10k-cross-period-metrics-2026-09-23.md` 的微软跨期原文核对表，逐项过滤概念、完整财年或期末日期、维度与成员，金额使用 `Decimal` 转换为亿美元，再与原表对比。每个选中的 XBRL `fact_id` 均在对应 SEC 主 HTML 中验证为实际元素 ID，可通过链接跳到原始披露。可复跑的[试验脚本](xbrl_msft_reconcile.py)不接入 Django，也不改变 NAS 依赖或生产数据。

| 口径；单位：亿美元 | FY2024 | FY2025 | FY2026 | XBRL 来源与限制 |
|---|---:|---:|---:|---|
| 经营现金流 | 1,185.48 | 1,361.62 | [1,829.35](https://www.sec.gov/Archives/edgar/data/789019/000119312526323660/msft-20260630.htm#F_1bf2d932-9a21-41e8-9d22-32533c8ae3e5) | `us-gaap:NetCashProvidedByUsedInOperatingActivities`，FY2026 文件中三个完整财年 |
| 固定资产现金增加额 | 444.77 | 645.51 | [1,159.48](https://www.sec.gov/Archives/edgar/data/789019/000119312526323660/msft-20260630.htm#F_09f41249-b58f-478b-a38c-f33fcdfa87ec) | `us-gaap:PaymentsToAcquirePropertyPlantAndEquipment`；现金流出在该事实中为正数，不额外取负 |
| 简化自由现金流 | 740.71 | 716.11 | 669.87 | 以上两项相减；**自定义计算，不是 XBRL 单独事实，也不是 AI 投资回报率** |
| 固定资产折旧费用 | 152 | 220 | [343](https://www.sec.gov/Archives/edgar/data/789019/000119312526323660/msft-20260630.htm#F_87edb2e0-abaa-4a71-80d7-0fecd3ad7e2e) | `us-gaap:Depreciation`，不可与“折旧、摊销及其他”合计混用 |
| 融资租赁新增使用权资产（非现金） | 116.33 | 205.11 | [246.08](https://www.sec.gov/Archives/edgar/data/789019/000119312526323660/msft-20260630.htm#F_22dddab0-1e45-4440-ab05-e3c6927c790c) | `us-gaap:RightOfUseAssetObtainedInExchangeForFinanceLeaseLiability` |
| 融资租赁本金现金支付 | 12.86 | 22.83 | [31.01](https://www.sec.gov/Archives/edgar/data/789019/000119312526323660/msft-20260630.htm#F_6febe00c-805d-4722-9426-b3fb24b75829) | `us-gaap:FinanceLeasePrincipalPayments` |
| 融资租赁资产净额（期末） | 258.62 | 440.15 | [672.81](https://www.sec.gov/Archives/edgar/data/789019/000119312526323660/msft-20260630.htm#F_c651dd3d-3237-40f3-83fd-514b22bbe5fb) | `us-gaap:PropertyPlantAndEquipmentNet` 加租赁合同期限维度 `msft:FinanceLeaseMember`；FY2024 比较数来自 FY2025 文件 |
| 融资租赁负债（期末） | 271.45 | 461.72 | [665.94](https://www.sec.gov/Archives/edgar/data/789019/000119312526323660/msft-20260630.htm#F_2e6e6970-8107-497c-960b-f9e603d7360b) | `us-gaap:FinanceLeaseLiability`；FY2024 比较数来自 FY2025 文件 |
| 尚未开始的租赁（未来安排，非当期现金支出） | 1,170 | 927 | [3,291](https://www.sec.gov/Archives/edgar/data/789019/000119312526323660/msft-20260630.htm#F_01dd074c-5f3e-4bc5-a343-3c43e3faa4d7) | 见下方的披露口径变化，不能机械相加 |
| Microsoft Cloud 收入（非 Azure AI 单独收入） | 1,377 | 1,689 | [2,144](https://www.sec.gov/Archives/edgar/data/789019/000119312526323660/msft-20260630.htm#F_b32fd3c0-ac9c-40c1-be9c-0e7bc0994816) | `us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax` 加 `msft:MicrosoftCloudMember` 产品维度；FY2024 使用后续文件按新口径列示的比较数 |

十行、三个财年都与原表相符。最重要的核对陷阱是“尚未开始的租赁”：微软 [FY2024 原始附注](https://www.microsoft.com/investor/reports/ar24/)分别列出经营租赁 [86 亿](https://www.sec.gov/Archives/edgar/data/789019/000095017024087843/msft-20240630.htm#F_8ae17124-01c7-4d56-934e-9907d8e393b9)与融资租赁 [1,084 亿](https://www.sec.gov/Archives/edgar/data/789019/000095017024087843/msft-20240630.htm#F_e095f40b-2594-46f4-bee1-0676b4a638e6)，合计 1,170 亿；FY2025 和 FY2026 的附注则各披露一个总数，XBRL 在经营与融资租赁成员下**重复同一个总数**。若把 FY2026 两个 3,291 亿事实相加，会错误地翻倍。因此脚本对 FY2024 明确求和，对后两年先验证重复事实相等，再只取一次。这是根据原文写出的微软专用规则，不能直接套到别家公司。

另一个口径变化是 FY2024 Microsoft Cloud：其 [FY2024 原报告](https://www.microsoft.com/investor/reports/ar24/)为 1,374 亿美元，[FY2025 年报](https://www.microsoft.com/investor/reports/ar25/)和 FY2026 10-K 的重列比较数为 1,377 亿美元。跨期表统一采用后续披露口径，脚本只从 FY2026 文件提取这一比较数；不能把两个定义的 FY2024 值当成数据源冲突后任选一个。

**接入判断：**EdgarTools 的完整单份 10-K XBRL 能读取标准事实、自定义维度及原始事实 ID，明显优于只读 SEC Company Facts API；后者只汇总适用于整个公司的非自定义分类法事实，因此找不到本试验所需的 Microsoft Cloud 维度和融资租赁资产净额维度。[SEC API 说明](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)、[EdgarTools 文档](https://github.com/dgunning/edgartools/blob/main/edgar/docs/Filing.md)。当前仍**不宜直接接入生产自动生成跨期表**：各公司标签与维度可能不同；同名指标在不同财年的披露粒度会变化；事实锚点虽能精确指向 SEC HTML，还没有自动映射到系统已保存的正文字符引用。苹果与特斯拉的后续盲测及具体反例见 [独立核对表](XBRL-APPLE-TESLA-BLIND-2026-09-23.md)。
