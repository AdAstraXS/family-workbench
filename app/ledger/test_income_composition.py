from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from family_core.models import Family, FamilyMember
from .models import ExpenseRecord, IncomeCategory, IncomeRecord
from .views import build_income_category_pie_data


class IncomeCompositionTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name="我的家庭")
        self.user = get_user_model().objects.create_user(username="income-chart")
        self.member = FamilyMember.objects.create(family=self.family, user=self.user, display_name="我")
        self.primary = IncomeCategory.objects.create(family=self.family, name="工资")
        self.secondary = IncomeCategory.objects.create(family=self.family, name="奖金", parent=self.primary)
        self.tertiary = IncomeCategory.objects.create(family=self.family, name="年终奖", parent=self.secondary)
        self.client.force_login(self.user)

    def record(self, amount, category=None, **kwargs):
        return IncomeRecord.objects.create(family=self.family, member=self.member,
            income_date=date(2026, 6, 10), amount=Decimal(amount), category=category, **kwargs)

    def test_three_levels_reconcile_direct_nested_and_uncategorized_income(self):
        self.record("100.25", self.primary)
        self.record("200.50", self.secondary)
        self.record("300.75", self.tertiary)
        self.record("50")
        data = build_income_category_pie_data(2026)
        for level in ("primary", "secondary", "tertiary"):
            self.assertEqual(sum(item["value"] for item in data[level]), 651.5)
        item = next(item for item in data["tertiary"] if item["id"] == self.tertiary.pk)
        self.assertEqual(item["parent_id"], self.secondary.pk)
        self.assertEqual(item["primary_id"], self.primary.pk)

    def test_period_filter_and_family_isolation(self):
        self.record("100", self.primary)
        self.record("200", self.primary, period_start=date(2026, 5, 1))
        self.record("300", self.primary, period_start=date(2025, 5, 1))
        other = Family.objects.create(name="其他家庭")
        member = FamilyMember.objects.create(family=other, display_name="其他")
        IncomeRecord.objects.create(family=other, member=member, income_date=date(2026, 6, 1), amount=999)
        data = build_income_category_pie_data(2026, 6)
        self.assertEqual(sum(item["value"] for item in data["primary"]), 100)
        annual = build_income_category_pie_data(2026)
        self.assertEqual(sum(item["value"] for item in annual["primary"]), 300)

    def test_page_order_independent_months_empty_state_and_no_writes(self):
        self.record("100", self.primary)
        response = self.client.get(reverse("ledger:cashflow_summary_year", args=[2026]),
            {"category_month": "5", "income_category_month": "6"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_category_month"], 5)
        self.assertEqual(response.context["selected_income_category_month"], 6)
        html = response.content.decode()
        self.assertLess(html.index('id="monthly-expense-category-pies"'), html.index('id="monthly-income-category-pies"'))
        self.assertContains(response, 'id="tertiary-income-pie"')
        self.assertEqual(IncomeRecord.objects.count(), 1)
        empty = self.client.get(reverse("ledger:cashflow_summary_year", args=[2026]), {"income_category_month": "12"})
        self.assertEqual(empty.context["income_category_pie_data"]["primary"], [])
        invalid = self.client.get(reverse("ledger:cashflow_summary_year", args=[2026]), {"income_category_month": "13"})
        self.assertEqual(invalid.context["selected_income_category_month"], "all")

    def test_member_filters_are_independent_and_family_totals_reconcile(self):
        other = FamilyMember.objects.create(family=self.family, display_name="孙秘书")
        self.record("100", self.primary)
        IncomeRecord.objects.create(family=self.family, member=other, income_date=date(2026, 6, 10), amount=200)
        for member, amount in [(self.member, 30), (other, 70)]:
            ExpenseRecord.objects.create(family=self.family, member=member, expense_date=date(2026, 6, 10), amount=amount)
        url = reverse("ledger:cashflow_summary_year", args=[2026])
        response = self.client.get(url, {"category_member": other.pk, "income_category_member": self.member.pk,
            "category_month": "6", "income_category_month": "6"})
        self.assertEqual(sum(item["value"] for item in response.context["expense_category_pie_data"]["primary"]), 70)
        self.assertEqual(sum(item["value"] for item in response.context["income_category_pie_data"]["primary"]), 100)
        self.assertContains(response, f'name="category_member" value="{other.pk}"')
        self.assertContains(response, f'name="income_category_member" value="{self.member.pk}"')
        self.assertContains(response, "孙秘书支出分类占比")
        annual = self.client.get(url)
        self.assertEqual(sum(item["value"] for item in annual.context["expense_category_pie_data"]["primary"]), 100)
        self.assertEqual(sum(item["value"] for item in annual.context["income_category_pie_data"]["primary"]), 300)

    def test_foreign_and_invalid_member_ids_are_not_selectable(self):
        self.record("100", self.primary)
        family = Family.objects.create(name="其他家庭")
        foreign = FamilyMember.objects.create(family=family, display_name="不应出现")
        IncomeRecord.objects.create(family=family, member=foreign, income_date=date(2026, 6, 10), amount=999)
        url = reverse("ledger:cashflow_summary_year", args=[2026])
        for value in [foreign.pk, "invalid", -1]:
            response = self.client.get(url, {"category_member": value, "income_category_member": value})
            self.assertEqual(response.context["selected_category_member"], "all")
            self.assertEqual(response.context["selected_income_category_member"], "all")
            self.assertNotContains(response, "不应出现")
            self.assertEqual(sum(item["value"] for item in response.context["income_category_pie_data"]["primary"]), 100)

    def test_expense_window_keeps_all_records_in_month_and_year_details(self):
        ExpenseRecord.objects.bulk_create([
            ExpenseRecord(family=self.family, member=self.member, expense_date=date(2026, 6, 10), amount=10)
            for _ in range(18)
        ])
        for name, args in [("ledger:expense_month_detail", [2026, 6]), ("ledger:expense_year_detail", [2026])]:
            response = self.client.get(reverse(name, args=args))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "record-table-wrap expense-record-window", count=1)
            self.assertContains(response, 'class="expense-record-row"', count=18)
            self.assertContains(response, "rows[14].getBoundingClientRect().bottom")
            self.assertContains(response, "家庭支出合计")
