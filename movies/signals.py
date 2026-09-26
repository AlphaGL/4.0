"""
Auto re-host poster images on R2 whenever a Movie is saved — so newly scraped
titles get their image on your own domain without a manual step.

Opt-in: only runs when REHOST_IMAGES=true (env) AND R2 is configured. Leave it
off for big bulk scrapes (faster) and run `manage.py rehost_images` afterwards;
turn it on for ongoing scrapes so new images are hosted automatically.
"""
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver
from decouple import config

from .models import Movie, Profile


@receiver(post_save, sender=User)
def create_profile_for_new_user(sender, instance, created, **kwargs):
    """Every new account (password signup OR Google OAuth) gets an anonymous
    nickname + generated avatar immediately — nobody ever sees the raw
    account username/email publicly."""
    if not created:
        return
    from .nicknames import generate_nickname
    Profile.objects.get_or_create(user=instance, defaults={
        'display_name': generate_nickname(),
    })


@receiver(post_save, sender=Movie)
def rehost_movie_image(sender, instance, **kwargs):
    if not config('REHOST_IMAGES', default=False, cast=bool):
        return
    url = (instance.image_url or '').strip()
    if not url:
        return
    public = config('R2_PUBLIC_URL', default='').rstrip('/')
    if not public or url.startswith(public):
        return  # blank or already on our domain

    from .r2 import is_configured, rehost_image
    if not is_configured():
        return

    new_url = rehost_image(url, instance.pk)
    if new_url and new_url != url:
        # .update() bypasses signals → no recursion.
        Movie.objects.filter(pk=instance.pk).update(image_url=new_url)
