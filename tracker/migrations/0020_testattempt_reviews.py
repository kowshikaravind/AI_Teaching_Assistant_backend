from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0019_testattempt'),
    ]

    operations = [
        migrations.AddField(
            model_name='testattempt',
            name='behavior_patterns',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='testattempt',
            name='conceptual_patterns',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
