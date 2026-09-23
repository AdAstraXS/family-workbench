"""微软 FY2024–FY2026 XBRL 核对试验；不接入 Django 或生产数据。

运行：RESEARCH_SEC_USER_AGENT=<公司与联系邮箱> python xbrl_msft_reconcile.py
依赖：edgartools==5.58.0。只读取 SEC 公开文件；不使用模型。
"""
import os
from decimal import Decimal

from edgar import get_by_accession_number, set_identity


ACCESSIONS = {
    2024: "0000950170-24-087843",
    2025: "0000950170-25-100235",
    2026: "0001193125-26-323660",
}
HUNDRED_MILLION = Decimal("100000000")
EXPECTED = {
    "operating_cash": ("1185.48", "1361.62", "1829.35"),
    "ppe_cash": ("444.77", "645.51", "1159.48"),
    "simple_fcf": ("740.71", "716.11", "669.87"),
    "depreciation": ("152", "220", "343"),
    "finance_rou_add": ("116.33", "205.11", "246.08"),
    "finance_principal_cash": ("12.86", "22.83", "31.01"),
    "finance_rou_net": ("258.62", "440.15", "672.81"),
    "finance_liability": ("271.45", "461.72", "665.94"),
    "uncommenced_leases": ("1170", "927", "3291"),
    "microsoft_cloud": ("1377", "1689", "2144"),
}
SPECS = {
    "operating_cash": ("us-gaap:NetCashProvidedByUsedInOperatingActivities", None, None, "duration"),
    "ppe_cash": ("us-gaap:PaymentsToAcquirePropertyPlantAndEquipment", None, None, "duration"),
    "depreciation": ("us-gaap:Depreciation", None, None, "duration"),
    "finance_rou_add": ("us-gaap:RightOfUseAssetObtainedInExchangeForFinanceLeaseLiability", None, None, "duration"),
    "finance_principal_cash": ("us-gaap:FinanceLeasePrincipalPayments", None, None, "duration"),
    "finance_rou_net": ("us-gaap:PropertyPlantAndEquipmentNet", "us-gaap:LeaseContractualTermAxis", "msft:FinanceLeaseMember", "instant"),
    "finance_liability": ("us-gaap:FinanceLeaseLiability", None, None, "instant"),
    "microsoft_cloud": ("us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "msft:ProductsOrServicesSecondaryCategorizationAxis", "msft:MicrosoftCloudMember", "duration"),
}
UNCOMMENCED = "us-gaap:UnrecordedUnconditionalPurchaseObligationBalanceSheetAmount"
LEASE_AXIS = "us-gaap:LeaseContractualTermAxis"


def _fact(frame, concept, year, axis, member, period_type):
    rows = frame[frame["concept"] == concept]
    if axis:
        rows = rows[(rows["dimension"] == axis) & (rows["member"] == member)]
    else:
        rows = rows[rows["is_dimensioned"] == False]
    if period_type == "duration":
        rows = rows[(rows["period_start"].astype(str) == f"{year-1}-07-01") &
                    (rows["period_end"].astype(str) == f"{year}-06-30")]
    else:
        rows = rows[rows["period_instant"].astype(str) == f"{year}-06-30"]
    rows = rows[rows["currency"] == "USD"]
    if len(rows) != 1:
        raise ValueError(f"{concept} FY{year} {member or 'undimensioned'}: {len(rows)} facts")
    row = rows.iloc[0]
    return Decimal(str(row["value"])) / HUNDRED_MILLION, row["fact_id"]


def main():
    identity = os.environ.get("RESEARCH_SEC_USER_AGENT", "").strip()
    if not identity:
        raise ValueError("请设置 RESEARCH_SEC_USER_AGENT 为公司名称与联系邮箱。")
    set_identity(identity)
    filings = {year: get_by_accession_number(accession)
               for year, accession in ACCESSIONS.items()}
    frames = {year: filing.xbrl().facts.to_dataframe()
              for year, filing in filings.items()}
    html = {year: filing.html() for year, filing in filings.items()}
    values = {}
    for metric, (concept, axis, member, period_type) in SPECS.items():
        values[metric] = []
        for year in (2024, 2025, 2026):
            source = 2025 if period_type == "instant" and year == 2024 else 2026
            amount, fact_id = _fact(frames[source], concept, year, axis, member, period_type)
            values[metric].append((amount, [(source, fact_id)]))

    values["uncommenced_leases"] = []
    for year in (2024, 2025, 2026):
        operating = _fact(frames[year], UNCOMMENCED, year, LEASE_AXIS,
                          "msft:OperatingLeaseMember", "instant")
        finance = _fact(frames[year], UNCOMMENCED, year, LEASE_AXIS,
                        "msft:FinanceLeaseMember", "instant")
        # FY2024 附注分别披露两类租赁；FY2025/26 附注披露一个合计，
        # XBRL 在两个 member 下重复该合计，不能相加。
        if year == 2024:
            amount = operating[0] + finance[0]
        else:
            if operating[0] != finance[0]:
                raise ValueError(f"FY{year} 的租赁合计在两个 XBRL member 下不一致")
            amount = finance[0]
        values["uncommenced_leases"].append(
            (amount, [(year, operating[1]), (year, finance[1])])
        )

    values["simple_fcf"] = [
        (values["operating_cash"][index][0] - values["ppe_cash"][index][0],
         values["operating_cash"][index][1] + values["ppe_cash"][index][1])
        for index in range(3)
    ]
    for metric, yearly in values.items():
        for index, year in enumerate((2024, 2025, 2026)):
            amount, sources = yearly[index]
            if amount != Decimal(EXPECTED[metric][index]):
                raise ValueError(f"{metric} FY{year}: {amount} != {EXPECTED[metric][index]}")
            links = []
            for filing_year, fact_id in sources:
                if f'id="{fact_id}"' not in html[filing_year]:
                    raise ValueError(f"SEC 原文未找到 {fact_id}")
                links.append(f"{filings[filing_year].filing_url}#{fact_id}")
            print(f"{metric}\tFY{year}\t{amount}\t" + " | ".join(links))
    print("PASS: 10 个指标 × 3 个财年，金额、期间、来源与原文事实锚点均通过核对。")


if __name__ == "__main__":
    main()
