"""
Resets every movie's weekly_views counter to 0. Run once a week (see
CRONJOBS in settings.py) so "Trending This Week" reflects only this
week's activity instead of accumulating forever like the all-time `views`
counter does.

    python manage.py reset_weekly_views
"""
from django.core.management.base import BaseCommand

from movies.models import Movie


class Command(BaseCommand):
    help = "Reset weekly_views to 0 for every movie (weekly cron)."

    def handle(self, *args, **opts):
        updated = Movie.objects.exclude(weekly_views=0).update(weekly_views=0)
        self.stdout.write(self.style.SUCCESS(f"Reset weekly_views for {updated} movies."))
