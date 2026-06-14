"""
Keep the movie DB clean. Safe to run repeatedly (idempotent).

  python manage.py cleanse_db            # purge junk links + de-dupe movies
  python manage.py cleanse_db --links    # only purge junk download links
  python manage.py cleanse_db --dupes    # only de-dupe movies
  python manage.py cleanse_db --dry-run  # report what WOULD change, change nothing

Put it on cron (after your scrapers) so "S01 / Season 1" twins and source-page
links never accumulate again.
"""
from django.core.management.base import BaseCommand
from django.db import connection, transaction

from movies.scraper_utils import JUNK_DOWNLOAD_HOSTS


class Command(BaseCommand):
    help = "Purge junk download links and de-duplicate movies."

    def add_arguments(self, parser):
        parser.add_argument('--links', action='store_true', help='Only purge junk links')
        parser.add_argument('--dupes', action='store_true', help='Only de-dupe movies')
        parser.add_argument('--dry-run', action='store_true', help='Report only, change nothing')

    def handle(self, *args, **opts):
        do_links = opts['links'] or not opts['dupes']
        do_dupes = opts['dupes'] or not opts['links']
        dry = opts['dry_run']

        if do_links:
            self._purge_links(dry)
        if do_dupes:
            self._dedupe(dry)
        self.stdout.write(self.style.SUCCESS('cleanse_db done.'))

    # ── junk download links ──────────────────────────────────────────────────
    def _purge_links(self, dry):
        junk = list(JUNK_DOWNLOAD_HOSTS)
        where = """
            host IS NULL OR host = '' OR host NOT LIKE '%%.%%'
            OR regexp_replace(host, '^www[0-9]*\\.', '') = ANY(%s)
            OR host = ANY(%s)
        """
        sql_count = f"""
            WITH h AS (SELECT id, lower(substring(url from '://([^/:]+)')) host FROM movies_downloadlink)
            SELECT count(*) FROM h WHERE {where}
        """
        sql_delete = f"""
            DELETE FROM movies_downloadlink WHERE id IN (
                SELECT id FROM (
                    SELECT id, lower(substring(url from '://([^/:]+)')) host FROM movies_downloadlink
                ) h WHERE {where}
            )
        """
        with connection.cursor() as cur:
            cur.execute(sql_count, [junk, junk])
            n = cur.fetchone()[0]
            if dry:
                self.stdout.write(f"[dry-run] would remove {n} junk download links")
                return
            with transaction.atomic():
                cur.execute(sql_delete, [junk, junk])
            self.stdout.write(self.style.WARNING(f"removed {n} junk download links"))

    # ── duplicate movies ─────────────────────────────────────────────────────
    def _dedupe(self, dry):
        # Normalized key: S01/S1/S01E05 -> "season 1", drop (complete), strip punctuation.
        key = (
            "btrim(regexp_replace("
            "regexp_replace(lower(m.title), '\\ms0*([0-9]+)(e[0-9]+)?\\M', 'season \\1', 'g'),"
            "'(\\(?\\s*complete[d]?\\s*\\)?)|[^a-z0-9]+', ' ', 'g'))"
        )
        count_sql = f"""
            WITH n AS (SELECT m.id, {key} k FROM movies_movie m)
            SELECT coalesce(sum(c-1),0) FROM (SELECT k, count(*) c FROM n GROUP BY k HAVING count(*)>1) t
        """
        with connection.cursor() as cur:
            cur.execute(count_sql)
            extra = cur.fetchone()[0]
            if dry:
                self.stdout.write(f"[dry-run] would merge {extra} duplicate movies")
                return
            if not extra:
                self.stdout.write("no duplicate movies")
                return
            with transaction.atomic():
                cur.execute(self._dedupe_block(key))
            self.stdout.write(self.style.WARNING(f"merged {extra} duplicate movies"))

    @staticmethod
    def _dedupe_block(key):
        # Keeps the richest copy (has stream, then most links, then completed),
        # moves links/comments/categories/likes onto it, deletes the rest.
        return f"""
        DO $$
        DECLARE r record; keep bigint; dups bigint[];
        BEGIN
          FOR r IN
            SELECT array_agg(id ORDER BY (stream_url IS NOT NULL AND stream_url<>'') DESC,
                                         dlc DESC, completed DESC, views DESC, id ASC) AS ids
            FROM (SELECT m.id, m.stream_url, m.completed, m.views, {key} AS k,
                    (SELECT count(*) FROM movies_downloadlink d WHERE d.movie_id=m.id) AS dlc
                  FROM movies_movie m) t
            GROUP BY k HAVING count(*)>1
          LOOP
            keep := r.ids[1]; dups := r.ids[2:array_length(r.ids,1)];
            UPDATE movies_downloadlink SET movie_id=keep WHERE movie_id=ANY(dups);
            UPDATE movies_comment     SET movie_id=keep WHERE movie_id=ANY(dups);
            UPDATE movies_movie_categories mc SET movie_id=keep WHERE mc.movie_id=ANY(dups)
              AND NOT EXISTS (SELECT 1 FROM movies_movie_categories x WHERE x.movie_id=keep AND x.category_id=mc.category_id);
            DELETE FROM movies_movie_categories WHERE movie_id=ANY(dups);
            UPDATE movies_movie_liked_by lb SET movie_id=keep WHERE lb.movie_id=ANY(dups)
              AND NOT EXISTS (SELECT 1 FROM movies_movie_liked_by x WHERE x.movie_id=keep AND x.user_id=lb.user_id);
            DELETE FROM movies_movie_liked_by WHERE movie_id=ANY(dups);
            UPDATE movies_movie_watchlisted_by wb SET movie_id=keep WHERE wb.movie_id=ANY(dups)
              AND NOT EXISTS (SELECT 1 FROM movies_movie_watchlisted_by x WHERE x.movie_id=keep AND x.user_id=wb.user_id);
            DELETE FROM movies_movie_watchlisted_by WHERE movie_id=ANY(dups);
            DELETE FROM movies_movie WHERE id=ANY(dups);
          END LOOP;
        END $$;
        """
