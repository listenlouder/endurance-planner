from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('events', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='event',
            name='admin_key',
            field=models.CharField(editable=False, max_length=20),
        ),
    ]
