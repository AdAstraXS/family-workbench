from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("family_core", "0005_accounttype_code_and_more"),
        ("ledger", "0009_bankaccount_supports_investment_and_more"),
        ("portfolio", "0015_bonddetail"),
    ]

    operations = [
        migrations.AddField(
            model_name="assetcategory",
            name="code",
            field=models.SlugField(blank=True, max_length=50, verbose_name="稳定代码"),
        ),
    ]
