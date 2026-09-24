"""苹果/特斯拉 FY2025 10-K XBRL 盲测；只读，不接入生产。

RESEARCH_SEC_USER_AGENT="Company Name contact@example.com" python xbrl_blind_aapl_tsla.py
依赖 edgartools==5.58.0。逐项要求唯一事实、正确期间/单位/维度、原始 HTML 锚点。
"""
import os
from decimal import Decimal

from edgar import get_by_accession_number, set_identity


FILINGS = {
    "AAPL": ("0000320193-25-000079", "2024-09-29", "2025-09-27"),
    "TSLA": ("0001628280-26-003952", "2025-01-01", "2025-12-31"),
}
HUNDRED_MILLION = Decimal("100000000")
# metric: concept, period, dimension axis, member, expected 亿美元; None = 此项未披露，非零。
SPECS = {
    "operating_cash": ("NetCashProvidedByUsedInOperatingActivities", "duration", None, None,
                       {"AAPL": "1114.82", "TSLA": "147.47"}),
    "ppe_cash": ("PaymentsToAcquirePropertyPlantAndEquipment", "duration", None, None,
                 {"AAPL": "127.15", "TSLA": "85.27"}),
    "ppe_depreciation": ("Depreciation", "duration", None, None,
                         {"AAPL": "80", "TSLA": "50.30"}),
    "finance_rou_add": ("RightOfUseAssetObtainedInExchangeForFinanceLeaseLiability", "duration", None, None,
                        {"AAPL": None, "TSLA": "0"}),
    "finance_principal": ("FinanceLeasePrincipalPayments", "duration", None, None,
                          {"AAPL": None, "TSLA": "1.04"}),
    "finance_liability": ("FinanceLeaseLiability", "instant", None, None,
                          {"AAPL": "12.30", "TSLA": "2.23"}),
    "finance_rou_asset": ("FinanceLeaseRightOfUseAsset", "instant", None, None,
                          {"AAPL": "10.33", "TSLA": "2.48"}),
    "services": ("RevenueFromContractWithCustomerExcludingAssessedTax", "duration",
                 "srt:ProductOrServiceAxis", "us-gaap:ServiceMember", {"AAPL": "1091.58"}),
    "energy_revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax", "duration",
                       "srt:ProductOrServiceAxis", "tsla:EnergyGenerationAndStorageMember",
                       {"TSLA": "127.71"}),
    "uncommenced_lease": ("UnrecordedUnconditionalPurchaseObligationBalanceSheetAmount", "instant",
                          "us-gaap:UnrecordedUnconditionalPurchaseObligationByCategoryOfItemPurchasedAxis",
                          "us-gaap:OperatingLeaseLeaseNotYetCommencedMember",
                          {"AAPL": "5.23", "TSLA": None}),
}


def select(frame, concept, kind, axis, member, start, end):
    rows = frame[frame["concept"] == f"us-gaap:{concept}"]
    if axis:
        rows = rows[(rows["dimension"] == axis) & (rows["member"] == member)]
    else:
        rows = rows[rows["is_dimensioned"] == False]
    if kind == "duration":
        rows = rows[(rows["period_start"].astype(str) == start) &
                    (rows["period_end"].astype(str) == end)]
    else:
        rows = rows[rows["period_instant"].astype(str) == end]
    return rows[rows["currency"] == "USD"]


def main():
    identity = os.environ.get("RESEARCH_SEC_USER_AGENT", "").strip()
    if not identity:
        raise ValueError("请设置 RESEARCH_SEC_USER_AGENT 为公司名称与联系邮箱。")
    set_identity(identity)
    for ticker, (accession, start, end) in FILINGS.items():
        filing = get_by_accession_number(accession)
        frame = filing.xbrl().facts.to_dataframe()
        html = filing.html()
        print(ticker, filing.filing_url)
        for metric, (concept, kind, axis, member, expected) in SPECS.items():
            if ticker not in expected:
                continue
            rows = select(frame, concept, kind, axis, member, start, end)
            target = expected[ticker]
            if target is None:
                if len(rows):
                    raise AssertionError(f"{ticker} {metric}: expected absent, got {len(rows)}")
                print(f"  {metric}: 未披露（不能当作 0）")
                continue
            if len(rows) != 1:
                raise AssertionError(f"{ticker} {metric}: expected 1 fact, got {len(rows)}")
            row = rows.iloc[0]
            amount = Decimal(str(row["value"])) / HUNDRED_MILLION
            if amount != Decimal(target):
                raise AssertionError(f"{ticker} {metric}: {amount} != {target}")
            fact_id = str(row["fact_id"])
            if f'id="{fact_id}"' not in html and f"id='{fact_id}'" not in html:
                raise AssertionError(f"{ticker} {metric}: missing HTML anchor {fact_id}")
            print(f"  {metric}: {amount} 亿美元, {filing.filing_url}#{fact_id}")


if __name__ == "__main__":
    main()
