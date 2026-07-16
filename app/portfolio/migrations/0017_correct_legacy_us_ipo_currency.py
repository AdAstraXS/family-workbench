from decimal import Decimal, ROUND_HALF_UP

from django.db import migrations


RATE = Decimal("7.8")
MONEY = Decimal("0.0001")
PRICE = Decimal("0.000001")
TARGETS = {
    ("老虎证券", "CHA"),
    ("老虎证券", "FIGR"),
    ("老虎证券", "BTGO"),
    ("富途证券", "FIGR"),
    ("富途证券", "GEMI"),
    ("富途证券", "BTGO"),
}


def converted(value, precision=MONEY):
    return (value / RATE).quantize(precision, rounding=ROUND_HALF_UP)


def correct_legacy_us_ipo_transactions(apps, schema_editor):
    CashMovement = apps.get_model("portfolio", "InvestmentCashMovement")
    Position = apps.get_model("portfolio", "InvestmentPosition")
    Transaction = apps.get_model("portfolio", "InvestmentTransaction")

    transactions = Transaction.objects.filter(
        source="ipo",
        ipo_subscription_trade__member__display_name="我",
        account__bank_account__account_name__in={name for name, _ in TARGETS},
        security__symbol__in={symbol for _, symbol in TARGETS},
        security__market="US",
    ).select_related("account__bank_account", "security")

    pairs = set()
    for item in transactions:
        if (item.account.bank_account.account_name, item.security.symbol) not in TARGETS:
            continue
        if (item.extra_data or {}).get("legacy_hkd_to_usd_rate"):
            continue
        item.price = converted(item.price, PRICE)
        for field in ("amount", "fee", "tax", "cash_change", "sell_cost", "realized_pnl"):
            setattr(item, field, converted(getattr(item, field)))
        item.extra_data = {
            **(item.extra_data or {}),
            "legacy_hkd_to_usd_rate": str(RATE),
        }
        item.save(
            update_fields=[
                "price",
                "amount",
                "fee",
                "tax",
                "cash_change",
                "sell_cost",
                "realized_pnl",
                "extra_data",
            ]
        )
        CashMovement.objects.filter(transaction_id=item.pk).update(
            amount=item.cash_change,
            currency="USD",
        )
        pairs.add((item.account_id, item.security_id))

    for account_id, security_id in pairs:
        realized = sum(
            Transaction.objects.filter(
                account_id=account_id,
                security_id=security_id,
            ).values_list("realized_pnl", flat=True),
            Decimal("0"),
        )
        position = Position.objects.filter(
            account_id=account_id,
            security_id=security_id,
            quantity=0,
        ).first()
        if position:
            position.current_price = converted(position.current_price, PRICE)
            position.realized_pnl = realized
            position.save(update_fields=["current_price", "realized_pnl"])


class Migration(migrations.Migration):
    dependencies = [("portfolio", "0016_backfill_security_asset_categories")]

    operations = [
        migrations.RunPython(
            correct_legacy_us_ipo_transactions,
            migrations.RunPython.noop,
        )
    ]
