from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0021_aianalysisresult'),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                """
                ALTER TABLE tracker_teachercredential
                ADD COLUMN IF NOT EXISTS assigned_class varchar(120) NOT NULL DEFAULT '';
                """,
                """
                UPDATE tracker_teachercredential
                SET assigned_class = COALESCE(NULLIF(assigned_class, ''), department, '')
                WHERE assigned_class IS NULL OR assigned_class = '';
                """,
                """
                ALTER TABLE tracker_teachercredential
                DROP COLUMN IF EXISTS department;
                """,
                """
                ALTER TABLE tracker_teachercredential
                ALTER COLUMN assigned_class SET DEFAULT '';
                """,
            ],
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
