from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('events', '0002_driver_user'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='availability',
            name='user',
        ),
    ]
