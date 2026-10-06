from datetime import date
from decimal import Decimal
from io import StringIO
from unittest.mock import patch
import json
from django.core.management import call_command
from django.core.management.base import CommandError
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.urls import reverse
from .models import Family, FamilyMember, AssetCategory, AssetClassificationAudit
from .asset_classification import seed_categories, validate_assignment, government_term_category
from .classification_preview import build_classification_preview, apply_classification_preview, propose_security, propose_entry
from ledger.models import BankAccount, AssetBalanceSnapshot, AssetBalanceEntry
from ledger.forms import AssetBalanceEntryForm
from portfolio.models import Security, InvestmentAccount, InvestmentTransaction, PortfolioSnapshot, PortfolioSnapshotPositionLine, WatchlistItem, BondDetail
from portfolio.forms import SecurityForm

class AssetClassificationTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name='家庭')
        self.user = get_user_model().objects.create_user(username='classification')
        self.member = FamilyMember.objects.create(family=self.family, user=self.user, display_name='我')
        seed_categories(self.family)
        self.categories = {c.code: c for c in AssetCategory.objects.filter(family=self.family)}
        self.bank = BankAccount.objects.create(family=self.family, member=self.member, account_name='招商银行', supports_investment=True)
        self.account = InvestmentAccount.objects.create(bank_account=self.bank)
        self.day = date(2025, 12, 31)
        self.snapshot = AssetBalanceSnapshot.objects.create(family=self.family, snapshot_date=self.day)
        self.security = Security.objects.create(symbol='VOO', name='标普500', market='US', currency='USD', asset_type='etf', asset_category=self.categories['equity'])
        WatchlistItem.objects.create(family=self.family, member=self.member, security=self.security)

    def entry(self, category='fixed_income', name=None, **kwargs):
        bank = self.bank
        if name:
            bank = BankAccount.objects.create(family=self.family, member=self.member, account_name=name)
        return AssetBalanceEntry.objects.create(snapshot=self.snapshot, member=self.member, account=bank, asset_category=self.categories[category], original_amount=Decimal('123.4567'), base_amount=Decimal('123.4567'), **kwargs)

    def report(self):
        return build_classification_preview(self.family, self.day, self.day)

    def test_complete_batch_rejects_unknowns_without_partial_writes(self):
        self.entry('alternatives', '未知产品')
        report=self.report()
        before=list(Security.objects.values())
        with self.assertRaisesMessage(ValidationError,'整批未写入'):
            apply_classification_preview(self.family,self.day,self.day,report['digest'],require_complete=True)
        self.assertEqual(before,list(Security.objects.values()))
        self.assertFalse(AssetClassificationAudit.objects.exists())

    def test_confirmation_stdin_is_read_only_bounded_and_same_digest(self):
        entry=self.entry()
        plan=self.confirmation(entry)
        report=build_classification_preview(self.family,self.day,self.day,confirmations=plan)
        output=StringIO()
        with patch('sys.stdin',StringIO(json.dumps(plan))):
            call_command('preview_asset_classification',family=self.family.pk,start=str(self.day),end=str(self.day),
                confirmations='-',stdout=output)
        self.assertEqual(json.loads(output.getvalue())['digest'],report['digest'])
        self.assertFalse(AssetClassificationAudit.objects.exists())
        for payload in ('[invalid', '中' * (2 * 1024 * 1024)):
            with patch('sys.stdin',StringIO(payload)),self.assertRaises(CommandError):
                call_command('preview_asset_classification',family=self.family.pk,start=str(self.day),end=str(self.day),
                    confirmations='-',stdout=StringIO())

    def test_dictionary_has_exact_thirteen_leaves_and_seed_is_idempotent(self):
        self.assertEqual(AssetCategory.objects.filter(family=self.family, parent__isnull=False).count(), 13)
        self.assertEqual(AssetCategory.objects.filter(family=self.family, parent__isnull=True).count(), 8)
        before = list(AssetCategory.objects.filter(family=self.family).values_list('id', 'code', 'name', 'parent_id'))
        seed_categories(self.family)
        self.assertEqual(before, list(AssetCategory.objects.filter(family=self.family).values_list('id', 'code', 'name', 'parent_id')))

    def test_form_renders_secondary_options_with_parent_and_adjacent_fields(self):
        self.security.asset_category = self.categories['equity_index']
        form = SecurityForm(instance=self.security, family=self.family)
        html = str(form['asset_category'])
        self.assertIn(f'data-parent-id="{self.categories["equity"].pk}"', html)
        self.assertIn('股指基金', html)
        self.assertIn('selected', html)
        names = list(form.fields)
        self.assertEqual(names[names.index('asset_primary') + 1], 'asset_category')
        legacy = AssetCategory.objects.create(family=self.family, code='old-card', name='信用卡（旧分类）')
        self.assertNotIn(legacy, form.fields['asset_primary'].queryset)
        self.security.asset_category = legacy
        historical = SecurityForm(instance=self.security, family=self.family)
        self.assertIn(legacy, historical.fields['asset_primary'].queryset)
        self.assertIn('历史未细分', str(historical['asset_category']))

    def test_dictionary_migration_preserves_old_categories_and_financial_records(self):
        import importlib
        from django.apps import apps
        from django.db import connection
        from types import SimpleNamespace
        legacy = Family.objects.create(name='旧家庭')
        old = AssetCategory.objects.create(family=legacy, name='基金类', code='fund')
        entry = self.entry()
        before = list(AssetBalanceEntry.objects.values())
        migration = importlib.import_module('family_core.migrations.0010_two_level_asset_dictionary')
        migration.install_dictionary(apps, SimpleNamespace(connection=connection))
        self.assertEqual(before, list(AssetBalanceEntry.objects.values()))
        old.refresh_from_db()
        self.assertIsNone(old.parent_id)
        self.assertEqual(old.name, '基金类')
        self.assertEqual(AssetCategory.objects.filter(family=legacy, parent__isnull=False).count(), 13)

    def test_legacy_credit_card_name_is_archived_without_repointing_financial_rows(self):
        import importlib
        from django.apps import apps
        from django.db import connection
        from types import SimpleNamespace
        legacy_family = Family.objects.create(name='旧信用卡家庭')
        member = FamilyMember.objects.create(family=legacy_family, display_name='成员')
        bank = BankAccount.objects.create(family=legacy_family, member=member, account_name='中国银行')
        category = AssetCategory.objects.create(family=legacy_family, name='信用卡', code='asset-category-legacy-card', extra_data={'existing':'保留'})
        snapshot = AssetBalanceSnapshot.objects.create(family=legacy_family, snapshot_date=self.day)
        entry = AssetBalanceEntry.objects.create(snapshot=snapshot, member=member, account=bank, asset_category=category, original_amount=Decimal('-99.1234'), base_amount=Decimal('-99.1234'))
        before = list(AssetBalanceEntry.objects.values())
        migration = importlib.import_module('family_core.migrations.0010_two_level_asset_dictionary')
        migration.install_dictionary(apps, SimpleNamespace(connection=connection))
        self.assertEqual(before, list(AssetBalanceEntry.objects.values()))
        category.refresh_from_db()
        entry.refresh_from_db()
        self.assertEqual(category.name, '信用卡（旧分类）')
        self.assertEqual(category.code, 'asset-category-legacy-card')
        self.assertIsNone(category.parent_id)
        self.assertEqual(category.extra_data['existing'],'保留')
        self.assertEqual(category.extra_data['classification_legacy_label'],'信用卡')
        self.assertEqual(AssetCategory.objects.get(family=legacy_family,code='credit_card').parent.code,'liabilities')
        self.assertEqual(propose_entry(entry)[0],'credit_card')
        entry.original_amount=Decimal('1')
        self.assertIsNone(propose_entry(entry)[0])
        seed_categories(legacy_family)
        self.assertEqual(AssetCategory.objects.filter(family=legacy_family,parent__isnull=False).count(),13)

    def test_get_pages_render_two_selectors_without_writing(self):
        self.entry()
        self.client.force_login(self.user)
        before = (AssetClassificationAudit.objects.count(), AssetBalanceEntry.objects.count())
        response = self.client.get(reverse('ledger:asset_snapshot_edit', args=[self.snapshot.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'entries-0-asset_primary')
        self.assertContains(response, 'entries-0-asset_category')
        response = self.client.get(reverse('portfolio:transaction_create'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id_asset_primary')
        self.assertContains(response, 'id_asset_category')
        legacy = AssetCategory.objects.create(family=self.family, code='old-card', name='信用卡（旧分类）')
        response = self.client.get(reverse('portfolio:transaction_form_options'), {'family': self.family.pk})
        choices = {row['id']: row for row in response.json()['categories']}
        self.assertFalse(choices[legacy.pk]['is_primary_choice'])
        self.assertTrue(choices[self.categories['equity'].pk]['is_primary_choice'])
        self.assertEqual(before, (AssetClassificationAudit.objects.count(), AssetBalanceEntry.objects.count()))

    def test_hierarchy_rejects_cycles_third_level_and_other_family(self):
        root = self.categories['equity']
        root.parent = root
        with self.assertRaises(ValidationError):
            root.clean()
        category = AssetCategory(family=self.family, name='第三级', code='third', parent=self.categories['equity_stock'])
        with self.assertRaises(ValidationError):
            category.clean()
        category.parent = self.categories['cash']
        category.family = Family.objects.create(name='其他家庭')
        with self.assertRaises(ValidationError):
            category.clean()
        with self.assertRaises(ProtectedError):
            self.categories['cash'].delete()

    def test_instrument_type_remains_independent_from_asset_class(self):
        validate_assignment(self.categories['government_long'], family=self.family, instrument='etf')
        validate_assignment(self.categories['government_long'], family=self.family, instrument='bond')
        validate_assignment(self.categories['crypto'], family=self.family, instrument='etf')
        with self.assertRaises(ValidationError):
            validate_assignment(self.categories['equity_stock'], instrument='etf')

    def test_original_issuance_anniversary_handles_leap_year_and_missing_metadata(self):
        self.assertEqual(government_term_category(date(2023, 3, 1), date(2024, 3, 1)), 'government_short')
        self.assertEqual(government_term_category(date(2024, 2, 29), date(2025, 2, 28)), 'government_short')
        self.assertEqual(government_term_category(date(2023, 3, 1), date(2024, 3, 2)), 'government_long')
        self.assertIsNone(government_term_category(None, self.day))

    def test_user_confirmed_ledger_mappings(self):
        for old, name, expected in [
            ('fixed_income', '招商银行', 'cash_balance'), ('fixed_income', '医保账户', 'cash_balance'),
            ('fixed_income', '养老金', 'savings_insurance'), ('cash', '活期余额', 'cash_balance'),
            ('fixed_income', '现金', 'cash_balance'),
        ]:
            self.assertEqual(propose_entry(self.entry(old, name))[0], expected)
        fund = AssetCategory.objects.create(family=self.family, name='基金类', code='fund')
        self.categories['fund'] = fund
        self.assertEqual(propose_entry(self.entry('fund', '支付宝'))[0], 'equity_fund')
        entry = self.entry('alternatives', '套利账户', remark='套利')
        self.assertEqual(propose_entry(entry)[0], 'cash_balance')

    def test_credit_card_positive_balance_is_not_silently_negated(self):
        entry = self.entry('cash', '信用卡')
        self.assertIsNone(propose_entry(entry)[0])
        entry.original_amount = Decimal('-20')
        self.assertEqual(propose_entry(entry)[0], 'credit_card')

    def test_bank_account_type_and_explicit_arbitrage_name_are_respected(self):
        from .models import AccountType
        account_type = AccountType.objects.create(family=self.family, name='银行', code='bank')
        entry = self.entry('fixed_income', '中银香港')
        entry.account.account_type_ref = account_type
        entry.account.save()
        self.assertEqual(propose_entry(entry)[0], 'cash_balance')
        self.assertEqual(propose_entry(self.entry('alternatives', '套利账户'))[0], 'cash_balance')

    def test_etf_rules_include_industry_crypto_bonds_and_mixed_fund(self):
        for symbol, expected in [('XLU', 'equity_fund'), ('XLV', 'equity_fund'), ('SMH', 'equity_fund'), ('TQQQ', 'equity_index'), ('IBIT', 'crypto'), ('03433', 'government_long'), ('ALLW', 'equity_fund')]:
            self.security.symbol = symbol
            self.assertEqual(propose_security(self.security)[0], expected)

    def test_exact_legacy_treasury_alias_and_owner_confirmed_tech_fund(self):
        self.security.asset_type = 'bond'
        self.security.symbol = 'GOVT 4.75 NOV15’53 912810TV0'
        self.assertEqual(propose_security(self.security)[0], 'government_long')
        self.assertEqual(self.security.symbol, 'GOVT 4.75 NOV15’53 912810TV0')
        self.security.symbol = 'UNKNOWN 912810TV0'
        self.assertIsNone(propose_security(self.security)[0])
        self.security.asset_type = 'etf'
        self.security.symbol = '07552'
        self.assertEqual(propose_security(self.security)[0], 'equity_fund')

    def confirmation(self, entry, target='cash_balance', scope='matching_history'):
        return {'version': 1, 'ledger_rules': [{
            'anchor': {'id': entry.pk, 'date': str(entry.snapshot.snapshot_date),
                       'member_name': str(entry.member), 'account_name': entry.account.account_name if entry.account else entry.account_name,
                       'currency': entry.currency, 'old_category_code': entry.asset_category.code,
                       'original_amount': str(entry.original_amount)},
            'target_code': target, 'scope': scope,
        }]}

    def test_government_etfs_accept_both_terms_and_unknown_underlying_is_not_guessed(self):
        for code in ('government_short', 'government_long'):
            for instrument in ('etf', 'fund', 'bond'):
                validate_assignment(self.categories[code], family=self.family, instrument=instrument)
        self.security.symbol = 'UNKNOWN_GOV_ETF'
        self.assertIsNone(propose_security(self.security)[0])
        self.security.asset_category = self.categories['government_short']
        self.assertEqual(propose_security(self.security)[0], 'government_short')

    def test_confirmation_overrides_generic_bank_rule_and_extends_by_ids_only(self):
        entry = self.entry()
        plan = self.confirmation(entry, 'bond_fund')
        earlier = AssetBalanceSnapshot.objects.create(family=self.family, snapshot_date=date(2024, 12, 31))
        historical = self.entry()
        historical.snapshot = earlier
        historical.save()
        same_name_different_account = self.entry(name=self.bank.account_name)
        different_currency = self.entry(currency='USD')
        before = list(AssetBalanceEntry.objects.values())
        report = build_classification_preview(self.family, earlier.snapshot_date, self.day, confirmations=plan)
        rows = {r['id']: r for r in report['rows'] if r['model'] == 'ledger.assetbalanceentry'}
        self.assertEqual(rows[entry.pk]['proposed_code'], 'bond_fund')
        self.assertEqual(rows[historical.pk]['proposed_code'], 'bond_fund')
        self.assertEqual(rows[same_name_different_account.pk]['proposed_code'], 'cash_balance')
        self.assertEqual(rows[different_currency.pk]['proposed_code'], 'cash_balance')
        self.assertEqual(before, list(AssetBalanceEntry.objects.values()))

    def test_ambiguous_historical_pair_keeps_exact_anchors_and_blocks_other_dates(self):
        cash, bond, historical = self.entry(), self.entry(), self.entry()
        plan = self.confirmation(cash, 'cash_balance')
        plan['ledger_rules'] += self.confirmation(bond, 'government_long')['ledger_rules']
        report = build_classification_preview(self.family, self.day, self.day, confirmations=plan)
        rows = {r['id']: r for r in report['rows'] if r['model'] == 'ledger.assetbalanceentry'}
        self.assertEqual(rows[cash.pk]['proposed_code'], 'cash_balance')
        self.assertEqual(rows[bond.pk]['proposed_code'], 'government_long')
        self.assertEqual(rows[historical.pk]['status'], 'unresolved')
        self.assertIn('多个新类别', rows[historical.pk]['reason'])

    def test_record_only_confirmation_does_not_reclassify_other_history(self):
        cash, bond, historical = self.entry(), self.entry(), self.entry()
        plan = self.confirmation(cash, 'cash_balance', 'record')
        plan['ledger_rules'] += self.confirmation(bond, 'government_long', 'record')['ledger_rules']
        rows = build_classification_preview(self.family, self.day, self.day, confirmations=plan)['rows']
        self.assertEqual(next(r for r in rows if r['model'] == 'ledger.assetbalanceentry' and r['id'] == historical.pk)['status'], 'unresolved')

    def test_explicit_other_dates_cash_preserves_source_date_bond_and_cash(self):
        cash, bond = self.entry(), self.entry()
        plan = self.confirmation(cash, 'cash_balance')
        plan['ledger_rules'] += self.confirmation(bond, 'government_long')['ledger_rules']
        for rule in plan['ledger_rules']:
            rule['history_target_code'] = 'cash_balance'
        historical = self.entry()
        earlier = AssetBalanceSnapshot.objects.create(family=self.family, snapshot_date=date(2024, 12, 31))
        historical.snapshot = earlier
        historical.save()
        unknown_same_day = self.entry()
        report = build_classification_preview(self.family, earlier.snapshot_date, self.day, confirmations=plan)
        rows = {r['id']: r for r in report['rows'] if r['model'] == 'ledger.assetbalanceentry'}
        self.assertEqual(rows[cash.pk]['proposed_code'], 'cash_balance')
        self.assertEqual(rows[bond.pk]['proposed_code'], 'government_long')
        self.assertEqual(rows[historical.pk]['proposed_code'], 'cash_balance')
        self.assertEqual(rows[unknown_same_day.pk]['status'], 'unresolved')
        before = report['financial_digest']
        apply_classification_preview(self.family, earlier.snapshot_date, self.day, report['digest'], confirmations=plan)
        historical.refresh_from_db()
        bond.refresh_from_db()
        self.assertEqual(historical.asset_category.code, 'cash_balance')
        self.assertEqual(bond.asset_category.code, 'government_long')
        self.assertEqual(build_classification_preview(self.family, earlier.snapshot_date, self.day, confirmations=plan)['financial_digest'], before)

    def test_conflicting_other_dates_codes_stay_unresolved(self):
        cash, bond = self.entry(), self.entry()
        plan = self.confirmation(cash, 'cash_balance')
        plan['ledger_rules'] += self.confirmation(bond, 'government_long')['ledger_rules']
        plan['ledger_rules'][0]['history_target_code'] = 'cash_balance'
        plan['ledger_rules'][1]['history_target_code'] = 'government_long'
        historical = self.entry()
        earlier = AssetBalanceSnapshot.objects.create(family=self.family, snapshot_date=date(2024, 12, 31))
        historical.snapshot = earlier
        historical.save()
        rows = build_classification_preview(self.family, earlier.snapshot_date, self.day, confirmations=plan)['rows']
        self.assertEqual(next(r for r in rows if r['model'] == 'ledger.assetbalanceentry' and r['id'] == historical.pk)['status'], 'unresolved')

    def test_stale_or_other_family_confirmation_stops_before_any_write(self):
        entry = self.entry()
        plan = self.confirmation(entry)
        report = build_classification_preview(self.family, self.day, self.day, confirmations=plan)
        entry.base_amount = Decimal('999')
        entry.save()
        with self.assertRaises(ValidationError):
            apply_classification_preview(self.family, self.day, self.day, report['digest'], confirmations=plan)
        entry.original_amount = Decimal('100')
        entry.save()
        with self.assertRaises(ValidationError):
            build_classification_preview(self.family, self.day, self.day, confirmations=plan)
        other = Family.objects.create(name='其他家庭')
        with self.assertRaises(ValidationError):
            build_classification_preview(other, self.day, self.day, confirmations=plan)
        self.assertFalse(AssetClassificationAudit.objects.exists())

    def test_modified_confirmation_digest_cannot_apply_and_valid_plan_is_idempotent(self):
        from copy import deepcopy
        entry = self.entry()
        plan = self.confirmation(entry, 'bond_fund')
        report = build_classification_preview(self.family, self.day, self.day, confirmations=plan)
        changed = deepcopy(plan)
        changed['ledger_rules'][0]['target_code'] = 'government_long'
        with self.assertRaises(ValidationError):
            apply_classification_preview(self.family, self.day, self.day, report['digest'], confirmations=changed)
        result = apply_classification_preview(self.family, self.day, self.day, report['digest'], confirmations=plan)
        entry.refresh_from_db()
        self.assertEqual(entry.asset_category.code, 'bond_fund')
        self.assertEqual(entry.original_amount, Decimal('123.4567'))
        self.assertEqual(entry.base_amount, Decimal('123.4567'))
        second = build_classification_preview(self.family, self.day, self.day, confirmations=plan)
        self.assertEqual(second['financial_digest'], report['financial_digest'])
        self.assertEqual(apply_classification_preview(self.family, self.day, self.day, second['digest'], confirmations=plan)['updated'], 0)
        self.assertEqual(AssetClassificationAudit.objects.count(), result['updated'])

    def test_confirmation_invalid_payloads_are_rejected_and_existing_leaf_preserved(self):
        from copy import deepcopy
        entry = self.entry()
        plan = self.confirmation(entry)
        for mutation in ('duplicate', 'nan', 'float', 'wrong_member', 'unknown_code'):
            invalid = deepcopy(plan)
            rule = invalid['ledger_rules'][0]
            if mutation == 'duplicate': invalid['ledger_rules'].append(deepcopy(rule))
            elif mutation == 'nan': rule['anchor']['original_amount'] = 'NaN'
            elif mutation == 'float': rule['anchor']['original_amount'] = 123.4567
            elif mutation == 'wrong_member': rule['anchor']['member_name'] = '其他人'
            else: rule['target_code'] = 'unknown'
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                build_classification_preview(self.family, self.day, self.day, confirmations=invalid)
        historical = self.entry('equity_stock')
        rows = build_classification_preview(self.family, self.day, self.day, confirmations=plan)['rows']
        self.assertEqual(next(r for r in rows if r['model'] == 'ledger.assetbalanceentry' and r['id'] == historical.pk)['status'], 'unchanged')

    def test_bond_without_original_issue_date_remains_unresolved(self):
        self.security.asset_type = 'bond'
        self.security.symbol = 'UNKNOWN'
        self.security.save()
        BondDetail.objects.create(security=self.security, maturity_date=date(2030, 1, 1))
        self.assertIsNone(propose_security(self.security)[0])

    def test_bond_form_uses_original_term_and_rejects_company_bond_as_government(self):
        from .models import Currency
        from portfolio.models import SecurityMarket
        from portfolio.forms import BondForm
        Currency.objects.get_or_create(code='USD', defaults={'name': '美元'})
        SecurityMarket.objects.get_or_create(code='US', defaults={'name': '美国', 'default_currency': 'USD'})
        data = {'asset_primary': self.categories['fixed_income'].pk, 'asset_category': self.categories['government_short'].pk, 'symbol': 'TESTBOND', 'name': '测试国债', 'market': 'US', 'currency': 'USD', 'bond_type': BondDetail.GOVERNMENT, 'face_value': '100', 'coupon_rate': '0', 'coupon_frequency': '2', 'original_issue_date': '2024-01-01', 'maturity_date': '2025-01-01', 'redemption_price': '100', 'quote_basis': BondDetail.PER_100, 'clean_price': '99', 'accrued_interest': '0'}
        form = BondForm(data, family=self.family)
        self.assertTrue(form.is_valid(), form.errors)
        data['original_issue_date'] = '2020-01-01'
        form = BondForm(data, family=self.family)
        self.assertFalse(form.is_valid())
        self.assertIn('asset_category', form.errors)
        data['bond_type'] = BondDetail.CORPORATE
        form = BondForm(data, family=self.family)
        self.assertFalse(form.is_valid())
        self.assertIn('bond_type', form.errors)

    def test_preview_is_read_only_and_unknowns_are_explicit(self):
        entry = self.entry('alternatives', '未知产品')
        before = (entry.asset_category_id, AssetClassificationAudit.objects.count())
        report = self.report()
        row = next(r for r in report['rows'] if r['model'] == 'ledger.assetbalanceentry')
        self.assertEqual(row['status'], 'unresolved')
        entry.refresh_from_db()
        self.assertEqual(before, (entry.asset_category_id, AssetClassificationAudit.objects.count()))

    def test_apply_preserves_financial_values_records_old_categories_and_is_idempotent(self):
        entry = self.entry()
        unknown = self.entry('alternatives', '未知产品')
        unknown_old = unknown.asset_category_id
        report = self.report()
        result = apply_classification_preview(self.family, self.day, self.day, report['digest'])
        entry.refresh_from_db()
        self.security.refresh_from_db()
        unknown.refresh_from_db()
        self.assertEqual(entry.asset_category.code, 'cash_balance')
        self.assertEqual(self.security.asset_category.code, 'equity_index')
        self.assertEqual(self.security.asset_type, 'etf')
        self.assertEqual(entry.original_amount, Decimal('123.4567'))
        self.assertEqual(entry.base_amount, Decimal('123.4567'))
        self.assertEqual(unknown.asset_category_id, unknown_old)
        self.assertEqual(AssetClassificationAudit.objects.count(), result['updated'])
        self.assertTrue(AssetClassificationAudit.objects.filter(old_category__name='权益类').exists())
        second = self.report()
        self.assertEqual(second['financial_digest'], report['financial_digest'])
        again = apply_classification_preview(self.family, self.day, self.day, second['digest'])
        self.assertEqual(again['updated'], 0)

    def test_stale_preview_cannot_write_any_categories(self):
        entry = self.entry()
        report = self.report()
        entry.original_amount += Decimal('1')
        entry.save()
        with self.assertRaises(ValidationError):
            apply_classification_preview(self.family, self.day, self.day, report['digest'])
        self.security.refresh_from_db()
        self.assertEqual(self.security.asset_category.code, 'equity')
        self.assertFalse(AssetClassificationAudit.objects.exists())

    def test_date_range_limits_ledger_rows(self):
        entry = self.entry()
        report = build_classification_preview(self.family, date(2026, 1, 1), date(2026, 12, 31))
        self.assertFalse(any(r['model'] == 'ledger.assetbalanceentry' for r in report['rows']))
        self.assertTrue(any(r['model'] == 'portfolio.security' for r in report['rows']))

    def test_cross_family_shared_security_is_not_overwritten(self):
        other = Family.objects.create(name='其他家庭')
        WatchlistItem.objects.create(family=other, security=self.security)
        row = next(r for r in self.report()['rows'] if r['model'] == 'portfolio.security')
        self.assertEqual(row['status'], 'unresolved')

    def test_family_dictionary_takes_precedence_over_global_dictionary(self):
        seed_categories(None)
        row = next(r for r in self.report()['rows'] if r['model'] == 'portfolio.security')
        self.assertEqual(row['new_id'], self.categories['equity_index'].pk)
        self.assertEqual(Security.default_asset_category(self.family, 'stock'), self.categories['equity_stock'])

    def test_explicit_transaction_secondary_category_is_preserved(self):
        trade = InvestmentTransaction.objects.create(account=self.account, security=self.security, trade_date=self.day, asset_category=self.categories['equity_fund'], currency='USD', quantity=1, price=10, amount=10)
        row = next(r for r in self.report()['rows'] if r['model'] == 'portfolio.investmenttransaction')
        self.assertEqual(row['new_id'], trade.asset_category_id)
        self.assertEqual(row['status'], 'unchanged')

    def test_invalid_instrument_classification_is_not_propagated_into_history(self):
        self.security.asset_category = self.categories['equity_stock']
        self.security.save()
        InvestmentTransaction.objects.create(account=self.account, security=self.security, trade_date=self.day, currency='USD', quantity=1, price=10, amount=10)
        rows = [r for r in self.report()['rows'] if r['model'] in ('portfolio.security', 'portfolio.investmenttransaction')]
        self.assertTrue(all(r['status'] == 'unresolved' for r in rows))

    def test_empty_inline_and_unsaved_snapshot_use_selected_family_dictionary(self):
        from ledger.forms import make_asset_balance_entry_formset
        other = Family.objects.create(name='其他家庭')
        seed_categories(other)
        snapshot = AssetBalanceSnapshot(family=other, snapshot_date=self.day)
        formset = make_asset_balance_entry_formset(extra=1)(instance=snapshot)
        self.assertTrue(all(c.family_id in (None, other.pk) for c in formset.empty_form.fields['asset_category'].queryset))
        empty = make_asset_balance_entry_formset(extra=1)({'entries-TOTAL_FORMS': '1', 'entries-INITIAL_FORMS': '0', 'entries-0-currency': 'CNY', 'entries-0-display_order': '0', 'entries-0-original_amount': '0'}, instance=snapshot)
        self.assertTrue(empty.is_valid(), empty.errors)

    def test_new_snapshot_category_is_frozen_independently_of_security(self):
        snapshot = PortfolioSnapshot.objects.create(family=self.family, account=self.account, snapshot_date=self.day)
        line = PortfolioSnapshotPositionLine.objects.create(snapshot=snapshot, account=self.account, security=self.security, asset_name='VOO', asset_type='etf', currency='USD', asset_category=self.categories['equity_index'])
        self.security.asset_category = self.categories['equity_fund']
        self.security.save()
        line.refresh_from_db()
        self.assertEqual(line.asset_category.code, 'equity_index')

    def test_snapshot_service_freezes_leaf_categories_and_ipo_uses_stock_leaf(self):
        from types import SimpleNamespace
        from portfolio.snapshot_service import create_portfolio_snapshot
        from portfolio.ipo_sync import _stock_category
        seed_categories(None)
        self.security.asset_category = self.categories['equity_index']
        self.security.save()
        position = SimpleNamespace(account=self.account, security=self.security, quantity=Decimal('2'), price=Decimal('10'), price_as_of=self.day, price_source='manual', pricing_status='fresh', fx_rate=Decimal('7'), fx_rate_as_of=self.day, market_value_original=Decimal('20'), market_value=Decimal('140'), cost_original=Decimal('15'), cost=Decimal('105'))
        valuation = {'total_cash': Decimal('70'), 'total_market_value': Decimal('140'), 'total_asset': Decimal('210'), 'total_cost': Decimal('105'), 'total_pnl': Decimal('35'), 'complete': True, 'missing_rates': [], 'stale_prices': [], 'missing_prices': [], 'errors': [], 'positions': [position], 'cash_lines': [{'account_id': self.account.pk, 'currency': 'USD', 'amount': Decimal('10'), 'converted': Decimal('70'), 'fx_rate': Decimal('7')}]}
        snapshot = create_portfolio_snapshot(self.family, [self.account], self.day, 'CNY', account=self.account, valuation=valuation)
        self.assertEqual(snapshot.position_lines.get(asset_type='cash').asset_category_id, self.categories['cash_balance'].pk)
        self.assertEqual(snapshot.position_lines.get(security=self.security).asset_category_id, self.categories['equity_index'].pk)
        self.assertEqual(snapshot.total_asset, Decimal('210'))
        self.assertEqual(_stock_category(self.family), self.categories['equity_stock'])

    def test_form_rejects_primary_secondary_mismatch_and_other_family_category(self):
        data = {'member': self.member.pk, 'account': self.bank.pk, 'asset_primary': self.categories['equity'].pk, 'asset_category': self.categories['cash_balance'].pk, 'currency': 'CNY', 'original_amount': '100'}
        form = AssetBalanceEntryForm(data, family=self.family)
        self.assertFalse(form.is_valid())
        self.assertIn('asset_category', form.errors)
        other = Family.objects.create(name='其他家庭')
        seed_categories(other)
        data['asset_category'] = AssetCategory.objects.get(family=other, code='equity_stock').pk
        self.assertFalse(AssetBalanceEntryForm(data, family=self.family).is_valid())

    def test_page_uses_own_family_and_does_not_write(self):
        self.entry()
        self.client.force_login(self.user)
        before = AssetClassificationAudit.objects.count()
        response = self.client.get(reverse('ledger:asset_classification'), {'start': '2025-12-31', 'end': '2025-12-31'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '只读')
        self.assertContains(response, '权益类 → 股指基金')
        self.assertEqual(AssetClassificationAudit.objects.count(), before)
        self.member.is_active = False
        self.member.save()
        self.assertEqual(self.client.get(reverse('ledger:asset_classification')).status_code, 403)
