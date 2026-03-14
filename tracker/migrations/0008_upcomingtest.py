from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0007_student_auth_fields'),
    ]

    operations = [
        migrations.CreateModel(
            name='UpcomingTest',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('test_name', models.CharField(max_length=120)),
                ('topic', models.CharField(max_length=255)),
                ('test_date', models.DateField()),
                ('total_marks', models.PositiveIntegerField()),
                ('class_name', models.CharField(max_length=50)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['test_date', '-created_at'],
            },
        ),
    ]
