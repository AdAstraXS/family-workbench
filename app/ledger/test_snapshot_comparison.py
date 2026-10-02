from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from family_core.models import AssetCategory, Family, FamilyMember
from .asset_snapshot_comparison import SnapshotComparisonForm, build_snapshot_comparison
from .models import AssetBalanceEntry, AssetBalanceSnapshot, BankAccount


class SnapshotComparisonTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name="家庭")
        self.user = get_user_model().objects.create_user(username="comparison")
        self.me = FamilyMember.objects.create(family=self.family, user=self.user, display_name="我")
        self.other = FamilyMember.objects.create(family=self.family, display_name="孙秘书")
        self.category = AssetCategory.objects.create(family=self.family, name="现金")
        self.account = BankAccount.objects.create(family=self.family, member=self.me, account_name="同名账户")
        self.other_account = BankAccount.objects.create(family=self.family, member=self.other, account_name="同名账户")
        self.snapshots = [AssetBalanceSnapshot.objects.create(
            family=self.family, snapshot_date=date(2026, month, 1), usd_to_base=Decimal("7")
        ) for month in (1, 2, 3)]
        self.client.force_login(self.user)

    def entry(self, snapshot, amount, *, member=None, account=None, currency="USD", base=None):
        return AssetBalanceEntry.objects.create(
            snapshot=snapshot, member=member or self.me, account=account or self.account,
            asset_category=self.category, currency=currency, original_amount=Decimal(amount),
            base_amount=Decimal(base) if base is not None else Decimal(amount) * Decimal("7"),
        )

    def test_saved_base_amounts_and_selected_period_changes_use_decimal(self):
        self.entry(self.snapshots[0], "100.1234", base="700.8638")
        self.entry(self.snapshots[1], "150", base="1050")
        self.entry(self.snapshots[2], "200.1234", base="1440.8888")
        report = build_snapshot_comparison([self.snapshots[2], self.snapshots[0]], [self.me], "base")
        self.assertEqual(report["snapshots"], [self.snapshots[0], self.snapshots[2]])
        self.assertEqual(report["rows"][0]["cells"][1]["change"], Decimal("740.0250"))
        original = build_snapshot_comparison(self.snapshots, [self.me], "original")
        self.assertEqual(original["rows"][0]["cells"][1]["change"], Decimal("49.8766"))

    def test_same_named_accounts_and_members_are_not_merged(self):
        for snapshot in self.snapshots:
            self.entry(snapshot, "10")
            self.entry(snapshot, "30", member=self.other, account=self.other_account)
        report = build_snapshot_comparison(self.snapshots, [self.me, self.other], "original")
        self.assertEqual(len(report["rows"]), 2)
        self.assertEqual(report["totals"][0]["cells"][0]["amount"], Decimal("40"))
        filtered = build_snapshot_comparison(self.snapshots, [self.me], "original")
        self.assertEqual(len(filtered["rows"]), 1)
        self.assertEqual(filtered["totals"][0]["cells"][0]["amount"], Decimal("10"))

    def test_missing_record_is_not_zero_and_explicit_zero_can_be_compared(self):
        self.entry(self.snapshots[0], "10")
        self.entry(self.snapshots[2], "0")
        cells = build_snapshot_comparison(self.snapshots, [self.me], "original")["rows"][0]["cells"]
        self.assertIsNone(cells[1]["amount"])
        self.assertIsNone(cells[2]["change"])
        self.assertEqual(cells[2]["amount"], Decimal("0"))
        cells = build_snapshot_comparison([self.snapshots[0], self.snapshots[2]], [self.me], "original")["rows"][0]["cells"]
        self.assertEqual(cells[1]["change"], Decimal("-10"))

    def test_original_currency_totals_are_separate_and_missing_rate_is_flagged(self):
        self.entry(self.snapshots[0], "10")
        self.entry(self.snapshots[0], "20", currency="CNY", base="20")
        report = build_snapshot_comparison(self.snapshots, [self.me], "original")
        self.assertEqual({r["currency"] for r in report["totals"]}, {"USD", "CNY"})
        self.snapshots[0].usd_to_base = Decimal("0")
        report = build_snapshot_comparison(self.snapshots, [self.me], "base")
        self.assertTrue(report["warnings"])
        self.assertIsNone(report["totals"][0]["cells"][0]["amount"])

    def test_form_rejects_draft_other_family_and_different_base_currency(self):
        draft = AssetBalanceSnapshot.objects.create(family=self.family, snapshot_date=date(2026, 4, 1), is_draft=True)
        foreign_family = Family.objects.create(name="其他家庭")
        foreign = AssetBalanceSnapshot.objects.create(family=foreign_family, snapshot_date=date(2026, 4, 1))
        for invalid in (draft, foreign):
            form = SnapshotComparisonForm({"snapshots": [self.snapshots[0].pk, invalid.pk], "members": [self.me.pk], "amount_mode": "base"}, family=self.family)
            self.assertFalse(form.is_valid())
            self.assertIn("snapshots", form.errors)
        self.snapshots[1].base_currency = "USD"
        self.snapshots[1].save()
        form = SnapshotComparisonForm({"snapshots": [s.pk for s in self.snapshots], "members": [self.me.pk], "amount_mode": "base"}, family=self.family)
        self.assertFalse(form.is_valid())
        self.assertIn("amount_mode", form.errors)

    def test_page_renders_multiple_periods_without_writing_data(self):
        for snapshot in self.snapshots:
            self.entry(snapshot, "100")
        counts = (AssetBalanceSnapshot.objects.count(), AssetBalanceEntry.objects.count())
        response = self.client.get(reverse("ledger:asset_snapshot_compare"), {
            "snapshots": [s.pk for s in reversed(self.snapshots)], "members": [self.me.pk], "amount_mode": "base",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "较前期变动", count=3)
        self.assertEqual(len(response.context["report"]["rows"][0]["cells"]), 3)
        self.assertEqual(counts, (AssetBalanceSnapshot.objects.count(), AssetBalanceEntry.objects.count()))
        invalid = self.client.get(reverse("ledger:asset_snapshot_compare"), {"snapshots": [self.snapshots[0].pk], "members": [self.me.pk], "amount_mode": "base"})
        self.assertContains(invalid, "至少选择两期")
