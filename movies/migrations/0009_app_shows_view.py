"""
Creates the `app_shows` view: one representative row per show_key, with a
season_count and a cleaned-up show_title (season suffix stripped). The Watch2D
app reads this through PostgREST to list shows once instead of once per season.
"""
from django.db import migrations


CREATE_VIEW = r"""
DROP VIEW IF EXISTS app_shows;
CREATE VIEW app_shows AS
SELECT DISTINCT ON (m.show_key)
    m.show_key,
    m.id          AS movie_id,
    m.title,
    m.slug,
    m.image_url,
    m.is_series,
    m.completed,
    m.created_at,
    m.views,
    sc.season_count,
    regexp_replace(
        m.title,
        '\s+(s[0-9]{1,2}(e[0-9]+)?|season\s+[0-9]+)(\s*\(?\s*complete[d]?\s*\)?)?\s*$',
        '', 'i'
    ) AS show_title
FROM movies_movie m
JOIN (
    SELECT show_key, COUNT(*) AS season_count
    FROM movies_movie
    WHERE show_key <> ''
    GROUP BY show_key
) sc ON sc.show_key = m.show_key
WHERE m.show_key <> ''
ORDER BY m.show_key, m.season_number DESC NULLS LAST, m.created_at DESC;

GRANT SELECT ON app_shows TO anon, authenticated;
"""

DROP_VIEW = "DROP VIEW IF EXISTS app_shows;"


class Migration(migrations.Migration):

    dependencies = [
        ('movies', '0008_movie_season_number_movie_show_key'),
    ]

    operations = [
        migrations.RunSQL(sql=CREATE_VIEW, reverse_sql=DROP_VIEW),
    ]
