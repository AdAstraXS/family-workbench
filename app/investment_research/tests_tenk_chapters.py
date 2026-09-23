"""10-K 章节索引：目录排除、字符坐标与区段覆盖。"""
from types import SimpleNamespace

from django.test import SimpleTestCase

from .tenk_chapters import tenk_chapter_coverage
from .tenk_financial_index import tenk_item8_index


def _version(text, kind="10-k"):
    return SimpleNamespace(content_text=text, document=SimpleNamespace(document_type=kind))


class TenKChapterTests(SimpleTestCase):
    def test_dense_table_of_contents_is_not_mistaken_for_body(self):
        toc = "\n".join([
            "Item 1. Business", "Item 1A. Risk Factors", "Item 2. Properties",
            "Item 3. Legal Proceedings", "Item 7. Management's Discussion",
            "Item 8. Financial Statements",
        ])
        body = ("\nITEM 1. BUSINESS\n" + "business " * 400 +
                "\nITEM 1A. RISK FACTORS\n" + "risk " * 400 +
                "\nITEM 7. MANAGEMENT'S DISCUSSION AND ANALYSIS\n" + "analysis " * 400 +
                "\nITEM 8. FINANCIAL STATEMENTS AND SUPPLEMENTARY DATA\n" + "finance " * 400)
        text = "INDEX\n" + toc + "\n" + "preface " * 300 + body
        chapters = [item for item in tenk_chapter_coverage(_version(text)) if item["located"]]
        self.assertEqual([item["code"] for item in chapters], ["1", "1A", "7", "8"])
        self.assertEqual(chapters[0]["start"], text.index("ITEM 1. BUSINESS"))
        self.assertEqual(chapters[0]["end"], text.index("ITEM 1A. RISK FACTORS"))

    def test_coverage_counts_intersecting_segments_without_claiming_verification(self):
        text = ("ITEM 1. BUSINESS\n" + "a" * 17000 +
                "\nITEM 1A. RISK FACTORS\n" + "b" * 2000 +
                "\nITEM 7. MANAGEMENT'S DISCUSSION AND ANALYSIS\n" + "c" * 2000)
        chapters = [item for item in tenk_chapter_coverage(_version(text), {0}) if item["located"]]
        self.assertEqual((chapters[0]["covered_count"], chapters[0]["segment_count"]), (1, 2))
        self.assertEqual((chapters[1]["covered_count"], chapters[1]["segment_count"]), (0, 1))
        self.assertEqual((chapters[2]["covered_count"], chapters[2]["segment_count"]), (0, 1))

    def test_unrecognized_titles_and_other_filings_fail_closed(self):
        text = "Item 7. See another document\n" + "x" * 1000
        self.assertEqual(tenk_chapter_coverage(_version(text)), [])
        self.assertEqual(tenk_chapter_coverage(_version("ITEM 7. MANAGEMENT'S DISCUSSION", "10-q")), [])

    def test_missing_standard_item_is_visible_as_unlocated(self):
        text = "ITEM 1. BUSINESS\nAbout this company.\nITEM 7. MANAGEMENT'S DISCUSSION\nResults."
        chapters = tenk_chapter_coverage(_version(text))
        missing = next(item for item in chapters if item["code"] == "1A")
        self.assertFalse(missing["located"])
        self.assertNotIn("covered_count", missing)

    def test_item8_financial_index_handles_three_filing_title_styles(self):
        variants = (
            ("INCOME STATEMENTS", "CASH FLOWS STATEMENTS", "STOCKHOLDERS’ EQUITY STATEMENTS",
             "NOTES TO FINANCIAL STATEMENTS", "NOTE 1 — ACCOUNTING POLICIES", "NOTE 2 — LEASES"),
            ("CONSOLIDATED STATEMENTS OF OPERATIONS", "CONSOLIDATED STATEMENTS OF CASH FLOWS",
             "CONSOLIDATED STATEMENTS OF SHAREHOLDERS’ EQUITY",
             "Notes to Consolidated Financial Statements", "Note 1 – Summary of Significant Accounting Policies",
             "Note 2 – Revenue"),
            ("Consolidated Statements of Operations", "Consolidated Statements of Cash Flows",
             "Consolidated Statements of Redeemable Noncontrolling Interests and Equity",
             "Notes to Consolidated Financial Statements", "Note 1 – Overview", "Note 2 – Leases"),
        )
        for income, cash, equity, notes_title, first, second in variants:
            with self.subTest(first=first):
                text = ("ITEM 7. MANAGEMENT'S DISCUSSION AND ANALYSIS\n"
                        "Note 1 – Not inside Item 8\n"
                        "ITEM 8. FINANCIAL STATEMENTS AND SUPPLEMENTARY DATA\n"
                        f"{income}\n{cash}\nStockholders’ equity\n{equity}\n"
                        "Note 1 – Not a heading before notes\n"
                        f"{notes_title}\n{first}\nSome discussion.\n{second}\n"
                        "ITEM 9. CHANGES IN AND DISAGREEMENTS WITH ACCOUNTANTS\n")
                entries = tenk_item8_index(_version(text))
                self.assertEqual([entry["kind"] for entry in entries],
                                 ["statement", "statement", "statement", "note", "note"])
                self.assertEqual([entry["title"] for entry in entries[-2:]], [first, second])
                self.assertEqual(entries[-1]["start"], text.index(second))
                self.assertEqual(text[entries[-1]["start"]:entries[-1]["quote_end"]], second)

    def test_item8_index_does_not_guess_missing_notes(self):
        text = ("ITEM 8. FINANCIAL STATEMENTS AND SUPPLEMENTARY DATA\n"
                "BALANCE SHEETS\nNote 1 – Mention in prose\n")
        self.assertEqual([entry["title"] for entry in tenk_item8_index(_version(text))],
                         ["BALANCE SHEETS"])
