# Generated manually to rename department to assigned_class

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0015_delete_attendance'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='teachercredential',
            name='department',
        ),
        migrations.AddField(
            model_name='teachercredential',
            name='assigned_class',
            field=models.CharField(blank=True, default='', max_length=120),
        ),
    ]
