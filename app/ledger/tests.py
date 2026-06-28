from datetime import date, datetime
from decimal import Decimal
from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import Sum
from django.test import TestCase
from django.utils import timezone
from django.urls import reverse
from openpyxl import Workbook

from family_core.models import AccountRegion, AccountType, AssetCategory, Family, FamilyMember

from .asset_snapshot_import import (
    AssetSnapshotWorkbookError,
    import_asset_snapshot_workbook,
    latest_snapshot_default_signature,
)
from .admin import ExpenseCategoryAdminForm, IncomeCategoryAdminForm
from .expense_import import EXPECTED_HEADERS, ExpenseWorkbookError, import_expense_workbook
from .forms import ExpenseRecordForm
from .models import (
    AssetBalanceEntry,
    AssetBalanceSnapshot,
    BankAccount,
    ExpenseCategory,
    ExpenseImportBatch,
    ExpenseRecord,
    IncomeCategory,
    IncomeRecord,
)
from .views import build_cashflow_monthly_rows


class LedgerExpenseImportTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name="我的家庭")
        self.me = FamilyMember.objects.create(
            family=self.family,
            display_name="我",
            role=FamilyMember.ROLE_ADMIN,
        )
        self.secretary = FamilyMember.objects.create(
            family=self.family,
            display_name="孙秘书",
        )
        self.account_types = {
            name: AccountType.objects.create(family=self.family, name=name, display_order=index)
            for index, name in enumerate(("银行", "微信", "支付宝", "券商"), start=1)
        }
        self.accounts = {}
        for member in (self.me, self.secretary):
            for name in ("微信", "支付宝"):
                self.accounts[(member.display_name, name)] = BankAccount.objects.create(
                    family=self.family,
                    member=member,
                    account_name=name,
                    account_type_ref=self.account_types[name],
                )
        self.bank_account = BankAccount.objects.create(
            family=self.family,
            member=self.me,
            account_name="招商银行",
            account_type_ref=self.account_types["银行"],
        )
        self.broker_account = BankAccount.objects.create(
            family=self.family,
            member=self.me,
            account_name="测试证券",
            account_type_ref=self.account_types["券商"],
        )
        self.user = get_user_model().objects.create_user(username="tester", password="password")

    def workbook_upload(self, rows, filename="支出.xlsx", headers=EXPECTED_HEADERS):
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "月度支出"
        worksheet.append(list(headers))
        for row in rows:
            worksheet.append(row)
        stream = BytesIO()
        workbook.save(stream)
        workbook.close()
        return SimpleUploadedFile(
            filename,
            stream.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def sample_rows(self):
        return [
            [datetime(2026, 6, 1, 8, 30), "我", "微信", "经常性", "餐饮", "食堂", 100, "早餐"],
            [datetime(2026, 6, 2, 9, 0), "我", "微信", "经常性", "餐饮", "", -20, "退款"],
            [datetime(2026, 6, 3, 10, 15), "孙秘书", "支付宝", "固定资产", "家居家电", "小家电", 10, "插座"],
        ]

    def test_import_creates_records_accounts_and_three_level_categories(self):
        result = import_expense_workbook(
            family=self.family,
            uploaded_file=self.workbook_upload(self.sample_rows()),
            imported_by=self.user,
        )

        self.assertFalse(result.duplicate_file)
        self.assertEqual(result.batch.row_count, 3)
        self.assertEqual(result.batch.imported_count, 3)
        self.assertEqual(result.batch.total_amount, Decimal("90"))
        self.assertEqual(ExpenseRecord.objects.count(), 3)
        refund = ExpenseRecord.objects.get(remark="退款")
        self.assertEqual(refund.amount, Decimal("-20"))
        self.assertEqual(refund.bank_account, self.accounts[("我", "微信")])
        self.assertEqual(str(refund.category), "经常性-餐饮")
        appliance = ExpenseRecord.objects.get(remark="插座")
        self.assertEqual(str(appliance.category), "固定资产-家居家电-小家电")
        self.assertEqual(timezone.localtime(appliance.occurred_at).hour, 10)

    def test_duplicate_file_and_overlapping_rows_are_not_reimported(self):
        first_upload = self.workbook_upload(self.sample_rows())
        first_bytes = first_upload.read()
        first_upload.seek(0)
        first = import_expense_workbook(family=self.family, uploaded_file=first_upload)
        duplicate = import_expense_workbook(
            family=self.family,
            uploaded_file=SimpleUploadedFile("改名.xlsx", first_bytes),
        )
        self.assertTrue(duplicate.duplicate_file)
        self.assertEqual(duplicate.batch, first.batch)
        self.assertEqual(ExpenseRecord.objects.count(), 3)

        overlap_rows = [
            self.sample_rows()[0],
            [datetime(2026, 6, 4, 12, 0), "我", "支付宝", "经常性", "交通", "", 30, "公交"],
        ]
        overlap = import_expense_workbook(
            family=self.family,
            uploaded_file=self.workbook_upload(overlap_rows, filename="次月.xlsx"),
        )
        self.assertEqual(overlap.batch.imported_count, 1)
        self.assertEqual(overlap.batch.skipped_count, 1)
        self.assertEqual(ExpenseRecord.objects.count(), 4)
        self.assertEqual(ExpenseImportBatch.objects.count(), 2)

    def test_invalid_headers_are_rejected_without_database_changes(self):
        invalid_headers = ("错误时间",) + EXPECTED_HEADERS[1:]
        with self.assertRaisesMessage(ExpenseWorkbookError, "首行表头不符合固定格式"):
            import_expense_workbook(
                family=self.family,
                uploaded_file=self.workbook_upload(self.sample_rows(), headers=invalid_headers),
            )
        self.assertFalse(ExpenseRecord.objects.exists())
        self.assertFalse(ExpenseImportBatch.objects.exists())

    def test_manual_form_uses_linked_category_dropdowns_and_allowed_accounts(self):
        primary = ExpenseCategory.objects.create(family=self.family, name="经常性")
        secondary = ExpenseCategory.objects.create(family=self.family, name="餐饮", parent=primary)
        tertiary = ExpenseCategory.objects.create(family=self.family, name="食堂", parent=secondary)
        form = ExpenseRecordForm(
            data={
                "family": self.family.pk,
                "member": self.me.pk,
                "bank_account": self.accounts[("我", "微信")].pk,
                "primary_category": primary.pk,
                "secondary_category": secondary.pk,
                "tertiary_category": tertiary.pk,
                "expense_date": "2026-06-27",
                "amount": "25.50",
                "currency": "CNY",
                "remark": "午餐",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertNotIn(self.broker_account, form.fields["bank_account"].queryset)
        rendered_accounts = str(form["bank_account"])
        self.assertIn("data-member-id", rendered_accounts)
        self.assertIn("我 - 微信", rendered_accounts)
        self.assertNotIn("测试证券", rendered_accounts)
        record = form.save()
        self.assertEqual(record.category, tertiary)
        self.assertEqual(record.bank_account, self.accounts[("我", "微信")])
        self.assertEqual(record.period_start.isoformat(), "2026-06-27")
        self.client.force_login(self.user)
        response = self.client.get(reverse("ledger:expense_year_detail", args=[2026]))
        self.assertContains(response, "<th>支出账户</th>", html=True)
        self.assertContains(response, "<td>微信</td>", html=True)

    def test_manual_form_rejects_account_owned_by_another_member(self):
        primary = ExpenseCategory.objects.create(family=self.family, name="经常性")
        secondary = ExpenseCategory.objects.create(family=self.family, name="餐饮", parent=primary)
        form = ExpenseRecordForm(
            data={
                "family": self.family.pk,
                "member": self.me.pk,
                "bank_account": self.accounts[("孙秘书", "微信")].pk,
                "primary_category": primary.pk,
                "secondary_category": secondary.pk,
                "tertiary_category": "",
                "expense_date": "2026-06-27",
                "amount": "5",
                "currency": "CNY",
                "remark": "",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("bank_account", form.errors)

    def test_monthly_cashflow_summary_includes_family_totals(self):
        record_date = date(2026, 6, 15)
        IncomeRecord.objects.create(
            family=self.family,
            member=self.me,
            income_date=record_date,
            period_start=record_date,
            period_end=record_date,
            amount=Decimal("100"),
        )
        ExpenseRecord.objects.create(
            family=self.family,
            member=self.me,
            bank_account=self.accounts[("我", "微信")],
            expense_date=record_date,
            period_start=record_date,
            period_end=record_date,
            amount=Decimal("30"),
        )
        ExpenseRecord.objects.create(
            family=self.family,
            member=self.secretary,
            bank_account=self.accounts[("孙秘书", "支付宝")],
            expense_date=record_date,
            period_start=record_date,
            period_end=record_date,
            amount=Decimal("20"),
        )

        _, sections = build_cashflow_monthly_rows(2026)
        june = next(row for row in sections[0]["rows"] if row["month"] == 6)
        self.assertEqual(june["income_total"], Decimal("100"))
        self.assertEqual(june["expense_total"], Decimal("50"))
        self.assertEqual(june["net_total"], Decimal("50"))
        self.assertEqual(sections[0]["total"]["expense_total"], Decimal("50"))

        self.client.force_login(self.user)
        response = self.client.get(reverse("ledger:cashflow_summary_year", args=[2026]))
        self.assertContains(response, "<th>家庭合计</th>", count=3, html=True)

    def test_monthly_summary_reuses_linked_expense_pies_with_month_filter(self):
        primary = ExpenseCategory.objects.create(family=self.family, name="经常性")
        secondary = ExpenseCategory.objects.create(
            family=self.family,
            name="餐饮",
            parent=primary,
        )
        tertiary = ExpenseCategory.objects.create(
            family=self.family,
            name="食堂",
            parent=secondary,
        )
        for record_date, amount in (
            (date(2026, 6, 20), Decimal("10000")),
            (date(2026, 7, 20), Decimal("20000")),
        ):
            ExpenseRecord.objects.create(
                family=self.family,
                member=self.me,
                bank_account=self.accounts[("我", "微信")],
                category=tertiary,
                expense_date=record_date,
                period_start=record_date,
                period_end=record_date,
                amount=amount,
            )
        self.client.force_login(self.user)

        june_response = self.client.get(
            reverse("ledger:cashflow_summary_year", args=[2026]),
            {"category_month": "6"},
        )

        self.assertEqual(june_response.status_code, 200)
        self.assertEqual(june_response.context["selected_category_month"], 6)
        june_pies = june_response.context["expense_category_pie_data"]
        self.assertEqual(june_pies["unit"], "元")
        self.assertEqual(sum(item["value"] for item in june_pies["primary"]), 10000.0)
        self.assertEqual(sum(item["value"] for item in june_pies["secondary"]), 10000.0)
        self.assertEqual(sum(item["value"] for item in june_pies["tertiary"]), 10000.0)
        self.assertContains(june_response, 'id="monthly-expense-category-pies"')
        self.assertContains(june_response, 'name="category_month"')
        self.assertContains(june_response, 'value="12"')
        self.assertContains(june_response, "js/expense_category_pies.js")
        self.assertContains(
            june_response,
            'action="/ledger/expenses/2026/summary/#monthly-expense-category-pies"',
        )
        rendered_html = june_response.content.decode()
        self.assertLess(
            rendered_html.index("</table>"),
            rendered_html.index('id="monthly-expense-category-pies"'),
        )

        annual_response = self.client.get(
            reverse("ledger:cashflow_summary_year", args=[2026]),
            {"category_month": "all"},
        )
        annual_pies = annual_response.context["expense_category_pie_data"]
        self.assertEqual(annual_pies["unit"], "元")
        self.assertEqual(sum(item["value"] for item in annual_pies["primary"]), 30000.0)
        self.assertEqual(sum(item["value"] for item in annual_pies["secondary"]), 30000.0)
        self.assertEqual(sum(item["value"] for item in annual_pies["tertiary"]), 30000.0)

    def test_expense_index_cashflow_chart_supports_single_year_and_all_years(self):
        recurring = ExpenseCategory.objects.create(family=self.family, name="经常性")
        dining = ExpenseCategory.objects.create(
            family=self.family,
            name="餐饮",
            parent=recurring,
        )
        cafeteria = ExpenseCategory.objects.create(
            family=self.family,
            name="食堂",
            parent=dining,
        )
        fixed_asset = ExpenseCategory.objects.create(family=self.family, name="固定资产")
        home_appliance = ExpenseCategory.objects.create(
            family=self.family,
            name="家居家电",
            parent=fixed_asset,
        )
        small_appliance = ExpenseCategory.objects.create(
            family=self.family,
            name="小家电",
            parent=home_appliance,
        )
        for record_date, income_amount, expense_amount, category in (
            (date(2025, 12, 15), Decimal("20000"), Decimal("5000"), cafeteria),
            (date(2026, 1, 15), Decimal("10000"), Decimal("12000"), cafeteria),
            (date(2026, 6, 15), Decimal("30000"), Decimal("5000"), small_appliance),
        ):
            IncomeRecord.objects.create(
                family=self.family,
                member=self.me,
                income_date=record_date,
                period_start=record_date,
                period_end=record_date,
                amount=income_amount,
            )
            ExpenseRecord.objects.create(
                family=self.family,
                member=self.me,
                bank_account=self.accounts[("我", "微信")],
                category=category,
                expense_date=record_date,
                period_start=record_date,
                period_end=record_date,
                amount=expense_amount,
            )
        self.client.force_login(self.user)

        yearly_response = self.client.get(
            reverse("ledger:expense_list"),
            {"trend_year": "2026", "category_year": "2026"},
        )
        self.assertEqual(yearly_response.status_code, 200)
        yearly_trend = yearly_response.context["cashflow_trend_data"]
        self.assertEqual(yearly_trend["mode"], "monthly")
        self.assertEqual(yearly_trend["labels"], [f"{month}月" for month in range(1, 13)])
        self.assertEqual(yearly_trend["income"][0], 1.0)
        self.assertEqual(yearly_trend["expense"][0], 1.2)
        self.assertEqual(yearly_trend["net"][0], -0.2)
        self.assertEqual(yearly_trend["income"][5], 3.0)
        self.assertContains(yearly_response, 'id="cashflow-trend-chart"')
        self.assertContains(yearly_response, 'value="all"')
        self.assertContains(yearly_response, "js/cashflow_trend.js")
        self.assertContains(yearly_response, "js/expense_category_pies.js")
        self.assertContains(yearly_response, 'id="primary-expense-pie"')
        category_pies = yearly_response.context["expense_category_pie_data"]
        primary_values = {
            item["name"]: item["value"]
            for item in category_pies["primary"]
        }
        self.assertEqual(primary_values, {"经常性": 1.2, "固定资产": 0.5})
        recurring_secondary = next(
            item for item in category_pies["secondary"] if item["name"] == "餐饮"
        )
        self.assertEqual(recurring_secondary["parent_id"], recurring.pk)
        recurring_tertiary = next(
            item for item in category_pies["tertiary"] if item["name"] == "食堂"
        )
        self.assertEqual(recurring_tertiary["parent_id"], dining.pk)
        self.assertEqual(recurring_tertiary["primary_id"], recurring.pk)

        all_response = self.client.get(
            reverse("ledger:expense_list"),
            {"trend_year": "all", "category_year": "all"},
        )
        all_trend = all_response.context["cashflow_trend_data"]
        self.assertEqual(all_trend["mode"], "yearly")
        self.assertEqual(all_trend["labels"], ["2025", "2026"])
        self.assertEqual(all_trend["income"], [2.0, 4.0])
        self.assertEqual(all_trend["expense"], [0.5, 1.7])
        self.assertEqual(all_trend["net"], [1.5, 2.3])
        all_primary_values = {
            item["name"]: item["value"]
            for item in all_response.context["expense_category_pie_data"]["primary"]
        }
        self.assertEqual(all_primary_values, {"经常性": 1.7, "固定资产": 0.5})

    def test_expense_pie_levels_reconcile_direct_and_nested_classifications(self):
        primary = ExpenseCategory.objects.create(family=self.family, name="经常性")
        secondary = ExpenseCategory.objects.create(
            family=self.family,
            name="餐饮",
            parent=primary,
        )
        tertiary = ExpenseCategory.objects.create(
            family=self.family,
            name="食堂",
            parent=secondary,
        )
        record_date = date(2026, 6, 20)
        for category, amount in (
            (primary, Decimal("10000")),
            (secondary, Decimal("20000")),
            (tertiary, Decimal("30000")),
        ):
            ExpenseRecord.objects.create(
                family=self.family,
                member=self.me,
                bank_account=self.accounts[("我", "微信")],
                category=category,
                expense_date=record_date,
                period_start=record_date,
                period_end=record_date,
                amount=amount,
            )
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("ledger:expense_list"),
            {"category_year": "2026"},
        )

        self.assertEqual(response.status_code, 200)
        pie_data = response.context["expense_category_pie_data"]
        primary_total = sum(item["value"] for item in pie_data["primary"])
        secondary_total = sum(item["value"] for item in pie_data["secondary"])
        tertiary_total = sum(item["value"] for item in pie_data["tertiary"])
        self.assertEqual(primary_total, 6.0)
        self.assertEqual(secondary_total, primary_total)
        self.assertEqual(tertiary_total, primary_total)

        primary_value = next(
            item["value"] for item in pie_data["primary"] if item["id"] == primary.pk
        )
        primary_secondary_items = [
            item for item in pie_data["secondary"] if item["parent_id"] == primary.pk
        ]
        primary_tertiary_items = [
            item for item in pie_data["tertiary"] if item["primary_id"] == primary.pk
        ]
        self.assertEqual(sum(item["value"] for item in primary_secondary_items), primary_value)
        self.assertEqual(sum(item["value"] for item in primary_tertiary_items), primary_value)

        real_secondary_value = next(
            item["value"] for item in primary_secondary_items if item["id"] == secondary.pk
        )
        secondary_tertiary_total = sum(
            item["value"]
            for item in primary_tertiary_items
            if item["parent_id"] == secondary.pk
        )
        self.assertEqual(secondary_tertiary_total, real_secondary_value)
        self.assertTrue(
            any(item["name"] == "未细分至二级" for item in primary_secondary_items)
        )
        self.assertTrue(
            any(item["name"] == "未细分至三级" for item in primary_tertiary_items)
        )
        self.assertContains(
            response,
            'action="/ledger/expenses/#expense-category-pies"',
        )

    def test_month_expense_filters_recalculate_member_and_family_totals(self):
        primary = ExpenseCategory.objects.create(family=self.family, name="经常性")
        secondary = ExpenseCategory.objects.create(family=self.family, name="餐饮", parent=primary)
        cafeteria = ExpenseCategory.objects.create(family=self.family, name="食堂", parent=secondary)
        takeaway = ExpenseCategory.objects.create(family=self.family, name="外卖", parent=secondary)
        record_date = date(2026, 6, 20)
        ExpenseRecord.objects.create(
            family=self.family,
            member=self.me,
            bank_account=self.accounts[("我", "微信")],
            category=cafeteria,
            expense_date=record_date,
            period_start=record_date,
            period_end=record_date,
            amount=Decimal("25.50"),
            remark="筛选目标",
        )
        ExpenseRecord.objects.create(
            family=self.family,
            member=self.me,
            bank_account=self.accounts[("我", "支付宝")],
            category=takeaway,
            expense_date=record_date,
            period_start=record_date,
            period_end=record_date,
            amount=Decimal("30"),
            remark="同成员不同账户",
        )
        ExpenseRecord.objects.create(
            family=self.family,
            member=self.secretary,
            bank_account=self.accounts[("孙秘书", "支付宝")],
            category=cafeteria,
            expense_date=record_date,
            period_start=record_date,
            period_end=record_date,
            amount=Decimal("40"),
            remark="不同成员",
        )

        self.client.force_login(self.user)
        response = self.client.get(
            reverse("ledger:expense_month_detail", args=[2026, 6]),
            {
                "member": self.me.pk,
                "bank_account": self.accounts[("我", "微信")].pk,
                "primary_category": primary.pk,
                "secondary_category": secondary.pk,
                "tertiary_category": cafeteria.pk,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["expense_rows"]), 1)
        self.assertEqual(response.context["expense_rows"][0]["remark"], "筛选目标")
        totals = {
            row["member"].display_name: row["amount"]
            for row in response.context["expense_member_totals"]
        }
        self.assertEqual(totals["我"], Decimal("25.50"))
        self.assertEqual(totals["孙秘书"], Decimal("0"))
        self.assertEqual(response.context["expense_family_total"], Decimal("25.50"))
        self.assertContains(response, "已按条件筛选，合计已重新计算")
        self.assertContains(response, 'aria-label="按三级分类筛选"')

        year_response = self.client.get(
            reverse("ledger:expense_year_detail", args=[2026]),
            {
                "member": self.me.pk,
                "bank_account": self.accounts[("我", "微信")].pk,
                "primary_category": primary.pk,
                "secondary_category": secondary.pk,
                "tertiary_category": cafeteria.pk,
            },
        )
        self.assertEqual(year_response.status_code, 200)
        self.assertEqual(len(year_response.context["expense_rows"]), 1)
        self.assertEqual(year_response.context["expense_family_total"], Decimal("25.50"))
        self.assertContains(year_response, 'aria-label="按成员筛选"')
        self.assertContains(year_response, "已按条件筛选，合计已重新计算")


class AssetSnapshotImportTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name="我的家庭")
        self.me = FamilyMember.objects.create(
            family=self.family,
            display_name="我",
            role=FamilyMember.ROLE_ADMIN,
        )
        self.secretary = FamilyMember.objects.create(
            family=self.family,
            display_name="孙秘书",
        )
        self.bank_type = AccountType.objects.create(family=self.family, name="银行")
        self.broker_type = AccountType.objects.create(family=self.family, name="券商")
        self.domestic = AccountRegion.objects.create(family=self.family, name="境内")
        self.overseas = AccountRegion.objects.create(family=self.family, name="境外")
        self.cash = AssetCategory.objects.create(family=self.family, name="现金")
        self.fund = AssetCategory.objects.create(family=self.family, name="股指基金")
        self.current_account = BankAccount.objects.create(
            family=self.family,
            member=self.me,
            account_name="当前账户",
            account_type_ref=self.bank_type,
            account_region=self.domestic,
        )
        self.latest = AssetBalanceSnapshot.objects.create(
            family=self.family,
            snapshot_date=date(2026, 5, 31),
            usd_to_base=Decimal("6.78730000"),
            hkd_to_base=Decimal("0.86560000"),
        )
        AssetBalanceEntry.objects.create(
            snapshot=self.latest,
            member=self.me,
            account=self.current_account,
            account_name=self.current_account.account_name,
            asset_category=self.cash,
            currency="CNY",
            original_amount=Decimal("100"),
            base_amount=Decimal("100"),
            display_order=1,
            remark="保留此模板",
        )

    def workbook_source(self, existing_total=100):
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "账户余额NEW"
        worksheet.cell(1, 6, datetime(2024, 12, 31))
        worksheet.cell(2, 7, 7.3)
        worksheet.cell(2, 9, 0.94)
        worksheet.cell(3, 6, "我")
        worksheet.cell(3, 8, "孙秘书")
        worksheet.cell(1, 10, datetime(2026, 5, 31))
        worksheet.cell(2, 11, 6.7873)
        worksheet.cell(2, 13, 0.8656)
        worksheet.cell(3, 10, "我")
        worksheet.cell(3, 12, "孙秘书")
        for column, value in enumerate(
            ("账户名称", "账户地区", "账户类型", "资产类别", "币种"),
            start=1,
        ):
            worksheet.cell(4, column, value)
        historical_rows = (
            ("历史银行", "境内", "银行", "现金", "RMB", 50, 50, 0, 0),
            ("境外券商", "境外", "券商", "股指基金", "USD", 10, 73, 0, 0),
        )
        for row_number, row in enumerate(historical_rows, start=5):
            for column, value in enumerate(row, start=1):
                worksheet.cell(row_number, column, value)
        worksheet.cell(7, 1, "当前账户")
        worksheet.cell(7, 2, "境内")
        worksheet.cell(7, 3, "银行")
        worksheet.cell(7, 4, "现金")
        worksheet.cell(7, 5, "RMB")
        worksheet.cell(7, 10, existing_total)
        worksheet.cell(7, 11, existing_total)
        stream = BytesIO()
        workbook.save(stream)
        workbook.close()
        stream.seek(0)
        stream.name = "资产快照录入.xlsx"
        return stream

    def test_imports_older_snapshot_and_preserves_latest_default_signature(self):
        before = latest_snapshot_default_signature()
        result = import_asset_snapshot_workbook(
            family=self.family,
            source=self.workbook_source(),
            source_filename="资产快照录入.xlsx",
        )

        self.assertEqual(result.created_dates, [date(2024, 12, 31)])
        self.assertEqual(result.verified_dates, [date(2026, 5, 31)])
        imported = AssetBalanceSnapshot.objects.get(snapshot_date=date(2024, 12, 31))
        self.assertEqual(imported.entries.count(), 2)
        self.assertEqual(
            imported.entries.aggregate(total=Sum("base_amount"))["total"],
            Decimal("123"),
        )
        self.assertFalse(
            BankAccount.objects.get(member=self.me, account_name="历史银行").is_active
        )
        self.assertFalse(
            BankAccount.objects.get(member=self.me, account_name="境外券商").is_active
        )
        self.assertEqual(latest_snapshot_default_signature(), before)

    def test_existing_snapshot_mismatch_rolls_back_every_change(self):
        before = latest_snapshot_default_signature()
        with self.assertRaises(AssetSnapshotWorkbookError):
            import_asset_snapshot_workbook(
                family=self.family,
                source=self.workbook_source(existing_total=99),
                source_filename="资产快照录入.xlsx",
            )

        self.assertEqual(AssetBalanceSnapshot.objects.count(), 1)
        self.assertFalse(BankAccount.objects.filter(account_name="历史银行").exists())
        self.assertEqual(latest_snapshot_default_signature(), before)

    def test_asset_snapshot_list_builds_monthly_and_yearly_trend_in_ten_thousands(self):
        older = AssetBalanceSnapshot.objects.create(
            family=self.family,
            snapshot_date=date(2025, 12, 31),
            usd_to_base=Decimal("7"),
            hkd_to_base=Decimal("0.9"),
        )
        AssetBalanceEntry.objects.create(
            snapshot=older,
            member=self.me,
            account=self.current_account,
            account_name=self.current_account.account_name,
            asset_category=self.cash,
            currency="CNY",
            original_amount=Decimal("80000"),
            base_amount=Decimal("80000"),
            display_order=1,
        )
        AssetBalanceEntry.objects.create(
            snapshot=older,
            member=self.secretary,
            account_name="历史账户",
            asset_category=self.cash,
            currency="CNY",
            original_amount=Decimal("20000"),
            base_amount=Decimal("20000"),
            display_order=2,
        )
        user = get_user_model().objects.create_user(
            username="trend-tester",
            password="password",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("ledger:asset_snapshot_list"))

        self.assertEqual(response.status_code, 200)
        trend_data = response.context["trend_data"]
        self.assertEqual(trend_data["monthly"]["labels"], ["2025-12", "2026-05"])
        self.assertEqual(trend_data["yearly"]["labels"], ["2025", "2026"])
        monthly_series = {
            series["name"]: series["values"]
            for series in trend_data["monthly"]["series"]
        }
        self.assertEqual(monthly_series["我"], [8.0, 0.01])
        self.assertEqual(monthly_series["孙秘书"], [2.0, 0.0])
        self.assertEqual(monthly_series["家庭合计"], [10.0, 0.01])
        self.assertContains(response, 'id="asset-trend-chart"')
        self.assertContains(response, 'data-trend-period="monthly"')
        self.assertContains(response, 'data-trend-period="yearly"')
        self.assertContains(response, "js/asset_snapshot_trend.js")
        rendered_html = response.content.decode()
        self.assertLess(
            rendered_html.index("<h1>资产快照</h1>"),
            rendered_html.index("<h1>资产余额趋势</h1>"),
        )
        self.assertNotIn("asset-trend-summary", rendered_html)
        self.assertIn('class="asset-trend-empty" hidden', rendered_html)


class CategoryManagementTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name="我的家庭")
        self.user = get_user_model().objects.create_user(
            username="category-tester",
            password="password",
        )

    def test_expense_admin_form_creates_explicit_three_level_path(self):
        primary = ExpenseCategory.objects.create(
            family=self.family,
            name="经常性支出",
        )
        secondary = ExpenseCategory.objects.create(
            family=self.family,
            name="餐饮",
            parent=primary,
        )
        form = ExpenseCategoryAdminForm(
            data={
                "family": self.family.pk,
                "category_level": "3",
                "primary_category": primary.pk,
                "secondary_category": secondary.pk,
                "name": "工作餐",
                "is_active": "on",
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        category = form.save()
        self.assertEqual(category.parent, secondary)
        self.assertEqual(category.category_level, 3)
        self.assertEqual(str(category), "经常性支出-餐饮-工作餐")

    def test_admin_form_rejects_secondary_from_another_primary(self):
        first_primary = ExpenseCategory.objects.create(
            family=self.family,
            name="经常性支出",
        )
        second_primary = ExpenseCategory.objects.create(
            family=self.family,
            name="固定资产",
        )
        wrong_secondary = ExpenseCategory.objects.create(
            family=self.family,
            name="家居家电",
            parent=second_primary,
        )
        form = ExpenseCategoryAdminForm(
            data={
                "family": self.family.pk,
                "category_level": "3",
                "primary_category": first_primary.pk,
                "secondary_category": wrong_secondary.pk,
                "name": "小家电",
                "is_active": "on",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("secondary_category", form.errors)

    def test_income_admin_form_uses_same_level_structure(self):
        primary = IncomeCategory.objects.create(family=self.family, name="经营收入")
        form = IncomeCategoryAdminForm(
            data={
                "family": self.family.pk,
                "category_level": "2",
                "primary_category": primary.pk,
                "secondary_category": "",
                "name": "服务收入",
                "is_active": "on",
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        category = form.save()
        self.assertEqual(str(category), "经营收入-服务收入")
        self.assertEqual(category.category_level, 2)

    def test_front_page_is_read_only_and_legacy_add_url_redirects_to_admin(self):
        ExpenseCategory.objects.create(family=self.family, name="经常性支出")
        self.client.force_login(self.user)

        response = self.client.get(reverse("ledger:category_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "收入分类统一在后台按一级、二级维护")
        self.assertContains(response, "支出分类统一在后台按一级、二级、三级维护")
        self.assertContains(response, 'class="basics-table-scroll"', count=3)
        self.assertNotContains(response, "新增支出分类")
        self.assertNotContains(response, ">编辑</a>")

        redirect_response = self.client.get(reverse("ledger:expense_category_create"))
        self.assertRedirects(
            redirect_response,
            reverse("admin:ledger_expensecategory_add"),
            fetch_redirect_response=False,
        )

    def test_ledger_overview_does_not_show_direct_create_buttons(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("ledger:overview"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, ">新增收入</a>")
        self.assertNotContains(response, ">新增支出</a>")
        self.assertNotContains(response, ">新增资产快照</a>")

    def test_admin_add_pages_expose_correct_level_fields_instead_of_parent(self):
        primary = ExpenseCategory.objects.create(
            family=self.family,
            name="经常性支出",
        )
        secondary = ExpenseCategory.objects.create(
            family=self.family,
            name="餐饮",
            parent=primary,
        )
        staff = get_user_model().objects.create_superuser(
            username="category-admin",
            password="password",
            email="admin@example.com",
        )
        self.client.force_login(staff)

        income_response = self.client.get(reverse("admin:ledger_incomecategory_add"))
        self.assertEqual(income_response.status_code, 200)
        self.assertContains(income_response, 'name="category_level"')
        self.assertContains(income_response, 'name="primary_category"')
        self.assertNotContains(income_response, 'name="secondary_category"')
        self.assertNotContains(income_response, 'value="3">三级分类')
        self.assertNotContains(income_response, 'name="parent"')

        expense_response = self.client.get(reverse("admin:ledger_expensecategory_add"))
        self.assertEqual(expense_response.status_code, 200)
        self.assertContains(expense_response, 'name="category_level"')
        self.assertContains(expense_response, 'name="primary_category"')
        self.assertContains(expense_response, 'name="secondary_category"')
        self.assertContains(expense_response, 'value="3">三级分类')
        self.assertContains(expense_response, 'data-family-id="{}"'.format(self.family.pk))
        self.assertContains(expense_response, 'data-parent-id="{}"'.format(primary.pk))
        self.assertContains(expense_response, "js/admin_category_form.js")
        self.assertNotContains(expense_response, 'name="parent"')

    def test_expense_admin_add_form_defaults_family_and_loads_existing_categories(self):
        primary = ExpenseCategory.objects.create(
            family=self.family,
            name="经常性支出",
        )
        secondary = ExpenseCategory.objects.create(
            family=self.family,
            name="餐饮",
            parent=primary,
        )

        form = ExpenseCategoryAdminForm()

        self.assertEqual(form.initial["family"], self.family.pk)
        self.assertQuerySetEqual(
            form.fields["primary_category"].queryset,
            [primary],
        )
        self.assertQuerySetEqual(
            form.fields["secondary_category"].queryset,
            [secondary],
        )
        income_form = IncomeCategoryAdminForm()
        self.assertEqual(
            list(income_form.fields["category_level"].choices),
            [("1", "一级分类"), ("2", "二级分类")],
        )
        self.assertNotIn("secondary_category", income_form.fields)


class LedgerNavigationAndRedirectTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name="Navigation Test Family")
        self.member = FamilyMember.objects.create(
            family=self.family,
            display_name="Navigation Tester",
        )
        self.user = get_user_model().objects.create_user(
            username="navigation-tester",
            password="password",
        )
        self.client.force_login(self.user)

    def test_ledger_page_has_consistent_context_navigation(self):
        response = self.client.get(reverse("ledger:category_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="page-context-nav"')
        self.assertContains(response, "data-page-back")
        self.assertContains(response, reverse("ledger:overview"))
        self.assertContains(response, reverse("dashboard:home"))

    def test_ledger_overview_does_not_repeat_module_home_link(self):
        response = self.client.get(reverse("ledger:overview"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-page-back")
        self.assertNotContains(response, 'class="button page-nav-module"')

    def test_delete_redirect_does_not_accept_scheme_relative_external_url(self):
        record = ExpenseRecord.objects.create(
            family=self.family,
            member=self.member,
            expense_date=date(2026, 6, 28),
            amount=Decimal("10"),
        )

        response = self.client.post(
            reverse("ledger:expense_delete", args=[record.pk]),
            {"next": "//evil.example/steal"},
        )

        self.assertRedirects(
            response,
            reverse("ledger:expense_list"),
            fetch_redirect_response=False,
        )
