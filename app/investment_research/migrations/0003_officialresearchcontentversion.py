import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("investment_research", "0002_officialresearchdocument_researchsourcestate")]

    operations = [
        migrations.CreateModel(
            name="OfficialResearchContentVersion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("version_number", models.PositiveIntegerField(verbose_name="版本号")),
                ("source_url", models.URLField(max_length=1000, verbose_name="获取时来源链接")),
                ("raw_sha256", models.CharField(max_length=64, verbose_name="原始响应 SHA-256")),
                ("raw_gzip", models.BinaryField(verbose_name="原始 HTML（gzip）")),
                ("content_text", models.TextField(verbose_name="规范正文")),
                ("content_sha256", models.CharField(max_length=64, verbose_name="规范正文 SHA-256")),
                ("extractor_version", models.CharField(max_length=32, verbose_name="提取器版本")),
                ("fetched_at", models.DateTimeField(verbose_name="获取时间")),
                ("document", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="content_versions", to="investment_research.officialresearchdocument", verbose_name="官方资料")),
            ],
            options={
                "verbose_name": "官方资料正文版本",
                "verbose_name_plural": "官方资料正文版本",
                "ordering": ["-version_number"],
                "constraints": [models.UniqueConstraint(fields=("document", "version_number"), name="unique_research_content_version_number")],
            },
        ),
    ]
