# movies/urls.py (app-level)
from django.urls import path
from django.views.generic import TemplateView
from .views import (
    HomeView,
    CategoryMoviesView, MovieDetailView,
    toggle_like, toggle_watchlist, SearchResultsView, ping_view, add_comment, add_reply,
    delete_comment, resolve_download_link, check_streamable, stream_proxy,  # ← added stream_proxy
)

app_name = 'movies'

urlpatterns = [
    path('', HomeView.as_view(), name='home'),
    
    path('category/<int:cat_id>/', CategoryMoviesView.as_view(), name='category_movies'),
    path('movie/<int:pk>/', MovieDetailView.as_view(), name='movie_detail'),
    path('movie/<int:pk>/like/', toggle_like, name='toggle_like'),
    path('movie/<int:pk>/watchlist/', toggle_watchlist, name='toggle_watchlist'),
    path('search/', SearchResultsView.as_view(), name='search_results'),

    path('google302ebddf493cb41d.html', TemplateView.as_view(
        template_name='movies/google302ebddf493cb41d.html',
        content_type='text/html'
    )),

    path('wp_auth_encrypt_ping/', ping_view, name='ping'),

    # Comment URLs
    path('movie/<int:pk>/comment/', add_comment, name='add_comment'),
    path('movie/<int:movie_pk>/comment/<int:comment_pk>/reply/', add_reply, name='add_reply'),
    path('comment/<int:pk>/delete/', delete_comment, name='delete_comment'),

    # Live download URL resolver (fetches ?pt= token on-the-fly)
    path('resolve-download/', resolve_download_link, name='resolve_download'),
    path('check-streamable/', check_streamable, name='check_streamable'),

    # ── NEW: range-aware streaming proxy ─────────────────────────────────────
    # Called by the frontend when user clicks Watch/Stream.
    # Resolves the landing URL fresh each time (handles expiring links),
    # then pipes the video bytes through Django to the browser.
    path('stream/', stream_proxy, name='stream_proxy'),
]