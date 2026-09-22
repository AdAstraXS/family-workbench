import django.db.models.deletion
from django.db import migrations, models


def seed_watchlist(apps, schema_editor):
    Policy = apps.get_model("option_wheel", "WheelPolicy")
    Watch = apps.get_model("option_wheel", "WheelWatchItem")
    defaults = ("TSLA", "MSFT", "AAPL", "AMZN", "GOOG", "GOOGL", "META", "NVDA", "TSM", "ASML", "AMD", "INTC", "MU", "SKHY", "SPCX")
    for family_id in Policy.objects.values_list("family_id", flat=True).distinct():
        for symbol in defaults:
            Watch.objects.get_or_create(family_id=family_id, symbol=symbol)


class Migration(migrations.Migration):
    dependencies = [("option_wheel", "0010_wheelcandidate_warning_reasons")]

    operations = [
        migrations.AddField(
            model_name="wheelanalysisjob",
            name="screening_results",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.CreateModel(
            name="WheelWatchItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("symbol", models.CharField(max_length=12)),
                ("name", models.CharField(blank=True, max_length=100)),
                ("price", models.DecimalField(blank=True, decimal_places=6, max_digits=20, null=True)),
                ("price_as_of", models.DateTimeField(blank=True, null=True)),
                ("next_earnings", models.DateField(blank=True, null=True)),
                ("next_dividend", models.DateField(blank=True, null=True)),
                ("events_checked_at", models.DateTimeField(blank=True, null=True)),
                ("events_covered_until", models.DateField(blank=True, null=True)),
                ("family", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="wheel_watch_items", to="family_core.family")),
            ],
            options={
                "ordering": ("symbol",),
                "constraints": [models.UniqueConstraint(fields=("family", "symbol"), name="wheel_watch_family_symbol_unique")],
            },
        ),
        migrations.RunPython(seed_watchlist, migrations.RunPython.noop),
    ]
