# master/sitemaps.py  (place this in your master/ project folder)
#
# Covers:
#   - Static pages (home, movies list, anime list, manga list, etc.)
#   - Movie detail pages          /movie/<pk>/<slug>/
#   - Movie category pages        /category/<id>/<slug>/
#   - Anime detail pages          /anime/<slug>/
#   - Anime episode pages         /anime/watch/<slug>/episode/<n>/
#   - Anime category pages        /anime/category/<slug>/
#   - Manga detail pages          /manga/<slug>/
#   - Manga chapter reader pages  /manga/read/<slug>/chapter-<n>/
#   - Manga category pages        /manga/category/<slug>/
#
# Usage in master/urls.py:
#   from master.sitemaps import sitemaps
#   from django.contrib.sitemaps.views import sitemap
#   path('sitemap.xml', sitemap, {'sitemaps': sitemaps}, name='sitemap'),
#   path('sitemap-<section>.xml', sitemap, {'sitemaps': sitemaps}, name='django.contrib.sitemaps.views.sitemap'),

from django.contrib.sitemaps import Sitemap
from django.urls import reverse

from movies.models import Movie, Category as MovieCategory
from anime.models import Anime, Episode, AnimeCategory
from manga.models import Manga, Chapter, MangaCategory


# =============================================================================
# STATIC PAGES
# =============================================================================

class StaticViewSitemap(Sitemap):
    priority    = 1.0
    changefreq  = 'daily'
    protocol    = 'https'

    def items(self):
        return [
            # Movies
            ('movies:home',       {},  1.0, 'daily'),
            ('movies:search_results', {}, 0.6, 'weekly'),

            # Anime
            ('anime:list',            {}, 0.9, 'daily'),
            ('anime:trending',        {}, 0.8, 'daily'),
            ('anime:featured',        {}, 0.8, 'weekly'),
            ('anime:recently_added',  {}, 0.8, 'daily'),
            ('anime:category_list',   {}, 0.7, 'weekly'),
            ('anime:search',          {}, 0.6, 'weekly'),

            # Manga
            ('manga:list',        {}, 0.9, 'daily'),
            ('manga:trending',    {}, 0.8, 'daily'),
            ('manga:featured',    {}, 0.8, 'weekly'),
            ('manga:category_list', {}, 0.7, 'weekly'),
            ('manga:search',      {}, 0.6, 'weekly'),
        ]

    def location(self, item):
        name, kwargs, *_ = item
        return reverse(name, kwargs=kwargs) if kwargs else reverse(name)

    def priority(self, item):   # noqa: method shadows class attr intentionally
        return item[2]

    def changefreq(self, item): # noqa
        return item[3]


# =============================================================================
# MOVIES
# =============================================================================

class MovieSitemap(Sitemap):
    changefreq  = 'weekly'
    priority    = 0.8
    protocol    = 'https'
    limit       = 50000

    def items(self):
        return (
            Movie.objects
            .only('pk', 'slug', 'created_at', 'title_b_updated_at')
            .order_by('-created_at')
        )

    def location(self, obj):
        return reverse('movies:movie_detail', args=[obj.pk, obj.slug])

    def lastmod(self, obj):
        # If a new episode was added more recently, use that date
        return obj.title_b_updated_at or obj.created_at


class MovieCategorySitemap(Sitemap):
    changefreq  = 'weekly'
    priority    = 0.6
    protocol    = 'https'

    def items(self):
        return MovieCategory.objects.all()

    def location(self, obj):
        return reverse('movies:category_movies', args=[obj.pk, obj.slug])


# =============================================================================
# ANIME
# =============================================================================

class AnimeSitemap(Sitemap):
    changefreq  = 'weekly'
    priority    = 0.8
    protocol    = 'https'
    limit       = 50000

    def items(self):
        return (
            Anime.objects
            .filter(is_active=True)
            .only('slug', 'updated_at', 'created_at')
            .order_by('-created_at')
        )

    def location(self, obj):
        return reverse('anime:detail', args=[obj.slug])

    def lastmod(self, obj):
        return obj.updated_at or obj.created_at


class AnimeEpisodeSitemap(Sitemap):
    changefreq  = 'monthly'
    priority    = 0.6
    protocol    = 'https'
    limit       = 50000

    def items(self):
        return (
            Episode.objects
            .filter(is_active=True, anime__is_active=True)
            .select_related('anime')
            .only('anime__slug', 'episode_number', 'created_at')
            .order_by('-created_at')
        )

    def location(self, obj):
        return reverse('anime:episode_detail', args=[obj.anime.slug, obj.episode_number])

    def lastmod(self, obj):
        return obj.created_at


class AnimeCategorySitemap(Sitemap):
    changefreq  = 'weekly'
    priority    = 0.6
    protocol    = 'https'

    def items(self):
        return AnimeCategory.objects.filter(is_active=True)

    def location(self, obj):
        return reverse('anime:category_detail', args=[obj.slug])


# =============================================================================
# MANGA
# =============================================================================

class MangaSitemap(Sitemap):
    changefreq  = 'weekly'
    priority    = 0.8
    protocol    = 'https'
    limit       = 50000

    def items(self):
        return (
            Manga.objects
            .filter(is_active=True)
            .only('slug', 'updated_at', 'created_at')
            .order_by('-created_at')
        )

    def location(self, obj):
        return reverse('manga:detail', args=[obj.slug])

    def lastmod(self, obj):
        return obj.updated_at or obj.created_at


class MangaChapterSitemap(Sitemap):
    changefreq  = 'monthly'
    priority    = 0.6
    protocol    = 'https'
    limit       = 50000

    def items(self):
        return (
            Chapter.objects
            .filter(is_active=True, manga__is_active=True)
            .select_related('manga')
            .only('manga__slug', 'chapter_number', 'created_at')
            .order_by('-created_at')
        )

    def location(self, obj):
        return reverse('manga:chapter_detail', args=[obj.manga.slug, obj.chapter_number])

    def lastmod(self, obj):
        return obj.created_at


class MangaCategorySitemap(Sitemap):
    changefreq  = 'weekly'
    priority    = 0.6
    protocol    = 'https'

    def items(self):
        return MangaCategory.objects.filter(is_active=True)

    def location(self, obj):
        return reverse('manga:category_detail', args=[obj.slug])


# =============================================================================
# REGISTRY — imported by urls.py
# =============================================================================

sitemaps = {
    'static':           StaticViewSitemap(),
    'movies':           MovieSitemap(),
    'movie-categories': MovieCategorySitemap(),
    'anime':            AnimeSitemap(),
    'anime-episodes':   AnimeEpisodeSitemap(),
    'anime-categories': AnimeCategorySitemap(),
    'manga':            MangaSitemap(),
    'manga-chapters':   MangaChapterSitemap(),
    'manga-categories': MangaCategorySitemap(),
}