# movies/context_processors.py
from django.conf import settings
from django.core.cache import cache
from .models import Category

_CACHE_KEY = 'movies_categories_v2'   # bumped: now excludes adult/18+
_CACHE_TTL = 60 * 60  # 1 hour — categories change rarely


def categories_processor(request):
    cats = cache.get(_CACHE_KEY)
    if cats is None:
        # Adult/18+ hidden from public browse (ad-network + SEO safety).
        cats = list(Category.objects.exclude(name__icontains='18+'))
        cache.set(_CACHE_KEY, cats, _CACHE_TTL)
    ctx = {
        'categories': cats,
        # Monetag Telegram Mini App zone id (empty until configured) — read by
        # the Mini App script in base.html.
        'MONETAG_MINIAPP_ZONE': getattr(settings, 'MONETAG_MINIAPP_ZONE', ''),
    }
    # Blank login/signup forms available on EVERY page for the global auth
    # modal (base.html) — so "Log in" anywhere pops up in place instead of
    # navigating to a separate page. Skipped once logged in (never needed).
    if not request.user.is_authenticated:
        from allauth.account.forms import LoginForm, SignupForm
        ctx['global_login_form'] = LoginForm()
        ctx['global_signup_form'] = SignupForm()
    return ctx