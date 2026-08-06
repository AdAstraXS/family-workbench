from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("notes", "0003_investmentnotetype"),
    ]

    operations = [
        migrations.AddField(
            model_name="investmentnote",
            name="include_in_knowledge",
            field=models.BooleanField(
                default=False,
                help_text="勾选后才会在家庭知识中心建立可重建的搜索投影。",
                verbose_name="加入家庭知识库",
            ),
        ),
    ]
