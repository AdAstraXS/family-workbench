import django.db.models.deletion
import uuid

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("option_wheel", "0013_wheelputquotejob"),
        ("portfolio", "0029_investmenttransaction_option_purpose"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="WheelPositionScanJob",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("target_expiration", models.DateField()),
                ("status", models.CharField(choices=[("queued", "等待启动"), ("running", "查询与清理中"), ("saved", "已保存"), ("failed", "未保存"), ("interrupted", "运行已中断")], default="queued", max_length=16)),
                ("result", models.JSONField(default=dict)),
                ("message", models.TextField(blank=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("expires_at", models.DateTimeField()),
                ("family", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="family_core.family")),
                ("position", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="portfolio.investmentposition")),
                ("requested_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ],
            options={"verbose_name": "期权持仓调整分析任务", "verbose_name_plural": "期权持仓调整分析任务", "ordering": ["-created_at"]},
        ),
    ]
