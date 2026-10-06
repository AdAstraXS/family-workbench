from decimal import Decimal
from datetime import date
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from .models import Family, FamilyMember, AssetCategory
from .asset_classification import seed_categories
from .asset_category_management import AssetCategoryManagementForm
from .classification_preview import propose_security, build_classification_preview
from ledger.models import AssetBalanceSnapshot, AssetBalanceEntry
from ledger.forms import AssetBalanceEntryForm
from portfolio.models import Security, WatchlistItem
from portfolio.forms import SecurityForm, InvestmentTransactionForm


class AssetCategoryManagementTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name='管理家庭')
        self.user = get_user_model().objects.create_user(username='dictionary-manager')
        self.member = FamilyMember.objects.create(family=self.family,user=self.user,display_name='管理员',role='admin')
        seed_categories(self.family)
        self.categories = {c.code:c for c in AssetCategory.objects.filter(family=self.family)}
        self.client.force_login(self.user)

    def data(self, name, **changes):
        return {'level':'secondary','name':name,'parent':self.categories['equity'].pk,
            'display_order':'20','is_active':'on','remark':'',**changes}

    def create(self,name,**changes):
        response = self.client.post(reverse('ledger:asset_category_create'), self.data(name,**changes))
        self.assertEqual(response.status_code,302)
        return AssetCategory.objects.get(family=self.family,name=name)

    def test_basics_page_lists_only_own_asset_dictionary_without_writing(self):
        other = Family.objects.create(name='其他家庭')
        AssetCategory.objects.create(family=other,name='其他家庭保密类别',code='other')
        before = list(AssetCategory.objects.values())
        response = self.client.get(reverse('ledger:category_list'))
        self.assertContains(response,'新增资产类别')
        self.assertContains(response,'短期国债')
        self.assertNotContains(response,'其他家庭保密类别')
        self.assertEqual(before,list(AssetCategory.objects.values()))

    def test_member_and_other_family_cannot_write_dictionary(self):
        self.member.role='member'
        self.member.save()
        self.assertEqual(self.client.post(reverse('ledger:asset_category_create'),self.data('禁止新增')).status_code,403)
        self.member.role='admin'
        self.member.save()
        other=AssetCategory.objects.create(family=Family.objects.create(name='其他家庭'),name='其他',code='other')
        self.assertEqual(self.client.get(reverse('ledger:asset_category_edit',args=[other.pk])).status_code,404)

    def test_custom_primary_and_secondary_appear_in_both_modules_and_options(self):
        primary=self.create('新增一级',level='primary',parent='')
        secondary=self.create('新增二级',parent=primary.pk)
        self.assertTrue(primary.is_classification_primary)
        ledger_form=AssetBalanceEntryForm(family=self.family)
        security_form=SecurityForm(family=self.family)
        for form in (ledger_form,security_form):
            self.assertIn(primary,form.fields['asset_primary'].queryset)
            self.assertIn(secondary,form.fields['asset_category'].queryset)
        response=self.client.get(reverse('portfolio:transaction_form_options'),{'family':self.family.pk})
        options={r['id']:r for r in response.json()['categories']}
        self.assertTrue(options[primary.pk]['is_primary_choice'])
        self.assertIn('etf',options[secondary.pk]['instrument_types'])
        security=Security(symbol='CUSTOM',name='自定义',asset_type='etf',asset_category=secondary)
        self.assertEqual(propose_security(security)[0],secondary.code)

    def test_referenced_leaf_cannot_be_reparented_and_values_stay_unchanged(self):
        secondary=self.create('有历史引用')
        snapshot=AssetBalanceSnapshot.objects.create(family=self.family,snapshot_date=date(2025,12,31))
        entry=AssetBalanceEntry.objects.create(snapshot=snapshot,member=self.member,asset_category=secondary,
            original_amount=Decimal('123.4567'),base_amount=Decimal('900.1234'))
        before=list(AssetBalanceEntry.objects.values())
        response=self.client.post(reverse('ledger:asset_category_edit',args=[secondary.pk]),self.data('有历史引用',parent=self.categories['cash'].pk))
        self.assertContains(response,'不能改变层级或所属一级')
        secondary.refresh_from_db()
        self.assertEqual(secondary.parent_id,self.categories['equity'].pk)
        self.assertEqual(before,list(AssetBalanceEntry.objects.values()))
        self.assertEqual(entry.original_amount,Decimal('123.4567'))

    def test_rename_and_deactivate_keep_stable_code_and_history(self):
        secondary=self.create('原名称')
        code=secondary.code
        response=self.client.post(reverse('ledger:asset_category_edit',args=[secondary.pk]),self.data('新名称',is_active=''))
        self.assertEqual(response.status_code,302)
        secondary.refresh_from_db()
        self.assertEqual(secondary.code,code)
        self.assertFalse(secondary.is_active)
        self.assertEqual(secondary.extra_data['classification_changes'][-1]['before']['name'],'原名称')
        self.assertNotIn(secondary,AssetBalanceEntryForm(family=self.family).fields['asset_category'].queryset)
        sec=Security(symbol='OLD',asset_category=secondary)
        self.assertIn(secondary,SecurityForm(instance=sec,family=self.family).fields['asset_category'].queryset)

    def test_stopped_primary_hides_children_for_new_rows_but_keeps_current_leaf(self):
        root=self.create('停用一级',level='primary',parent='')
        leaf=self.create('停用一级下的二级',parent=root.pk)
        root.is_active=False
        root.save()
        self.assertNotIn(leaf,AssetBalanceEntryForm(family=self.family).fields['asset_category'].queryset)
        sec=Security(symbol='OLD',asset_category=leaf)
        form=SecurityForm(instance=sec,family=self.family)
        self.assertIn(root,form.fields['asset_primary'].queryset)
        self.assertIn(leaf,form.fields['asset_category'].queryset)

    def test_invalid_hierarchy_duplicate_and_cross_family_are_rejected(self):
        other=AssetCategory.objects.create(family=Family.objects.create(name='外部'),name='外部一级',code='external')
        for changes in ({'parent':other.pk},{'parent':self.categories['equity_stock'].pk},{'level':'primary'},{'name':'股票'}):
            form=AssetCategoryManagementForm({**self.data('错误分类'),**changes},family=self.family)
            self.assertFalse(form.is_valid(),changes)

    def test_legacy_root_is_read_only(self):
        old=AssetCategory.objects.create(family=self.family,name='旧基金类',code='fund')
        self.assertEqual(self.client.get(reverse('ledger:asset_category_edit',args=[old.pk])).status_code,403)

    def test_new_transaction_can_keep_retired_category_from_accessible_security_only(self):
        root=self.create('历史一级',level='primary',parent='')
        leaf=self.create('历史二级',parent=root.pk)
        root.is_active=False
        root.save()
        security=Security.objects.create(symbol='OLDMANAGED',name='历史标的',asset_type='etf',asset_category=leaf)
        WatchlistItem.objects.create(family=self.family,security=security)
        form=InvestmentTransactionForm({'family':self.family.pk,'security':security.pk,
            'asset_primary':root.pk,'asset_category':leaf.pk},user=self.user)
        self.assertIn(leaf,form.fields['asset_category'].queryset)
        self.assertIn(root,form.fields['asset_primary'].queryset)
        foreign=Security.objects.create(symbol='NOTVISIBLE',name='无权限标的',asset_type='etf',asset_category=leaf)
        denied=InvestmentTransactionForm({'family':self.family.pk,'security':foreign.pk,
            'asset_primary':root.pk,'asset_category':leaf.pk},user=self.user)
        self.assertNotIn(leaf,denied.fields['asset_category'].queryset)

    def test_builtin_category_cannot_change_parent_and_form_returns_to_basics(self):
        leaf=self.categories['gold']
        response=self.client.post(reverse('ledger:asset_category_edit',args=[leaf.pk]),self.data('黄金'))
        self.assertContains(response,'不能改变层级或所属一级')
        leaf.refresh_from_db()
        self.assertEqual(leaf.parent_id,self.categories['commodities'].pk)
        page=self.client.get(reverse('ledger:asset_category_create'))
        self.assertEqual(page.context['page_parent_url'],reverse('ledger:category_list'))

    def test_history_preview_keeps_retired_category_but_never_moves_into_it(self):
        leaf=self.categories['equity_index']
        old=Security.objects.create(symbol='VOO',name='旧标的',asset_type='etf',asset_category=leaf)
        pending=Security.objects.create(symbol='SPY',name='待映射',asset_type='etf',asset_category=self.categories['equity'])
        for security in (old,pending):
            WatchlistItem.objects.create(family=self.family,security=security)
        leaf.is_active=False
        leaf.save()
        report=build_classification_preview(self.family,date(2024,1,1),date(2026,10,6))
        rows={row['id']:row for row in report['rows']}
        self.assertEqual(rows[old.pk]['status'],'unchanged')
        self.assertEqual(rows[pending.pk]['status'],'unresolved')
