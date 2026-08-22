from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("intelligence", "0007_add_intelligence_digest"),
    ]

    operations = [
        migrations.AddField(
            model_name="sourceitem",
            name="article_content_hash",
            field=models.CharField(blank=True, db_index=True, max_length=64, verbose_name="公开网页内容指纹"),
        ),
        migrations.AddField(
            model_name="sourceitem",
            name="article_evidence",
            field=models.TextField(blank=True, help_text="只保存自动整理所需的少量公开段落，不保存完整版权正文。", verbose_name="公开网页证据摘录"),
        ),
        migrations.AddField(
            model_name="sourceitem",
            name="article_extraction_version",
            field=models.CharField(blank=True, max_length=50, verbose_name="网页提取器版本"),
        ),
        migrations.AddField(
            model_name="sourceitem",
            name="article_fetch_reason",
            field=models.CharField(blank=True, max_length=500, verbose_name="网页提取说明"),
        ),
        migrations.AddField(
            model_name="sourceitem",
            name="article_fetch_status",
            field=models.CharField(
                choices=[
                    ("not_requested", "未请求"),
                    ("extracted", "已提取公开证据"),
                    ("metadata_only", "仅保留元数据"),
                    ("blocked", "访问受限"),
                    ("failed", "提取失败"),
                ],
                default="not_requested",
                max_length=20,
                verbose_name="网页提取状态",
            ),
        ),
        migrations.AddField(
            model_name="sourceitem",
            name="article_fetched_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="网页提取时间"),
        ),
        migrations.AlterField(
            model_name="sourceitem",
            name="content_depth",
            field=models.CharField(
                choices=[
                    ("title", "仅标题"),
                    ("description", "标题与简介"),
                    ("public_article", "公开网页证据摘录"),
                    ("official_article", "官方文章正文"),
                    ("transcript", "完整字幕/文字稿"),
                    ("manual", "人工核查"),
                ],
                default="title",
                max_length=30,
                verbose_name="内容深度",
            ),
        ),
        migrations.AlterField(
            model_name="intelligenceevent",
            name="review_status",
            field=models.CharField(
                choices=[
                    ("published", "已发布"),
                    ("ai_published", "AI 自动发布（未人工复核）"),
                    ("pending", "待复核"),
                    ("reviewed", "已复核"),
                    ("ignored", "已忽略"),
                ],
                default="published",
                max_length=20,
                verbose_name="复核状态",
            ),
        ),
        migrations.AlterField(
            model_name="collectionrun",
            name="run_kind",
            field=models.CharField(
                choices=[
                    ("collection", "来源采集"),
                    ("processing", "条目处理"),
                    ("digest", "简报生成"),
                    ("automation", "自动情报循环"),
                    ("manual", "人工录入"),
                ],
                max_length=20,
                verbose_name="运行类型",
            ),
        ),
    ]
