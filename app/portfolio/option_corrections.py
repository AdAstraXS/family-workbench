"""Correct an option strike together with its already generated settlements."""
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import (
    InvestmentTransaction, OptionContract, TradeStatusChoices, TradeTypeChoices,
    TransactionSourceChoices,
)
from .services import lock_accounts, rebuild_position


def correct_settlement_prices(contract, new_strike, *, family, user=None):
    """Caller holds the contract lock in an atomic transaction.

    Keep both settlement legs and their IDs. Reject ambiguous links instead of
    editing one half of an event or guessing an independently changed price.
    """
    if new_strike == contract.strike_price:
        return
    facts = InvestmentTransaction.objects.filter(security_id=contract.security_id)
    if facts.exclude(account__bank_account__family=family).exists():
        raise ValidationError("此合约还有其他家庭的交易，不能在当前家庭更正行权价。")
    lock_accounts(facts.values_list("account_id", flat=True))
    closes = list(facts.filter(extra_data__option_action__in=["exercise", "assignment"]))
    pairs = []
    for close in closes:
        extra = close.extra_data or {}
        action = extra["option_action"]
        linked_id = extra.get("underlying_transaction_id")
        linked = InvestmentTransaction.objects.filter(pk=linked_id).first()
        expected_type = (
            TradeTypeChoices.BUY
            if (action == "exercise") == (contract.option_type == OptionContract.CALL)
            else TradeTypeChoices.SELL
        )
        expected_close_type = TradeTypeChoices.SELL if action == "exercise" else TradeTypeChoices.BUY
        if not linked or any([
            extra.get("option_contract_id") != contract.pk,
            (linked.extra_data or {}).get("option_close_transaction_id") != close.pk,
            (linked.extra_data or {}).get("option_action") != action,
            linked.account_id != close.account_id,
            linked.security_id != contract.underlying_id,
            linked.trade_date != close.trade_date,
            linked.trade_type != expected_type,
            close.trade_type != expected_close_type,
            close.position_effect != InvestmentTransaction.EFFECT_CLOSE,
            close.status != TradeStatusChoices.COMPLETED,
            linked.status != TradeStatusChoices.COMPLETED,
            close.source != TransactionSourceChoices.MANUAL,
            linked.source != TransactionSourceChoices.MANUAL,
            close.price != 0,
            close.amount != 0,
            linked.currency != contract.underlying.currency,
            close.quantity <= 0,
            linked.quantity != close.quantity * contract.multiplier,
            linked.price != contract.strike_price,
            linked.amount != linked.quantity * contract.strike_price,
        ]):
            raise ValidationError(f"期权结算 #{close.pk} 的关联流水不一致，请先核对完整事件。")
        pairs.append((close, linked))
    # Catch a one-way underlying link which is absent from the close leg.
    reverse_ids = set(InvestmentTransaction.objects.filter(
        extra_data__option_close_transaction_id__in=[item.pk for item in closes],
    ).values_list("pk", flat=True))
    if reverse_ids != {linked.pk for _, linked in pairs}:
        raise ValidationError("期权结算存在重复或不完整的关联流水，不能自动更正。")
    changed_accounts = {}
    for close, linked in pairs:
        record = {
            "at": timezone.now().isoformat(),
            "user_id": getattr(user, "pk", None),
            "old_strike": str(contract.strike_price),
            "new_strike": str(new_strike),
            "option_contract_id": contract.pk,
        }
        for item in (close, linked):
            history = list((item.extra_data or {}).get("strike_corrections", []))
            item.extra_data = {**(item.extra_data or {}), "strike_corrections": history + [record]}
            if user:
                item.updated_by = user
        linked.price = new_strike
        linked.amount = linked.quantity * new_strike
        linked.save(update_fields=["price", "amount", "extra_data", "updated_by", "updated_at"])
        close.save(update_fields=["extra_data", "updated_by", "updated_at"])
        changed_accounts[linked.account_id] = linked.account
    for account in changed_accounts.values():
        rebuild_position(account, contract.underlying)
