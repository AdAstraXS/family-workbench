import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ai_analysis", "0005_global_ai_conversation_lifecycle"),
    ]

    operations = [
        migrations.CreateModel(
            name="AiAnswerShare",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("title", models.CharField(blank=True, max_length=200, verbose_name="分享标题")),
                ("status", models.CharField(choices=[("draft", "等待预览"), ("active", "家庭可见"), ("paused", "已暂停"), ("withdrawn", "已撤回")], default="draft", max_length=20, verbose_name="状态")),
                ("answer_text_snapshot", models.TextField(verbose_name="回答副本")),
                ("evidence_snapshot", models.JSONField(blank=True, default=list, verbose_name="依据副本")),
                ("source_message_hash", models.CharField(max_length=64, verbose_name="来源回答校验值")),
                ("published_at", models.DateTimeField(blank=True, null=True, verbose_name="发布时间")),
                ("paused_at", models.DateTimeField(blank=True, null=True, verbose_name="暂停时间")),
                ("pause_reason", models.CharField(blank=True, max_length=300, verbose_name="暂停原因")),
                ("withdrawn_at", models.DateTimeField(blank=True, null=True, verbose_name="撤回时间")),
                ("family", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ai_answer_shares", to="family_core.family", verbose_name="所属家庭")),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ai_answer_shares", to="family_core.familymember", verbose_name="分享成员")),
                ("source_message", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="answer_shares", to="ai_analysis.aiconversationmessage", verbose_name="来源回答")),
            ],
            options={
                "verbose_name": "全局 AI 单条回答分享",
                "verbose_name_plural": "全局 AI 单条回答分享",
                "ordering": ["-created_at", "-pk"],
            },
        ),
        migrations.AddConstraint(
            model_name="aianswershare",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status__in", ["draft", "active", "paused"])),
                fields=("source_message",),
                name="unique_open_ai_answer_share",
            ),
        ),
    ]
