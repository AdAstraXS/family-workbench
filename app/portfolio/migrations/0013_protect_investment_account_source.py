import django.db.models.deletion

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("portfolio", "0012_finalize_account_and_snapshot_schema"),
    ]

    operations = [
        migrations.AlterField(
            model_name="investmentaccount",
            name="bank_account",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="investment_profile",
                to="ledger.bankaccount",
                verbose_name="关联账户",
            ),
        ),
    ]
