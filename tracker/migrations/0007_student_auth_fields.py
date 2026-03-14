from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0006_subject'),
    ]

    operations = [
        migrations.AddField(
            model_name='student',
            name='student_email',
            field=models.EmailField(blank=True, max_length=254, null=True, unique=True),
        ),
        migrations.AddField(
            model_name='student',
            name='student_number',
            field=models.CharField(blank=True, max_length=20, null=True, unique=True),
        ),
        migrations.AddField(
            model_name='student',
            name='student_password',
            field=models.CharField(default='student-123', max_length=128),
        ),
    ]
