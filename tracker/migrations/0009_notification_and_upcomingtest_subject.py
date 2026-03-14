from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0008_upcomingtest'),
    ]

    operations = [
        migrations.AddField(
            model_name='upcomingtest',
            name='subject',
            field=models.CharField(default='General', max_length=120),
        ),
        migrations.AlterField(
            model_name='upcomingtest',
            name='topic',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.CreateModel(
            name='Notification',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('recipient_role', models.CharField(choices=[('student', 'Student'), ('teacher', 'Teacher')], default='student', max_length=20)),
                ('type', models.CharField(choices=[('test', 'Test Notification'), ('ai_warning', 'AI Warning'), ('teacher_alert', 'Teacher Alert')], max_length=20)),
                ('subject', models.CharField(blank=True, max_length=120)),
                ('message', models.TextField()),
                ('event_key', models.CharField(max_length=180, unique=True)),
                ('read_status', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('details', models.JSONField(blank=True, default=dict)),
                ('student', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='notifications', to='tracker.student')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
    ]
