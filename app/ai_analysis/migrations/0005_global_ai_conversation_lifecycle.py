# Generated manually for the global AI v1 conversation and lifecycle foundation.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ai_analysis", "0004_add_zhipu_vision_provider"),
        ("family_core", "0008_assetcategory_code_constraint"),
    ]

    operations = [
        migrations.AddField(
            model_name="aiprovider",
            name="execution_location",
            field=models.CharField(
                choices=[("cloud", "云端"), ("local", "本地")],
                default="cloud",
                max_length=20,
                verbose_name="运行位置",
            ),
        ),
        migrations.CreateModel(
            name="AiConversation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("title", models.CharField(blank=True, max_length=200, verbose_name="标题")),
                ("financial_scope", models.CharField(choices=[("personal", "我的财务"), ("family", "全家财务")], default="personal", max_length=20, verbose_name="财务范围")),
                ("is_archived", models.BooleanField(default=False, verbose_name="已归档")),
                ("family", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ai_conversations", to="family_core.family", verbose_name="所属家庭")),
                ("member", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ai_conversations", to="family_core.familymember", verbose_name="会话所有者")),
            ],
            options={
                "verbose_name": "全局 AI 会话",
                "verbose_name_plural": "全局 AI 会话",
            },
        ),
        migrations.CreateModel(
            name="AiMemory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("visibility", models.CharField(choices=[("personal", "仅自己"), ("family", "家庭共同记录")], default="personal", max_length=20, verbose_name="可见范围")),
                ("status", models.CharField(choices=[("candidate", "待确认"), ("confirmed", "已确认"), ("rejected", "已拒绝"), ("superseded", "已被新版本替代"), ("deleted", "已删除")], default="candidate", max_length=20, verbose_name="状态")),
                ("content", models.TextField(verbose_name="内容")),
                ("source_note", models.CharField(blank=True, max_length=300, verbose_name="来源说明")),
                ("version", models.PositiveIntegerField(default=1, verbose_name="版本")),
                ("confirmed_at", models.DateTimeField(blank=True, null=True, verbose_name="确认时间")),
                ("deleted_at", models.DateTimeField(blank=True, null=True, verbose_name="删除时间")),
                ("confirmed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="confirmed_ai_memories", to="family_core.familymember", verbose_name="确认成员")),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="created_ai_memories", to="family_core.familymember", verbose_name="创建成员")),
                ("family", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ai_memories", to="family_core.family", verbose_name="所属家庭")),
                ("owner", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="ai_memories", to="family_core.familymember", verbose_name="个人记录所有者")),
                ("supersedes", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="replacement_versions", to="ai_analysis.aimemory", verbose_name="上一版本")),
            ],
            options={
                "verbose_name": "全局 AI 记忆",
                "verbose_name_plural": "全局 AI 记忆",
            },
        ),
        migrations.CreateModel(
            name="AiOutboundAuthorization",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("data_type", models.CharField(choices=[("conversation", "对话"), ("memory", "记忆"), ("knowledge", "知识正文"), ("financial", "财务数据")], max_length=30, verbose_name="数据类型")),
                ("is_allowed", models.BooleanField(default=False, verbose_name="允许发送")),
                ("family", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ai_outbound_authorizations", to="family_core.family", verbose_name="所属家庭")),
                ("member", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ai_outbound_authorizations", to="family_core.familymember", verbose_name="授权成员")),
                ("provider", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="outbound_authorizations", to="ai_analysis.aiprovider", verbose_name="服务商")),
            ],
            options={
                "verbose_name": "全局 AI 外发授权",
                "verbose_name_plural": "全局 AI 外发授权",
            },
        ),
        migrations.CreateModel(
            name="AiConversationMessage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sequence", models.PositiveIntegerField(verbose_name="顺序")),
                ("role", models.CharField(choices=[("user", "成员"), ("assistant", "助手"), ("summary", "会话摘要")], max_length=20, verbose_name="角色")),
                ("content", models.TextField(verbose_name="内容")),
                ("data_types", models.JSONField(blank=True, default=list, verbose_name="包含的数据类型")),
                ("evidence_refs", models.JSONField(blank=True, default=list, verbose_name="证据引用")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("conversation", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="messages", to="ai_analysis.aiconversation", verbose_name="所属会话")),
            ],
            options={
                "verbose_name": "全局 AI 会话消息",
                "verbose_name_plural": "全局 AI 会话消息",
                "ordering": ["sequence", "id"],
            },
        ),
        migrations.AddField(
            model_name="aianalysisrequest",
            name="conversation",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="requests", to="ai_analysis.aiconversation", verbose_name="全局 AI 会话"),
        ),
        migrations.AddField(model_name="aianalysisrequest", name="execution_token", field=models.CharField(blank=True, max_length=64, verbose_name="执行令牌")),
        migrations.AddField(model_name="aianalysisrequest", name="finished_at", field=models.DateTimeField(blank=True, null=True, verbose_name="结束时间")),
        migrations.AddField(model_name="aianalysisrequest", name="idempotency_key", field=models.CharField(blank=True, max_length=100, verbose_name="幂等键")),
        migrations.AddField(model_name="aianalysisrequest", name="request_fingerprint", field=models.CharField(blank=True, max_length=64, verbose_name="请求指纹")),
        migrations.AddField(model_name="aianalysisrequest", name="started_at", field=models.DateTimeField(blank=True, null=True, verbose_name="开始时间")),
        migrations.AlterField(
            model_name="aianalysisrequest",
            name="status",
            field=models.CharField(choices=[("pending", "等待中"), ("running", "运行中"), ("cancel_requested", "正在停止"), ("cancelled", "已停止"), ("unknown", "结果未知"), ("success", "成功"), ("failed", "失败")], default="pending", max_length=20, verbose_name="状态"),
        ),
        migrations.AddIndex(model_name="aiconversation", index=models.Index(fields=["family", "member", "is_archived", "-updated_at"], name="ai_conv_owner_state_idx")),
        migrations.AddIndex(model_name="aimemory", index=models.Index(fields=["family", "owner", "visibility", "status"], name="ai_memory_access_idx")),
        migrations.AddConstraint(
            model_name="aimemory",
            constraint=models.CheckConstraint(condition=models.Q(models.Q(("owner__isnull", False), ("visibility", "personal")), models.Q(("owner__isnull", True), ("visibility", "family")), _connector="OR"), name="ai_memory_owner_matches_visibility"),
        ),
        migrations.AddConstraint(
            model_name="aioutboundauthorization",
            constraint=models.UniqueConstraint(fields=("family", "member", "provider", "data_type"), name="unique_ai_outbound_authorization"),
        ),
        migrations.AddConstraint(
            model_name="aiconversationmessage",
            constraint=models.UniqueConstraint(fields=("conversation", "sequence"), name="unique_ai_message_sequence"),
        ),
        migrations.AddConstraint(
            model_name="aianalysisrequest",
            constraint=models.UniqueConstraint(condition=models.Q(("idempotency_key", ""), _negated=True), fields=("family", "member", "module", "idempotency_key"), name="unique_ai_request_idempotency_key"),
        ),
    ]
