"""
One-off backfill: give every existing User (who signed up before the
nickname/avatar system existed) a Profile with an auto-generated display
name. New signups get this automatically via the post_save signal — this
command is only needed once, for accounts that already existed.

    python manage.py backfill_profiles
"""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from movies.models import Profile
from movies.nicknames import generate_nickname


class Command(BaseCommand):
    help = "Create a Profile (anonymous nickname + avatar) for every User missing one."

    def handle(self, *args, **opts):
        users = User.objects.filter(profile__isnull=True)
        total = users.count()
        self.stdout.write(f"{total} users missing a profile...")
        for user in users:
            Profile.objects.create(user=user, display_name=generate_nickname())
        self.stdout.write(self.style.SUCCESS(f"Done. Created {total} profiles."))
