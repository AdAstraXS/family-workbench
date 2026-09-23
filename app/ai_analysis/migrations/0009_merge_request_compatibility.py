"""Join the existing global AI migrations with request-column compatibility."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("ai_analysis", "0005_reconcile_request_columns"),
        ("ai_analysis", "0008_aifamilyoutboundauthorization"),
    ]

    operations = []
