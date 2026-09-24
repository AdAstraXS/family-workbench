from django.db import migrations, models


def preserve_microsoft_focus(apps, schema_editor):
    Dossier = apps.get_model("investment_research", "ResearchDossier")
    for dossier in Dossier.objects.filter(security__symbol="MSFT", security__market="US"):
        dossier.selected_metric_codes = [
            "depreciation", "finance_rou_add", "finance_principal",
            "finance_liability", "finance_rou_asset", "company_revenue",
            "uncommenced_lease",
        ]
        dossier.save(update_fields=["selected_metric_codes"])


class Migration(migrations.Migration):
    dependencies = [("investment_research", "0004_exploratory_dossier")]

    operations = [
        migrations.AddField(
            model_name="researchdossier", name="selected_metric_codes",
            field=models.JSONField(blank=True, default=list, verbose_name="已确认追踪指标"),
        ),
        migrations.RunPython(preserve_microsoft_focus, migrations.RunPython.noop),
    ]
