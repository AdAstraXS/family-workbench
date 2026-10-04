from urllib.parse import parse_qs, urlsplit

from django.template import Context, Template
from django.test import RequestFactory, SimpleTestCase

from family_core.templatetags.table_navigation import table_page


class TableNavigationTests(SimpleTestCase):
    def test_pages_preserve_all_filters_and_other_tables(self):
        request = RequestFactory().get("/records/", {
            "q": "中文公司", "member": "7", "tag": ["甲", "乙"],
            "table_page_expenses": "2", "table_page_income": "3",
        })
        records = list(range(95))
        result = table_page({"request": request}, records, "expenses")
        self.assertEqual(list(result["page"]), list(range(30, 60)))
        self.assertEqual(records, list(range(95)))
        query = parse_qs(urlsplit(result["next_url"]).query)
        self.assertEqual(query["q"], ["中文公司"])
        self.assertEqual(query["member"], ["7"])
        self.assertEqual(query["tag"], ["甲", "乙"])
        self.assertEqual(query["table_page_income"], ["3"])
        self.assertEqual(query["table_page_expenses"], ["3"])
        self.assertEqual(urlsplit(result["next_url"]).fragment, "table-expenses")

    def test_empty_and_stale_page_numbers_remain_usable(self):
        for rows, number, expected in [(range(65), "999", [60,61,62,63,64]), ([], "2", []), (range(5), "bad", list(range(5)))]:
            request = RequestFactory().get("/", {"table_page_rows": number})
            result = table_page({"request": request}, rows, "rows")
            self.assertEqual(list(result["page"]), expected)

    def test_template_paginates_display_without_replacing_total_input(self):
        request = RequestFactory().get("/", {"table_page_rows": 2, "currency": "HKD"})
        output = Template('{% load table_navigation %}{% table_page rows "rows" as t %}'
                          '{{ rows|length }} / {{ t.page|length }} / {{ total }}'
                          '{% include "workspace/table_pagination.html" with table=t %}').render(
                              Context({"request": request, "rows": list(range(65)), "total": "123.45"}))
        self.assertIn("65 / 30 / 123.45", output)
        self.assertIn("currency=HKD", output)
        self.assertIn("每页 30 条", output)
