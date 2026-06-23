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

from movies.scraper_utils import JUNK_DOWNLOAD_HOSTS, canonical_category_name


class Command(BaseCommand):
    help = "Purge junk download links, merge duplicate categories, de-duplicate movies."

    def add_arguments(self, parser):
        parser.add_argument('--links', action='store_true', help='Only purge junk links')
        parser.add_argument('--linkdupes', action='store_true',
                            help='Only remove duplicate download links (same URL on a movie)')
        parser.add_argument('--cats', action='store_true', help='Only merge duplicate categories')
        parser.add_argument('--dupes', action='store_true', help='Only de-dupe movies')
        parser.add_argument('--dry-run', action='store_true', help='Report only, change nothing')

    def handle(self, *args, **opts):
        only = opts['links'] or opts['linkdupes'] or opts['cats'] or opts['dupes']
        dry = opts['dry_run']

        if opts['links'] or not only:
            self._purge_links(dry)
        if opts['linkdupes'] or not only:
            self._dedupe_links(dry)
        if opts['cats'] or not only:
            self._dedupe_categories(dry)
        if opts['dupes'] or not only:
            self._dedupe(dry)
        self.stdout.write(self.style.SUCCESS('cleanse_db done.'))

    # ── duplicate / mis-named categories ─────────────────────────────────────
    def _dedupe_categories(self, dry):
        from collections import defaultdict
        from movies.models import Category

        groups = defaultdict(list)
        for c in Category.objects.all().order_by('id'):
            groups[canonical_category_name(c.name).lower()].append(c)

        dupes = sum(len(v) - 1 for v in groups.values() if len(v) > 1)
        if dry:
            self.stdout.write(f"[dry-run] would merge {dupes} duplicate categories")
            return
        if not dupes:
            self.stdout.write("no duplicate categories")
            return

        with transaction.atomic(), connection.cursor() as cur:
            for cats in groups.values():
                if len(cats) < 2:
                    continue
                canonical = canonical_category_name(cats[0].name)
                keeper = next((c for c in cats if c.name == canonical), None)
                if keeper is None:
                    keeper = max(cats, key=lambda c: c.movies.count())
                    keeper.name = canonical
                    keeper.save(update_fields=['name'])
                for c in cats:
                    if c.id == keeper.id:
                        continue
                    cur.execute(
                        "UPDATE movies_movie_categories mc SET category_id=%s "
                        "WHERE mc.category_id=%s AND NOT EXISTS (SELECT 1 FROM "
                        "movies_movie_categories x WHERE x.movie_id=mc.movie_id AND x.category_id=%s)",
                        [keeper.id, c.id, keeper.id])
                    cur.execute("DELETE FROM movies_movie_categories WHERE category_id=%s", [c.id])
                    c.delete()
        self.stdout.write(self.style.WARNING(f"merged {dupes} duplicate categories"))

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

    # ── duplicate download links (same URL repeated on one movie) ────────────
    def _dedupe_links(self, dry):
        # Two links are duplicates if they belong to the same movie and have the
        # same URL (case-insensitive, ignoring a trailing slash). Keep lowest id.
        norm = "lower(regexp_replace(url, '/+$', ''))"
        count_sql = f"""
            SELECT coalesce(sum(c-1),0) FROM (
                SELECT count(*) c FROM movies_downloadlink
                GROUP BY movie_id, {norm} HAVING count(*) > 1
            ) t
        """
        delete_sql = f"""
            DELETE FROM movies_downloadlink a
            USING movies_downloadlink b
            WHERE a.movie_id = b.movie_id
              AND a.id > b.id
              AND {norm.replace('url', 'a.url')} = {norm.replace('url', 'b.url')}
        """
        with connection.cursor() as cur:
            cur.execute(count_sql)
            n = cur.fetchone()[0]
            if dry:
                self.stdout.write(f"[dry-run] would remove {n} duplicate download links")
                return
            if not n:
                self.stdout.write("no duplicate download links")
                return
            with transaction.atomic():
                cur.execute(delete_sql)
            self.stdout.write(self.style.WARNING(f"removed {n} duplicate download links"))

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
