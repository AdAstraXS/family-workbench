from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("family_core", "0007_assetcategory_code_constraint"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="assetcategory",
            constraint=models.UniqueConstraint(
                fields=("family", "code"),
                name="unique_asset_category_code_per_family",
            ),
        ),
    ]
