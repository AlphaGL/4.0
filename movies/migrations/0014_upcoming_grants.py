"""Let the app read the Coming Soon table via PostgREST."""
from django.db import migrations


SQL = """
GRANT SELECT ON movies_upcomingtitle TO anon, authenticated;
ALTER TABLE movies_upcomingtitle ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "read upcoming" ON movies_upcomingtitle;
CREATE POLICY "read upcoming" ON movies_upcomingtitle FOR SELECT USING (true);
"""

REVERSE = 'DROP POLICY IF EXISTS "read upcoming" ON movies_upcomingtitle;'


class Migration(migrations.Migration):
    dependencies = [('movies', '0013_upcomingtitle')]
    operations = [migrations.RunSQL(sql=SQL, reverse_sql=REVERSE)]
