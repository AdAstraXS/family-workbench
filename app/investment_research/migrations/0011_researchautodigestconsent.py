from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("investment_research", "0010_futufinancialsnapshot"),
        ("ai_analysis", "0011_module_model_defaults"),
    ]

    operations = [
        migrations.CreateModel(
            name="ResearchAutoDigestConsent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("authorized_at", models.DateTimeField(default=django.utils.timezone.now, verbose_name="授权时间")),
                ("revoked_at", models.DateTimeField(blank=True, null=True, verbose_name="关闭时间")),
                ("authorized_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="research_auto_digest_consents", to="family_core.familymember", verbose_name="授权成员")),
                ("dossier", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="auto_digest_consent", to="investment_research.researchdossier", verbose_name="研究档案")),
                ("provider", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="research_auto_digest_consents", to="ai_analysis.aiprovider", verbose_name="授权的文本模型")),
            ],
            options={"verbose_name": "次日跟踪自动对照授权", "verbose_name_plural": "次日跟踪自动对照授权"},
        ),
    ]
