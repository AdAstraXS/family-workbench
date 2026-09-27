from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('investment_research', '0008_officialresearchcontentversion_media_type_and_more')]
    operations = [migrations.AddField(
        model_name='officialresearchcontentversion', name='acquisition_note',
        field=models.CharField('原件入库说明', max_length=250, blank=True, default=''),
    )]
