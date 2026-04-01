# Generated manually for question bank support

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0016_remove_teachercredential_department_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='upcomingtest',
            name='question_bank',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
