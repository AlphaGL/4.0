# movies/urls.py (app-level)  ← FULL REPLACEMENT
from django.urls import path
from django.views.generic import TemplateView
from .views import (
    HomeView,
    CategoryMoviesView, MovieDetailView,
    toggle_like, toggle_watchlist, SearchResultsView, ping_view, add_comment, add_reply,
    delete_comment, resolve_download_link, check_streamable, stream_proxy,
    old_movie_redirect,       # ← handles legacy /movie/<pk>/ URLs (301 → slug URL)
    old_category_redirect, report_broken_link,    # ← handles legacy /category/<pk>/ URLs (301 → slug URL)
    DownloadGateView,         # ← NEW: monetised download gate
)

app_name = 'movies'

urlpatterns = [
    path('', HomeView.as_view(), name='home'),

    path('category/<int:cat_id>/<slug:slug>/', CategoryMoviesView.as_view(), name='category_movies'),

    # ── LEGACY redirect: /category/<pk>/ ─────────────────────────────────────
    path('category/<int:cat_id>/', old_category_redirect, name='category_movies_legacy'),

    # ── Action endpoints — MUST be declared before movie/<pk>/<slug:slug>/ ───
    # Django matches patterns top-to-bottom. Paths like "report-broken-link",
    # "like", "watchlist", and "comment" are all valid slugs and would be
    # swallowed by the MovieDetailView route if it came first.
    path('movie/<int:pk>/report-broken-link/', report_broken_link, name='report_broken_link'),
    path('movie/<int:pk>/like/',               toggle_like,         name='toggle_like'),
    path('movie/<int:pk>/watchlist/',          toggle_watchlist,    name='toggle_watchlist'),
    path('movie/<int:pk>/comment/',            add_comment,         name='add_comment'),
    path('movie/<int:movie_pk>/comment/<int:comment_pk>/reply/', add_reply, name='add_reply'),

    # ── NEW: Download gate — sits before the canonical detail URL ─────────────
    # The gate is a clean page (/movie/<pk>/download/) that fires the popunder
    # then counts down before handing the user the real download link.
    # ?link=<DownloadLink.pk>  or  ?url=<percent-encoded-url>
    path('movie/<int:pk>/download/', DownloadGateView.as_view(), name='download_gate'),

    # ── Canonical SEO URL:  /movie/<pk>/<slug>/  ─────────────────────────────
    path('movie/<int:pk>/<slug:slug>/', MovieDetailView.as_view(), name='movie_detail'),

    # ── LEGACY redirect:  /movie/<pk>/  ──────────────────────────────────────
    path('movie/<int:pk>/', old_movie_redirect, name='movie_detail_legacy'),

    path('comment/<int:pk>/delete/', delete_comment, name='delete_comment'),

    path('search/', SearchResultsView.as_view(), name='search_results'),

    path('google302ebddf493cb41d.html', TemplateView.as_view(
        template_name='movies/google302ebddf493cb41d.html',
        content_type='text/html'
    )),

    path('wp_auth_encrypt_ping/', ping_view, name='ping'),

    # Live download URL resolver
    path('resolve-download/', resolve_download_link, name='resolve_download'),
    path('check-streamable/', check_streamable,      name='check_streamable'),

    # Range-aware streaming proxy
    path('stream/', stream_proxy, name='stream_proxy'),
]