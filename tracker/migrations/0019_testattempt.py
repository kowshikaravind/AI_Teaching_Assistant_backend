from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0018_clear_upcoming_tests_data'),
    ]

    operations = [
        migrations.CreateModel(
            name='TestAttempt',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('answers_payload', models.JSONField(blank=True, default=list)),
                ('score', models.FloatField(default=0)),
                ('total_marks', models.FloatField(default=0)),
                ('correct_count', models.PositiveIntegerField(default=0)),
                ('incorrect_count', models.PositiveIntegerField(default=0)),
                ('unattempted_count', models.PositiveIntegerField(default=0)),
                ('attempted_count', models.PositiveIntegerField(default=0)),
                ('accuracy', models.FloatField(default=0)),
                ('attempt_rate', models.FloatField(default=0)),
                ('time_taken_seconds', models.PositiveIntegerField(default=0)),
                ('submitted_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('student', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='test_attempts', to='tracker.student')),
                ('test', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='test_attempts', to='tracker.upcomingtest')),
            ],
            options={
                'ordering': ['-updated_at'],
                'unique_together': {('student', 'test')},
            },
        ),
    ]
