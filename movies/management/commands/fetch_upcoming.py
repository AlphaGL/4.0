"""
Pull TMDB "Coming Soon" titles (upcoming movies + airing TV) into the
UpcomingTitle table, re-hosting posters to R2. Titles already in the catalogue
(same tmdb_id) are removed automatically — so once you scrape + enrich a film,
it leaves the Coming Soon section and shows up normally.

    python manage.py fetch_upcoming
    python manage.py fetch_upcoming --pages 3

Needs TMDB_API_KEY (and R2_* for poster re-hosting).
"""
from django.core.management.base import BaseCommand

from movies.models import Movie, UpcomingTitle
from movies import tmdb
from movies.r2 import rehost_image, is_configured as r2_ready


class Command(BaseCommand):
    help = "Refresh the Coming Soon list from TMDB."

    def add_arguments(self, parser):
        parser.add_argument('--pages', type=int, default=2,
                            help="Pages per type (20 items/page). Default 2.")

    def handle(self, *args, **opts):
        if not tmdb.is_configured():
            self.stderr.write(self.style.ERROR("Set TMDB_API_KEY in your .env."))
            return
        r2 = r2_ready()
        added = 0

        for media in ('movie', 'tv'):
            for it in tmdb.upcoming(media, pages=opts['pages']):
                tid = it['tmdb_id']
                # Already in the catalogue → not "upcoming" anymore.
                if Movie.objects.filter(tmdb_id=tid).exists():
                    UpcomingTitle.objects.filter(tmdb_id=tid).delete()
                    continue
                poster = (rehost_image(it['poster_url'])
                          if (it['poster_url'] and r2) else it['poster_url'])
                UpcomingTitle.objects.update_or_create(
                    tmdb_id=tid,
                    defaults={
                        'media_type': media,
                        'title': it['title'][:255],
                        'overview': it['overview'],
                        'release_date': it['release_date'],
                        'poster_url': poster,
                        'trailer_url': tmdb.trailer(tid, media),
                        'rating': it['rating'],
                    })
                added += 1

        # Final sweep: drop any upcoming that is now in the catalogue, plus any
        # whose release date has already passed.
        import datetime
        today = datetime.date.today().isoformat()
        removed = UpcomingTitle.objects.filter(
            tmdb_id__in=Movie.objects.exclude(tmdb_id__isnull=True)
                                     .values('tmdb_id')).delete()[0]
        removed += UpcomingTitle.objects.filter(
            release_date__lt=today).exclude(release_date='').delete()[0]

        self.stdout.write(self.style.SUCCESS(
            f"Upcoming refreshed: {added} added/updated, {removed} removed "
            f"(now in catalogue). Total: {UpcomingTitle.objects.count()}."))
