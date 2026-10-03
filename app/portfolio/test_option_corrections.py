from datetime import date
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from family_core.models import Currency, Family, FamilyMember
from .forms import OptionContractForm, save_option_contract
from .models import InvestmentCashMovement, InvestmentTransaction, Security, SecurityMarket, OptionContract
from .services import rebuild_position, settle_option_position
from .tests import create_broker_investment_account


class OptionStrikeCorrectionTests(TestCase):
    def setUp(self):
        Currency.objects.get_or_create(code="USD", defaults={"name": "美元"})
        SecurityMarket.objects.get_or_create(code="US", defaults={"name": "美股", "default_currency": "USD"})
        self.family = Family.objects.create(name="更正测试")
        self.user = get_user_model().objects.create_user(username="strike-editor")
        self.member = FamilyMember.objects.create(family=self.family, user=self.user, display_name="成员")
        self.account = create_broker_investment_account(self.family, self.member, "测试盈透")
        self.stock = Security.objects.create(symbol="SPCX", name="SpaceX", market="US", currency="USD")
        self.option = save_option_contract(
            member=self.member, underlying=self.stock, option_type="call", strike_price=D("153"),
            expiration_date=date(2026, 10, 2), multiplier=100, contract_symbol="SPCX 261002 155 CALL",
        )
        self.buy = self.trade(self.stock, "buy", "100", "152.5", "1.5", date(2026, 9, 25))
        rebuild_position(self.account, self.stock)
        self.open = self.trade(self.option, "sell", "1", "1.55", "2", date(2026, 9, 28), "open")
        position = rebuild_position(self.account, self.option)
        self.close, self.sale = settle_option_position(
            position, action="assignment", action_date=date(2026, 10, 2), quantity=D(1), fee=D(1), user=self.user,
        )
        self.client.force_login(self.user)

    def trade(self, security, kind, quantity, price, fee, day, effect=""):
        return InvestmentTransaction.objects.create(
            account=self.account, security=security, trade_type=kind, quantity=D(quantity), price=D(price),
            amount=D(quantity)*D(price)*security.contract_multiplier, fee=D(fee), currency="USD",
            trade_date=day, position_effect=effect,
        )

    def correct(self, **kwargs):
        values = dict(member=self.member, underlying=self.stock, option_type="call", strike_price=D("155"),
                      expiration_date=date(2026, 10, 2), multiplier=100,
                      contract_symbol=self.option.symbol, security=self.option, user=self.user)
        values.update(kwargs)
        return save_option_contract(**values)

    def payload(self):
        return dict(underlying=self.stock.pk, option_type="call", strike_price="155",
                    expiration_date="2026-10-02", multiplier="100", market="US", currency="USD",
                    contract_symbol=self.option.symbol)

    def test_edit_get_prefills_positional_none_and_shows_link_on_trade(self):
        form = OptionContractForm(None, family=self.family, instance=self.option)
        self.assertEqual(form["strike_price"].value(), D("153"))
        self.assertEqual(form["currency"].value(), "USD")
        page = self.client.get(reverse("portfolio:option_contract_edit", args=[self.option.pk]))
        self.assertEqual(page.context["form"]["strike_price"].value(), D("153"))
        page = self.client.get(reverse("portfolio:transaction_edit", args=[self.open.pk]))
        self.assertContains(page, "修改合约行权价")

    def test_post_corrects_entire_event_and_cash_without_changing_premium(self):
        count = InvestmentTransaction.objects.count()
        cash_count = InvestmentCashMovement.objects.count()
        response = self.client.post(reverse("portfolio:option_contract_edit", args=[self.option.pk]), self.payload())
        self.assertEqual(response.status_code, 302, response.context["form"].errors if response.context else None)
        self.sale.refresh_from_db(); self.close.refresh_from_db(); self.open.refresh_from_db(); self.option.refresh_from_db()
        self.assertEqual(self.option.option_contract.strike_price, D("155"))
        self.assertIn("看涨 155", self.option.name)
        self.assertEqual(self.sale.price, D("155"))
        self.assertEqual(self.sale.amount, D("15500"))
        self.assertEqual(self.sale.cash_change, D("15499"))
        self.assertEqual(self.sale.realized_pnl, D("247.5"))
        self.assertEqual(self.sale.sell_cost, D("15251.5"))
        self.assertEqual(self.sale.fee, D("1"))
        self.assertEqual(InvestmentCashMovement.objects.get(transaction=self.sale).amount, D("15499"))
        self.assertEqual(self.close.realized_pnl, D("153"))
        self.assertEqual(self.open.price, D("1.55"))
        self.assertEqual(self.sale.extra_data["option_close_transaction_id"], self.close.pk)
        self.assertEqual(self.close.extra_data["underlying_transaction_id"], self.sale.pk)
        self.assertEqual(self.sale.extra_data["strike_corrections"][0]["user_id"], self.user.pk)
        self.assertEqual(InvestmentTransaction.objects.count(), count)
        self.assertEqual(InvestmentCashMovement.objects.count(), cash_count)
        self.correct()
        self.sale.refresh_from_db()
        self.assertEqual(len(self.sale.extra_data["strike_corrections"]), 1)

    def test_broken_pair_aborts_without_partial_updates(self):
        self.sale.extra_data = {}
        self.sale.save()
        with self.assertRaises(ValidationError):
            self.correct()
        self.assertEqual(OptionContract.objects.get(security=self.option).strike_price, D("153"))
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.price, D("153"))

    def test_inconsistent_settlement_is_shown_as_form_error(self):
        self.sale.amount = D("15200")
        self.sale.save()
        response = self.client.post(reverse("portfolio:option_contract_edit", args=[self.option.pk]), self.payload())
        self.assertEqual(response.status_code, 200)
        self.assertIn("关联流水不一致", str(response.context["form"].errors))
        self.assertEqual(OptionContract.objects.get(security=self.option).strike_price, D("153"))

    def test_other_family_trade_blocks_global_contract_correction(self):
        family = Family.objects.create(name="其他家庭")
        member = FamilyMember.objects.create(family=family, display_name="其他成员")
        account = create_broker_investment_account(family, member, "其他券商")
        InvestmentTransaction.objects.create(account=account, security=self.option, trade_date=date(2026, 9, 28),
                                             trade_type="buy", quantity=1, price=1, amount=100, currency="USD")
        with self.assertRaisesMessage(ValidationError, "其他家庭"):
            self.correct()
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.price, D("153"))

    def test_used_contract_cannot_change_multiplier_or_expiration(self):
        for change in [dict(multiplier=10), dict(expiration_date=date(2026, 10, 9))]:
            with self.subTest(change=change), self.assertRaisesMessage(ValidationError, "已有历史交易"):
                self.correct(**change)

    def test_duplicate_symbol_rolls_back_cash_correction(self):
        Security.objects.create(symbol="EXISTS", market="US", name="重复")
        with self.assertRaises(ValidationError):
            self.correct(contract_symbol="EXISTS")
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.price, D("153"))
        self.assertNotIn("strike_corrections", self.sale.extra_data)
