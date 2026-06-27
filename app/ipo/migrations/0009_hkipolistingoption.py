from django.db import migrations, models


DEFAULT_OPTIONS = [
    ("listing_type", "new_listing", "新上市", 10),
    ("listing_type", "ah", "AH", 20),
    ("listing_type", "us_hk", "美港", 30),
    ("listing_type", "gem", "创业板", 40),
    ("listing_type", "other", "其他", 50),
    ("mechanism", "a", "机制A", 10),
    ("mechanism", "b", "机制B", 20),
    ("mechanism", "18a", "18A", 30),
    ("mechanism", "18c", "18C", 40),
]


def create_default_options(apps, schema_editor):
    option_model = apps.get_model("ipo", "HkIpoListingOption")
    option_model.objects.bulk_create(
        [
            option_model(
                category=category,
                code=code,
                name=name,
                sort_order=sort_order,
            )
            for category, code, name, sort_order in DEFAULT_OPTIONS
        ]
    )


class Migration(migrations.Migration):

    dependencies = [
        ("ipo", "0008_populate_missing_closed_sell_dates"),
    ]

    operations = [
        migrations.CreateModel(
            name="HkIpoListingOption",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "category",
                    models.CharField(
                        choices=[
                            ("listing_type", "新股类型"),
                            ("mechanism", "发行机制"),
                        ],
                        max_length=30,
                        verbose_name="选项类别",
                    ),
                ),
                ("code", models.SlugField(max_length=30, verbose_name="选项代码")),
                ("name", models.CharField(max_length=50, verbose_name="显示名称")),
                (
                    "sort_order",
                    models.PositiveSmallIntegerField(default=0, verbose_name="排序"),
                ),
                ("is_active", models.BooleanField(default=True, verbose_name="启用")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
            ],
            options={
                "verbose_name": "新股类型与机制选项",
                "verbose_name_plural": "新股类型与机制选项",
                "ordering": ["category", "sort_order", "id"],
            },
        ),
        migrations.AddConstraint(
            model_name="hkipolistingoption",
            constraint=models.UniqueConstraint(
                fields=("category", "code"),
                name="unique_ipo_listing_option_code",
            ),
        ),
        migrations.AlterField(
            model_name="hkipolisting",
            name="listing_type",
            field=models.CharField(default="new_listing", max_length=30, verbose_name="类型"),
        ),
        migrations.AlterField(
            model_name="hkipolisting",
            name="mechanism",
            field=models.CharField(default="a", max_length=20, verbose_name="机制"),
        ),
        migrations.RunPython(create_default_options, migrations.RunPython.noop),
    ]
