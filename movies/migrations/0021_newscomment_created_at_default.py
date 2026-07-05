from django.db import migrations


class Migration(migrations.Migration):
    """Give the news comment/reaction `created_at` columns a DB-level DEFAULT.

    The models use `auto_now_add=True`, which Django fills on ITS own inserts —
    but the app writes these rows DIRECTLY via Supabase, which doesn't provide
    the value, so the NOT NULL column raised "null value in column created_at"
    and every comment/reaction insert failed. A DB default fixes it for any
    client. Idempotent (SET DEFAULT), so safe to re-run.
    """

    dependencies = [
        ('movies', '0020_contactmessage'),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                "ALTER TABLE movies_newscomment ALTER COLUMN created_at SET DEFAULT now();",
                "ALTER TABLE movies_newsreaction ALTER COLUMN created_at SET DEFAULT now();",
            ],
            reverse_sql=[
                "ALTER TABLE movies_newscomment ALTER COLUMN created_at DROP DEFAULT;",
                "ALTER TABLE movies_newsreaction ALTER COLUMN created_at DROP DEFAULT;",
            ],
        ),
    ]
