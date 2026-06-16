"""
Enrich scraped titles with TheMovieDB data: rating, official trailer, cast,
overview, genres, runtime — and a clean poster (re-hosted to R2) when the
current image is broken/down. Matched by title (+ year), cached per title.

    python manage.py enrich_tmdb --limit 50 --verbose
    python manage.py enrich_tmdb --workers 8
    python manage.py enrich_tmdb --force        # re-run already-synced titles

Needs TMDB_API_KEY (and R2_* for poster re-hosting) in .env.
"""
import concurrent.futures

from django.core.management.base import BaseCommand
from django.db import connections
from decouple import config

from movies.models import Movie, Person, MovieCast
from movies import tmdb
from movies.r2 import rehost_image, is_configured as r2_ready


def _sync_cast(movie_id, cast_list, r2):
    """Create Person rows (with re-hosted headshots) + MovieCast links."""
    for c in cast_list or []:
        person, created = Person.objects.get_or_create(
            tmdb_id=c['tmdb_id'], defaults={'name': c['name'][:200]})
        if created and c.get('profile_path') and r2:
            img = rehost_image(f"https://image.tmdb.org/t/p/w185{c['profile_path']}")
            if img:
                Person.objects.filter(pk=person.pk).update(profile_url=img)
        MovieCast.objects.update_or_create(
            movie_id=movie_id, person=person,
            defaults={'character': (c.get('character') or '')[:200],
                      'order': c.get('order', 0)})


class Command(BaseCommand):
    help = "Enrich titles with TMDB rating / trailer / cast / overview / poster."

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=None)
        parser.add_argument('--workers', type=int, default=6,
                            help="Parallel lookups (default 6).")
        parser.add_argument('--force', action='store_true',
                            help="Re-run even titles already attempted.")
        parser.add_argument('--verbose', action='store_true',
                            help="Print titles with no TMDB match.")

    def handle(self, *args, **opts):
        if not tmdb.is_configured():
            self.stderr.write(self.style.ERROR("Set TMDB_API_KEY in your .env."))
            return

        public = config('R2_PUBLIC_URL', default='').rstrip('/')
        r2 = r2_ready()

        qs = Movie.objects.all()
        if not opts['force']:
            qs = qs.filter(tmdb_synced=False)
        qs = qs.only('id', 'title', 'is_series', 'vi_year', 'vi_cast',
                     'vi_genre', 'vi_runtime', 'description', 'image_url')
        if opts['limit']:
            qs = qs[:opts['limit']]
        movies = list(qs)
        total = len(movies)
        workers = max(1, opts['workers'])
        verbose = opts['verbose']
        self.stdout.write(f"{total} titles to enrich (workers={workers})...")

        def work(m):
            try:
                year = (m.vi_year or '').strip() or None
                match = tmdb.search(m.title, year, m.is_series)
                if not match:
                    Movie.objects.filter(pk=m.id).update(tmdb_synced=True)
                    return (m, False)
                tid, media = match
                d = tmdb.details(tid, media)
                if not d:
                    Movie.objects.filter(pk=m.id).update(tmdb_synced=True)
                    return (m, False)

                updates = {'tmdb_synced': True, 'tmdb_id': tid}
                if d['rating'] is not None:
                    updates['rating'] = d['rating']
                if d['trailer_url']:
                    updates['trailer_url'] = d['trailer_url']
                if not (m.vi_cast or '').strip() and d['cast']:
                    updates['vi_cast'] = d['cast']
                if not (m.description or '').strip() and d['overview']:
                    updates['description'] = d['overview']
                if not (m.vi_genre or '').strip() and d['genres']:
                    updates['vi_genre'] = d['genres'][:200]
                if not (m.vi_runtime or '').strip() and d['runtime']:
                    updates['vi_runtime'] = d['runtime'][:30]

                # Poster: only swap in TMDB's when the current image isn't
                # already safely on our R2 (i.e. it's broken / from a down site).
                cur = m.image_url or ''
                already_ok = public and cur.startswith(public)
                if d['poster_url'] and r2 and not already_ok:
                    new_img = rehost_image(d['poster_url'])
                    if new_img:
                        updates['image_url'] = new_img

                Movie.objects.filter(pk=m.id).update(**updates)
                _sync_cast(m.id, d.get('cast_list'), r2)
                return (m, True)
            finally:
                connections.close_all()

        done = failed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(work, m) for m in movies]
            for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
                try:
                    m, ok = fut.result()
                except Exception:
                    m, ok = None, False
                if ok:
                    done += 1
                else:
                    failed += 1
                    if verbose and m is not None:
                        self.stdout.write(f"  no match: {m.title[:60]}")
                if i % 100 == 0:
                    self.stdout.write(
                        f"  …{done} enriched, {failed} no-match ({i}/{total})")

        self.stdout.write(self.style.SUCCESS(
            f"Enriched {done}, no-match {failed}, of {total}."))
