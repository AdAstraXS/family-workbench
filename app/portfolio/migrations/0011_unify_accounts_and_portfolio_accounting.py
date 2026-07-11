import django.db.models.deletion

from django.db import migrations, models
from django.db.models import Count, Q


def migrate_account_and_position_data(apps, schema_editor):
    AccountType = apps.get_model("family_core", "AccountType")
    BankAccount = apps.get_model("ledger", "BankAccount")
    HkIpoSubscriptionTrade = apps.get_model("ipo", "HkIpoSubscriptionTrade")
    InvestmentAccount = apps.get_model("portfolio", "InvestmentAccount")
    InvestmentPosition = apps.get_model("portfolio", "InvestmentPosition")
    InvestmentCashMovement = apps.get_model("portfolio", "InvestmentCashMovement")
    InvestmentTransaction = apps.get_model("portfolio", "InvestmentTransaction")
    PortfolioSnapshot = apps.get_model("portfolio", "PortfolioSnapshot")

    linked_bank_ids = set(
        InvestmentAccount.objects.exclude(bank_account_id=None).values_list(
            "bank_account_id", flat=True
        )
    )
    for account in InvestmentAccount.objects.filter(bank_account_id=None).order_by("pk"):
        bank_account = (
            BankAccount.objects.filter(
                family_id=account.family_id,
                member_id=account.member_id,
                account_name=account.account_name,
            )
            .exclude(pk__in=linked_bank_ids)
            .first()
        )
        if not bank_account:
            account_type = (
                AccountType.objects.filter(
                    Q(family_id=account.family_id) | Q(family_id=None),
                    code="broker",
                )
                .order_by("-family_id", "pk")
                .first()
            )
            if not account_type:
                account_type = AccountType.objects.create(
                    family_id=account.family_id,
                    name="券商",
                    code="broker",
                )
            bank_account = BankAccount.objects.create(
                family_id=account.family_id,
                member_id=account.member_id,
                account_name=account.account_name,
                account_no_masked=account.account_no_masked,
                account_type_ref_id=account_type.pk,
                account_region_id=account.account_region_id,
                supports_investment=True,
                supports_ipo="打新账户" in (account.remark or ""),
                is_active=account.is_active,
                remark=account.remark,
            )
        elif not bank_account.supports_investment:
            bank_account.supports_investment = True
            bank_account.save(update_fields=["supports_investment"])
        account.bank_account_id = bank_account.pk
        account.save(update_fields=["bank_account"])
        linked_bank_ids.add(bank_account.pk)

    duplicate_positions = (
        InvestmentPosition.objects.values("account_id", "security_id")
        .annotate(row_count=Count("id"))
        .filter(row_count__gt=1)
    )
    for group in duplicate_positions:
        rows = InvestmentPosition.objects.filter(
            account_id=group["account_id"],
            security_id=group["security_id"],
        ).order_by("-position_date", "-updated_at", "-pk")
        keep = rows.first()
        rows.exclude(pk=keep.pk).delete()

    duplicate_snapshots = (
        PortfolioSnapshot.objects.values(
            "family_id", "member_id", "account_id", "snapshot_date", "currency"
        )
        .annotate(row_count=Count("id"))
        .filter(row_count__gt=1)
    )
    for group in duplicate_snapshots:
        rows = PortfolioSnapshot.objects.filter(
            family_id=group["family_id"],
            member_id=group["member_id"],
            account_id=group["account_id"],
            snapshot_date=group["snapshot_date"],
            currency=group["currency"],
        ).order_by("-pk")
        keep = rows.first()
        rows.exclude(pk=keep.pk).delete()

    valid_ipo_ids = set(HkIpoSubscriptionTrade.objects.values_list("pk", flat=True))
    for item in InvestmentTransaction.objects.filter(external_id__startswith="ipo:"):
        try:
            ipo_trade_id = int(item.external_id.split(":", 2)[1])
        except (IndexError, TypeError, ValueError):
            continue
        if ipo_trade_id not in valid_ipo_ids:
            continue
        item.ipo_subscription_trade_id = ipo_trade_id
        item.source = "ipo"
        item.extra_data = {}
        item.save(update_fields=["ipo_subscription_trade", "source", "extra_data"])
        InvestmentCashMovement.objects.filter(transaction_id=item.pk).update(
            source="ipo",
            external_id=item.external_id,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("ipo", "0016_hkiposubscriptiontrade_created_by_and_more"),
        ("ledger", "0009_bankaccount_supports_investment_and_more"),
        ("portfolio", "0010_investmentcashmovement_created_by_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="investmenttransaction",
            name="ipo_subscription_trade",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="investment_transactions",
                to="ipo.hkiposubscriptiontrade",
                verbose_name="港股打新申购",
            ),
        ),
        migrations.AlterField(
            model_name="investmenttransaction",
            name="source",
            field=models.CharField(
                choices=[
                    ("manual", "手工录入"),
                    ("import", "文件导入"),
                    ("futu", "Futu 同步"),
                    ("ipo", "港股打新"),
                ],
                default="manual",
                max_length=20,
                verbose_name="数据来源",
            ),
        ),
        migrations.AlterField(
            model_name="investmentcashmovement",
            name="source",
            field=models.CharField(
                choices=[
                    ("manual", "手工录入"),
                    ("import", "文件导入"),
                    ("futu", "Futu 同步"),
                    ("ipo", "港股打新"),
                ],
                default="manual",
                max_length=20,
                verbose_name="数据来源",
            ),
        ),
        migrations.RunPython(
            migrate_account_and_position_data,
            migrations.RunPython.noop,
        ),
    ]
