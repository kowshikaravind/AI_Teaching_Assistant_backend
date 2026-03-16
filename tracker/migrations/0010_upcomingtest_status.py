from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0009_notification_and_upcomingtest_subject'),
    ]

    operations = [
        migrations.AddField(
            model_name='upcomingtest',
            name='status',
            field=models.CharField(
                choices=[('scheduled', 'Scheduled'), ('finished', 'Finished')],
                default='scheduled',
                max_length=20,
            ),
        ),
    ]
