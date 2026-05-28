# movies/urls.py (app-level)
from django.urls import path
from django.views.generic import TemplateView
from .views import (
    HomeView,
    CategoryMoviesView, MovieDetailView,
    toggle_like, toggle_watchlist, SearchResultsView, ping_view, add_comment, add_reply,
    delete_comment, resolve_download_link, check_streamable, stream_proxy,
    old_movie_redirect,  # ← handles legacy /movie/<pk>/ URLs (301 → slug URL)
)

app_name = 'movies'

urlpatterns = [
    path('', HomeView.as_view(), name='home'),

    path('category/<int:cat_id>/', CategoryMoviesView.as_view(), name='category_movies'),

    # ── NEW canonical URL:  /movie/<pk>/<slug>/  ──────────────────────────────
    # This is the SEO URL every new link and template should use.
    # If someone visits with the wrong slug the view redirects them to the
    # correct one (permanent 301), so there is only one canonical version.
    path('movie/<int:pk>/<slug:slug>/', MovieDetailView.as_view(), name='movie_detail'),

    # ── LEGACY redirect:  /movie/<pk>/  ──────────────────────────────────────
    # Keeps all 20K existing links working.
    # Issues a permanent 301 redirect to the matching slug URL above.
    # Google will transfer link equity; bookmarks and old embeds keep working.
    path('movie/<int:pk>/', old_movie_redirect, name='movie_detail_legacy'),

    path('movie/<int:pk>/like/',      toggle_like,      name='toggle_like'),
    path('movie/<int:pk>/watchlist/', toggle_watchlist,  name='toggle_watchlist'),
    path('search/', SearchResultsView.as_view(), name='search_results'),

    path('google302ebddf493cb41d.html', TemplateView.as_view(
        template_name='movies/google302ebddf493cb41d.html',
        content_type='text/html'
    )),

    path('wp_auth_encrypt_ping/', ping_view, name='ping'),

    # Comment URLs
    path('movie/<int:pk>/comment/',                               add_comment,  name='add_comment'),
    path('movie/<int:movie_pk>/comment/<int:comment_pk>/reply/',  add_reply,    name='add_reply'),
    path('comment/<int:pk>/delete/',                              delete_comment, name='delete_comment'),

    # Live download URL resolver (fetches ?pt= token on-the-fly)
    path('resolve-download/', resolve_download_link, name='resolve_download'),
    path('check-streamable/', check_streamable,      name='check_streamable'),

    # Range-aware streaming proxy
    path('stream/', stream_proxy, name='stream_proxy'),
]