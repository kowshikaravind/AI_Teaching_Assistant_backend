from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0010_upcomingtest_status'),
    ]

    operations = [
        migrations.CreateModel(
            name='AdminCredential',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('username', models.CharField(max_length=100, unique=True)),
                ('password', models.CharField(max_length=128)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['-updated_at'],
            },
        ),
        migrations.CreateModel(
            name='TeacherCredential',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('teacher_name', models.CharField(max_length=120)),
                ('username', models.CharField(max_length=120, unique=True)),
                ('password', models.CharField(max_length=128)),
                ('department', models.CharField(blank=True, default='', max_length=120)),
                ('status', models.CharField(choices=[('pending', 'Pending Approval'), ('approved', 'Approved'), ('rejected', 'Rejected')], default='pending', max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['status', '-created_at'],
            },
        ),
    ]
