from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0011_admincredential_teachercredential'),
    ]

    operations = [
        migrations.AddField(
            model_name='teachercredential',
            name='assigned_class',
            field=models.CharField(blank=True, default='', max_length=50),
        ),
    ]