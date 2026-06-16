"""
Let the app (anon / authenticated) read the new cast tables via PostgREST.
"""
from django.db import migrations


SQL = """
GRANT SELECT ON movies_person   TO anon, authenticated;
GRANT SELECT ON movies_moviecast TO anon, authenticated;

ALTER TABLE movies_person    ENABLE ROW LEVEL SECURITY;
ALTER TABLE movies_moviecast ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "read persons" ON movies_person;
CREATE POLICY "read persons" ON movies_person FOR SELECT USING (true);

DROP POLICY IF EXISTS "read cast" ON movies_moviecast;
CREATE POLICY "read cast" ON movies_moviecast FOR SELECT USING (true);
"""

REVERSE = """
DROP POLICY IF EXISTS "read persons" ON movies_person;
DROP POLICY IF EXISTS "read cast" ON movies_moviecast;
"""


class Migration(migrations.Migration):

    dependencies = [
        ('movies', '0011_person_moviecast'),
    ]

    operations = [
        migrations.RunSQL(sql=SQL, reverse_sql=REVERSE),
    ]
