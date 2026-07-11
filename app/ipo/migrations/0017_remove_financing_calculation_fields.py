from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("ipo", "0016_hkiposubscriptiontrade_created_by_and_more")]

    operations = [
        migrations.RemoveField(
            model_name="hkiposubscriptiontrade",
            name="financing_amount",
        ),
        migrations.RemoveField(
            model_name="hkiposubscriptiontrade",
            name="financing_rate",
        ),
        migrations.RemoveField(
            model_name="hkiposubscriptiontrade",
            name="financing_days",
        ),
    ]
