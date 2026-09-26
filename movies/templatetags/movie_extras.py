# main/templatetags/movie_extras.py
from django import template
from datetime import datetime, timedelta, timezone
from django.utils import timezone as dj_timezone

register = template.Library()

@register.filter
def shorttime(value):
    """Compact, X/Twitter-style relative timestamp: 'now', '2m', '3h', '1d',
    '2w', then a short date past ~5 weeks — instead of Django's verbose
    '3 hours, 12 minutes ago'."""
    if not value:
        return ''
    now = dj_timezone.now()
    seconds = (now - value).total_seconds()
    if seconds < 60:
        return 'now'
    minutes = seconds / 60
    if minutes < 60:
        return f'{int(minutes)}m'
    hours = minutes / 60
    if hours < 24:
        return f'{int(hours)}h'
    days = hours / 24
    if days < 7:
        return f'{int(days)}d'
    weeks = days / 7
    if weeks < 5:
        return f'{int(weeks)}w'
    return f"{value.strftime('%b')} {value.day}" if hasattr(value, 'strftime') else str(value)

@register.filter
def is_recent(value, days=7):
    if not value:
        return False
    now = datetime.now(timezone.utc)
    return value >= now - timedelta(days=int(days))

@register.simple_tag
def trending_movies(count=10):
    from movies.models import Movie
    return Movie.objects.order_by('-views', '-created_at')[:int(count)]

@register.filter
def external_rating(movie, source):
    """Pull one source's ExternalRating from an already-prefetched
    `movie.external_ratings.all()` — no extra query. Returns None if absent."""
    for r in movie.external_ratings.all():
        if r.source == source:
            return r
    return None

@register.filter
def get_item(d, key):
    return d.get(key, 0) if d else 0

@register.filter
def tomato_status(score):
    """RT-style qualitative label for a critic score (0-10 scale) — used in
    the badge's tooltip, e.g. 'Certified fresh score. 87%'."""
    if score is None:
        return ''
    if score >= 8.5:
        return 'Certified fresh score'
    if score >= 6:
        return 'Fresh score'
    return 'Rotten score'

@register.filter
def popcorn_status(score):
    """RT-style qualitative label for an audience score (0-10 scale)."""
    if score is None:
        return ''
    if score >= 6:
        return 'Fresh audience score'
    return 'Spilled audience score'

_CATEGORY_ICONS = [
    ('nollywood', 'fa-masks-theater'),
    ('hollywood', 'fa-film'),
    ('korean', 'fa-flag'),
    ('kdrama', 'fa-flag'),
    ('chinese', 'fa-flag'),
    ('anime', 'fa-torii-gate'),
    ('thai', 'fa-flag'),
    ('bollywood', 'fa-music'),
    ('ongoing', 'fa-arrows-rotate'),
    ('horror', 'fa-ghost'),
    ('comedy', 'fa-face-laugh-beam'),
    ('romance', 'fa-heart'),
    ('action', 'fa-explosion'),
    ('sport', 'fa-trophy'),
    ('wrestling', 'fa-trophy'),
    ('18', 'fa-lock'),
    ('series', 'fa-tv'),
]


@register.filter
def category_icon_class(name):
    """Font Awesome icon class for a category name — single source of truth
    instead of copy-pasted emoji if/elif chains across templates."""
    n = (name or '').lower()
    for key, icon in _CATEGORY_ICONS:
        if key in n:
            return icon
    return 'fa-video'


@register.filter
def user_reaction(comment, user):
    """Which emoji (if any) this user reacted to a comment with — uses the
    prefetched `comment.reactions.all()`, no extra query."""
    if not user or not getattr(user, 'is_authenticated', False):
        return None
    for r in comment.reactions.all():
        if r.user_id == user.pk:
            return r.emoji
    return None