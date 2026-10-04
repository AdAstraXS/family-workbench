from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from family_core.models import Family, FamilyMember
from .historical_valuation import HistoricalPositionValue, _apply_prices
from .models import BondDetail, HistoricalValuationPrice, PricingStatusChoices, Security, PortfolioSnapshot
from .snapshot_service import create_portfolio_snapshots_for_date
from .tests import create_broker_investment_account
from .models import InvestmentTransaction, TradeTypeChoices


class HistoricalPriceReviewTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name="价格确认测试")
        self.member = FamilyMember.objects.create(family=self.family, display_name="成员")
        self.account = create_broker_investment_account(self.family, self.member, "停牌账户")
        self.other = create_broker_investment_account(self.family, self.member, "其他账户")
        self.security = Security.objects.create(symbol="HALT", name="停牌股票", market="CN", currency="CNY", asset_type=Security.TYPE_STOCK)
        self.user = get_user_model().objects.create_user(username="reviewer")
        self.day = date(2025, 12, 31)
        self.review = HistoricalValuationPrice.objects.create(
            account=self.account, security=self.security, valuation_date=self.day,
            quote_date=date(2025, 11, 4), price=Decimal("13.3"), currency="CNY",
            basis="suspended_close", evidence="成员确认停牌前收盘价，保留实际报价日期。", confirmed_by=self.user,
        )

    def position(self, account=None):
        return HistoricalPositionValue(account or self.account, self.security, Decimal("5000"), Decimal("50000"))

    def test_confirmation_scoped_to_exact_account_and_date(self):
        accepted, other = self.position(), self.position(self.other)
        _apply_prices([accepted, other], self.day)
        self.assertEqual(accepted.price, Decimal("13.3"))
        self.assertEqual(accepted.price_as_of, date(2025, 11, 4))
        self.assertEqual(accepted.pricing_status, PricingStatusChoices.MANUAL)
        self.assertIsNone(other.price)
        tomorrow = self.position()
        _apply_prices([tomorrow], date(2026, 1, 1))
        self.assertIsNone(tomorrow.price)

    def test_invalid_confirmation_does_not_suppress_validation(self):
        for field, value in [("currency", "USD"), ("quote_date", date(2026, 1, 1)), ("price", Decimal("0")), ("basis", "bond_clean"), ("evidence", " ")]:
            original = getattr(self.review, field)
            setattr(self.review, field, value)
            with self.assertRaises(ValidationError):
                self.review.full_clean()
            setattr(self.review, field, original)

    def test_snapshot_preserves_quote_date_and_confirmation_and_is_idempotent(self):
        InvestmentTransaction.objects.create(account=self.account, security=self.security, trade_date=date(2025, 11, 1), trade_type=TradeTypeChoices.BUY, quantity=Decimal("5000"), price=Decimal("10"), amount=Decimal("50000"), currency="CNY")
        for _ in range(2):
            snapshots = create_portfolio_snapshots_for_date(self.family, [self.account], self.day, "CNY", require_complete=True)
        snapshot = next(s for s in snapshots if s.account_id is None and s.member_id is None)
        line = snapshot.position_lines.get(security=self.security)
        self.assertEqual(line.market_value, Decimal("66500"))
        self.assertEqual(line.price_as_of, date(2025, 11, 4))
        self.assertTrue(snapshot.extra_data["complete"])
        self.assertEqual(snapshot.extra_data["historical_price_reviews"][0]["id"], self.review.pk)
        self.assertEqual(PortfolioSnapshot.objects.filter(account=None, member=None, snapshot_date=self.day).count(), 1)

    def test_bond_clean_price_uses_face_value_divided_by_100(self):
        self.security.asset_type = Security.TYPE_BOND
        self.security.save()
        BondDetail.objects.create(security=self.security, quote_basis=BondDetail.PER_100, accrued_interest=Decimal("1.5"))
        self.review.basis = "bond_clean"
        self.review.price = Decimal("99.6875")
        self.review.save()
        InvestmentTransaction.objects.create(account=self.account, security=self.security, trade_date=date(2025, 11, 1), trade_type=TradeTypeChoices.BUY, quantity=Decimal("5000"), price=Decimal("99"), amount=Decimal("4950"), currency="CNY")
        snapshots = create_portfolio_snapshots_for_date(self.family, [self.account], self.day, "CNY", require_complete=True)
        snapshot = next(s for s in snapshots if s.account_id is None and s.member_id is None)
        self.assertEqual(snapshot.position_lines.get(security=self.security).market_value_original, Decimal("4984.375"))

    def test_option_cannot_be_confirmed_as_stock_price(self):
        self.security.asset_type = Security.TYPE_OPTION
        self.security.save()
        with self.assertRaises(ValidationError):
            self.review.full_clean()
