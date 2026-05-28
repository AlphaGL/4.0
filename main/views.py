from django.shortcuts import render
from django.views.generic import TemplateView
from django.db.models import Q, Prefetch
from django.http import JsonResponse

# Import models — only Movies, Anime, Manga
from movies.models import Movie, Category as MovieCategory
from anime.models import Anime, Episode
from manga.models import Manga, Chapter


def ping_view(request):
    return JsonResponse({"status": "OK"})


# ============================================
# Custom Error Handlers
# ============================================

def custom_404_view(request, exception):
    context = {'exception': str(exception) if exception else None}
    return render(request, '404.html', context, status=404)


def custom_500_view(request):
    return render(request, '500.html', {}, status=500)


def custom_403_view(request, exception):
    context = {'exception': str(exception) if exception else None}
    return render(request, '403.html', context, status=403)


def custom_400_view(request, exception):
    context = {'exception': str(exception) if exception else None}
    return render(request, '400.html', context, status=400)


def custom_503_view(request):
    return render(request, '503.html', status=503)


class UnifiedHomeView(TemplateView):
    """
    Unified homepage combining Movies (primary), Anime, and Manga.
    Movies is the hero content; anime & manga appear below as discovery sections.
    """
    template_name = 'main/home.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # ========== MOVIES — Primary content ==========
        context['featured_movies'] = Movie.objects.filter(
            is_blockbuster=True
        ).select_related().prefetch_related('categories').order_by('-created_at')[:12]

        context['trending_movies'] = Movie.objects.filter(
            views__gt=0
        ).order_by('-views', '-created_at')[:24]

        context['latest_movies'] = Movie.objects.filter(
            Q(title_b__isnull=True) | Q(title_b='')
        ).order_by('-created_at')[:12]

        context['movie_categories'] = MovieCategory.objects.all()

        # ========== ANIME ==========
        featured_anime = Anime.objects.filter(
            is_active=True, is_featured=True
        ).prefetch_related(
            Prefetch(
                'episodes',
                queryset=Episode.objects.filter(is_active=True).order_by('-episode_number')[:5],
                to_attr='latest_episodes_list'
            )
        ).select_related('category').prefetch_related('genres').order_by('-created_at')[:3]

        if not featured_anime.exists():
            featured_anime = Anime.objects.filter(is_active=True).prefetch_related(
                Prefetch(
                    'episodes',
                    queryset=Episode.objects.filter(is_active=True).order_by('-episode_number')[:5],
                    to_attr='latest_episodes_list'
                )
            ).select_related('category').prefetch_related('genres').order_by('-created_at')[:3]

        context['featured_anime'] = featured_anime

        context['trending_anime'] = Anime.objects.filter(
            is_active=True, is_trending=True
        ).order_by('-views')[:12]

        if not context['trending_anime'].exists():
            context['trending_anime'] = Anime.objects.filter(
                is_active=True
            ).order_by('-created_at')[:12]

        context['latest_episodes'] = Episode.objects.filter(
            is_active=True, anime__is_active=True
        ).select_related('anime', 'anime__category').order_by('-created_at')[:12]

        # ========== MANGA ==========
        featured_manga = Manga.objects.filter(
            is_active=True, is_featured=True
        ).prefetch_related(
            Prefetch(
                'chapters',
                queryset=Chapter.objects.filter(is_active=True).order_by('-chapter_number')[:5],
                to_attr='latest_chapters_list'
            )
        ).select_related('category').prefetch_related('genres').order_by('-created_at')[:3]

        if not featured_manga.exists():
            featured_manga = Manga.objects.filter(is_active=True).prefetch_related(
                Prefetch(
                    'chapters',
                    queryset=Chapter.objects.filter(is_active=True).order_by('-chapter_number')[:5],
                    to_attr='latest_chapters_list'
                )
            ).select_related('category').prefetch_related('genres').order_by('-created_at')[:3]

        context['featured_manga'] = featured_manga

        context['trending_manga'] = Manga.objects.filter(
            is_active=True, is_trending=True
        ).order_by('-views')[:12]

        if not context['trending_manga'].exists():
            context['trending_manga'] = Manga.objects.filter(
                is_active=True
            ).order_by('-created_at')[:12]

        context['latest_chapters'] = Chapter.objects.filter(
            is_active=True, manga__is_active=True
        ).select_related('manga', 'manga__category').order_by('-created_at')[:12]

        # ========== HERO STATS ==========
        context['total_movies'] = Movie.objects.count()
        context['total_anime'] = Anime.objects.filter(is_active=True).count()
        context['total_manga'] = Manga.objects.filter(is_active=True).count()

        return context