"""
automation/tasks.py

Two types of Telegram posts:
  1. NEW content  — when a Movie / Anime / Manga is scraped for the first time
  2. UPDATES      — when an existing series gets a new episode / chapter

Both run every hour via Celery Beat.
"""

import logging
from celery import shared_task
from django.conf import settings
from .telegram import send_photo, send_message
from .models import TelegramPost, TelegramUpdate

logger = logging.getLogger(__name__)

SITE_URL = lambda: getattr(settings, 'SITE_URL', 'https://watch2d.org')


# ============================================================
# MAIN SCHEDULED TASK — runs every hour
# ============================================================

@shared_task(name='automation.tasks.post_new_content_to_telegram')
def post_new_content_to_telegram():
    """Post new content AND episode/chapter updates every hour."""
    total = 0

    # --- NEW content ---
    total += _post_new_movies()
    total += _post_new_anime()
    total += _post_new_manga()

    # --- UPDATES (new episodes / chapters on existing series) ---
    total += _post_movie_updates()
    total += _post_anime_updates()
    total += _post_manga_updates()

    logger.info(f"Telegram batch complete. Posted: {total} items.")
    return f"Posted {total} items to Telegram."


# ============================================================
# NEW MOVIES
# ============================================================

def _post_new_movies() -> int:
    try:
        from movies.models import Movie
    except ImportError:
        return 0

    channel = getattr(settings, 'TELEGRAM_MOVIES_CHANNEL', '')
    if not channel:
        return 0

    posted_ids = TelegramPost.objects.filter(
        content_type='movie'
    ).values_list('content_id', flat=True)

    new_items = Movie.objects.exclude(id__in=posted_ids).order_by('-created_at')[:5]

    count = 0
    for movie in new_items:
        if _post_movie(movie, channel):
            count += 1
    return count


def _post_movie(movie, channel: str) -> bool:
    url = f"{SITE_URL()}/movie/{movie.pk}/"
    emoji = "🎬" if not movie.is_series else "📺"

    lines = [f"{emoji} <b>{movie.title}</b>", ""]

    if movie.description:
        lines.append(f"{movie.description[:200]}...")
        lines.append("")

    cats = movie.categories.all()
    if cats:
        lines.append(f"🏷 <b>Genre:</b> {', '.join(c.name for c in cats[:4])}")

    if movie.is_series:
        status = "✅ Completed" if movie.completed else "🔄 Ongoing Series"
        lines.append(f"📡 <b>Status:</b> {status}")

    lines += ["", "⬇️ <b>Tap the Download Link button below</b> 👇",
              "", "#Watch2D #Movie #FreeStream"]

    return _send_and_record_new(
        'movie', movie.id, movie.title,
        movie.image_url or '', "\n".join(lines), channel,
        button_url=url,
    )


# ============================================================
# MOVIE EPISODE UPDATES
# ============================================================

def _post_movie_updates() -> int:
    """
    Post update notifications for series that have a new episode.
    Uses the Movie.title_b field (which stores episode info like "Episode 5")
    and Movie.title_b_updated_at to detect new updates.
    """
    try:
        from movies.models import Movie
    except ImportError:
        return 0

    channel = getattr(settings, 'TELEGRAM_MOVIES_CHANNEL', '')
    if not channel:
        return 0

    # Only check series that have an episode update (title_b is set)
    updated_series = Movie.objects.filter(
        is_series=True,
        title_b__isnull=False,
    ).exclude(title_b='').order_by('-title_b_updated_at')[:20]

    count = 0
    for movie in updated_series:
        # The update_key is the episode label stored in title_b
        # e.g. "Episode 5", "S02E03", "Episode 12 (Final)"
        update_key = movie.title_b.strip()

        # Skip if we already posted this exact update
        already_posted = TelegramUpdate.objects.filter(
            content_type='movie',
            content_id=movie.id,
            update_key=update_key,
        ).exists()

        if already_posted:
            continue

        if _post_movie_episode_update(movie, update_key, channel):
            count += 1

    return count


def _post_movie_episode_update(movie, episode_label: str, channel: str) -> bool:
    """Post a 'new episode available' notification for a series."""
    url = f"{SITE_URL()}/movie/{movie.pk}/"

    lines = [
        f"🆕 <b>New Episode Available!</b>",
        f"",
        f"📺 <b>{movie.title}</b>",
        f"🎬 <b>Episode:</b> {episode_label}",
        f"",
    ]

    cats = movie.categories.all()
    if cats:
        lines.append(f"🏷 <b>Genre:</b> {', '.join(c.name for c in cats[:3])}")

    lines += [
        "",
        "⬇️ <b>Tap the Download Link button below</b> 👇",
        "",
        "#Watch2D #NewEpisode #Series",
    ]

    return _send_and_record_update(
        'movie', movie.id, movie.title, episode_label,
        movie.image_url or '', "\n".join(lines), channel,
        button_url=url,
    )


# ============================================================
# NEW ANIME
# ============================================================

def _post_new_anime() -> int:
    try:
        from anime.models import Anime
    except ImportError:
        return 0

    channel = getattr(settings, 'TELEGRAM_ANIME_CHANNEL', '')
    if not channel:
        return 0

    posted_ids = TelegramPost.objects.filter(
        content_type='anime'
    ).values_list('content_id', flat=True)

    new_items = Anime.objects.exclude(id__in=posted_ids).order_by('-created_at')[:5]

    count = 0
    for anime in new_items:
        if _post_anime(anime, channel):
            count += 1
    return count


def _post_anime(anime, channel: str) -> bool:
    slug  = getattr(anime, 'slug', None) or anime.pk
    url   = f"{SITE_URL()}/anime/{slug}/"
    lines = [f"🎌 <b>{anime.title}</b>", ""]

    desc = getattr(anime, 'description', '') or getattr(anime, 'synopsis', '') or ''
    if desc:
        lines += [f"{desc[:200]}...", ""]

    ep_count = getattr(anime, 'episode_count', None) or getattr(anime, 'total_episodes', None)
    if ep_count:
        lines.append(f"📺 <b>Episodes:</b> {ep_count}")

    status = getattr(anime, 'status', '') or getattr(anime, 'airing_status', '')
    if status:
        lines.append(f"📡 <b>Status:</b> {status}")

    genres = getattr(anime, 'genres', None) or getattr(anime, 'categories', None)
    if genres:
        try:
            names = ", ".join(g.name for g in genres.all()[:4])
            if names:
                lines.append(f"🏷 <b>Genre:</b> {names}")
        except Exception:
            pass

    lines += ["", f"🔗 <a href='{url}'>Watch on Watch2D</a>", "", "#Watch2D #Anime #FreeAnime"]

    cover = (
        getattr(anime, 'cover_image', '') or
        getattr(anime, 'image_url', '') or
        getattr(anime, 'poster', '') or ''
    )

    return _send_and_record_new('anime', anime.id, anime.title, cover, "\n".join(lines), channel)


# ============================================================
# ANIME EPISODE UPDATES
# ============================================================

def _post_anime_updates() -> int:
    """Post new episode notifications for airing anime."""
    try:
        from anime.models import Anime, Episode
    except ImportError:
        return 0

    channel = getattr(settings, 'TELEGRAM_ANIME_CHANNEL', '')
    if not channel:
        return 0

    count = 0

    # Get recently added episodes (last 20)
    try:
        recent_episodes = Episode.objects.select_related('anime').order_by('-created_at')[:20]
    except Exception:
        # Try other common field names
        try:
            recent_episodes = Episode.objects.select_related('anime').order_by('-added_at')[:20]
        except Exception:
            return 0

    for episode in recent_episodes:
        anime = episode.anime
        ep_num = str(getattr(episode, 'episode_number', '') or getattr(episode, 'number', '') or '')
        if not ep_num:
            continue

        update_key = f"ep-{ep_num}"

        already_posted = TelegramUpdate.objects.filter(
            content_type='anime',
            content_id=anime.id,
            update_key=update_key,
        ).exists()

        if already_posted:
            continue

        if _post_anime_episode_update(anime, episode, ep_num, channel):
            count += 1

    return count


def _post_anime_episode_update(anime, episode, ep_num: str, channel: str) -> bool:
    slug = getattr(anime, 'slug', None) or anime.pk
    url  = f"{SITE_URL()}/anime/watch/{slug}/episode/{ep_num}/"

    ep_title = getattr(episode, 'title', '') or f"Episode {ep_num}"

    lines = [
        f"🆕 <b>New Anime Episode!</b>",
        f"",
        f"🎌 <b>{anime.title}</b>",
        f"▶️ <b>{ep_title}</b>",
        f"",
        f"🔗 <a href='{url}'>Watch Now on Watch2D</a>",
        f"",
        f"#Watch2D #Anime #NewEpisode",
    ]

    cover = (
        getattr(anime, 'cover_image', '') or
        getattr(anime, 'image_url', '') or ''
    )

    return _send_and_record_update(
        'anime', anime.id, anime.title, f"ep-{ep_num}",
        cover, "\n".join(lines), channel
    )


# ============================================================
# NEW MANGA
# ============================================================

def _post_new_manga() -> int:
    try:
        from manga.models import Manga
    except ImportError:
        return 0

    channel = getattr(settings, 'TELEGRAM_MANGA_CHANNEL', '')
    if not channel:
        return 0

    posted_ids = TelegramPost.objects.filter(
        content_type='manga'
    ).values_list('content_id', flat=True)

    new_items = Manga.objects.exclude(id__in=posted_ids).order_by('-created_at')[:5]

    count = 0
    for manga in new_items:
        if _post_manga(manga, channel):
            count += 1
    return count


def _post_manga(manga, channel: str) -> bool:
    slug  = getattr(manga, 'slug', None) or manga.pk
    url   = f"{SITE_URL()}/manga/{slug}/"
    lines = [f"📖 <b>{manga.title}</b>", ""]

    desc = getattr(manga, 'description', '') or getattr(manga, 'synopsis', '') or ''
    if desc:
        lines += [f"{desc[:200]}...", ""]

    total_ch = getattr(manga, 'total_chapters', None)
    if total_ch:
        lines.append(f"📚 <b>Chapters:</b> {total_ch}")

    status = getattr(manga, 'status', '')
    if status:
        lines.append(f"📡 <b>Status:</b> {status}")

    cats = getattr(manga, 'categories', None) or getattr(manga, 'genres', None)
    if cats:
        try:
            names = ", ".join(c.name for c in cats.all()[:4])
            if names:
                lines.append(f"🏷 <b>Genre:</b> {names}")
        except Exception:
            pass

    lines += ["", f"🔗 <a href='{url}'>Read on Watch2D</a>", "", "#Watch2D #Manga #ReadManga"]

    cover = (
        getattr(manga, 'cover_image', '') or
        getattr(manga, 'cover_url', '') or
        getattr(manga, 'image_url', '') or ''
    )

    return _send_and_record_new('manga', manga.id, manga.title, cover, "\n".join(lines), channel)


# ============================================================
# MANGA CHAPTER UPDATES
# ============================================================

def _post_manga_updates() -> int:
    """Post new chapter notifications for ongoing manga."""
    try:
        from manga.models import Manga, Chapter
    except ImportError:
        return 0

    channel = getattr(settings, 'TELEGRAM_MANGA_CHANNEL', '')
    if not channel:
        return 0

    count = 0

    try:
        recent_chapters = Chapter.objects.select_related('manga').order_by('-created_at')[:20]
    except Exception:
        try:
            recent_chapters = Chapter.objects.select_related('manga').order_by('-uploaded_at')[:20]
        except Exception:
            return 0

    for chapter in recent_chapters:
        manga    = chapter.manga
        ch_num   = str(
            getattr(chapter, 'chapter_number', '') or
            getattr(chapter, 'number', '') or ''
        )
        if not ch_num:
            continue

        update_key = f"ch-{ch_num}"

        already_posted = TelegramUpdate.objects.filter(
            content_type='manga',
            content_id=manga.id,
            update_key=update_key,
        ).exists()

        if already_posted:
            continue

        if _post_manga_chapter_update(manga, chapter, ch_num, channel):
            count += 1

    return count


def _post_manga_chapter_update(manga, chapter, ch_num: str, channel: str) -> bool:
    slug  = getattr(manga, 'slug', None) or manga.pk
    url   = f"{SITE_URL()}/manga/read/{slug}/chapter-{ch_num}/"

    ch_title = getattr(chapter, 'title', '') or f"Chapter {ch_num}"

    lines = [
        f"🆕 <b>New Chapter Available!</b>",
        f"",
        f"📖 <b>{manga.title}</b>",
        f"📄 <b>{ch_title}</b>",
        f"",
        f"🔗 <a href='{url}'>Read Now on Watch2D</a>",
        f"",
        f"#Watch2D #Manga #NewChapter",
    ]

    cover = (
        getattr(manga, 'cover_image', '') or
        getattr(manga, 'cover_url', '') or
        getattr(manga, 'image_url', '') or ''
    )

    return _send_and_record_update(
        'manga', manga.id, manga.title, f"ch-{ch_num}",
        cover, "\n".join(lines), channel
    )


# ============================================================
# SHARED HELPERS
# ============================================================

def _download_button(url: str) -> dict:
    """A big tappable 'Download Link' button under the post (Nkiri-style) that
    sends users to our site to complete the download (where the gate + ads are)."""
    return {'inline_keyboard': [[{'text': '⬇️ Download Link', 'url': url}]]}


def _send_and_record_new(
    content_type: str, content_id: int, title: str,
    image_url: str, caption: str, channel: str,
    button_url: str = None,
) -> bool:
    """Send new-content post and record in TelegramPost."""
    markup = _download_button(button_url) if button_url else None
    try:
        result = (send_photo(channel, image_url, caption, reply_markup=markup)
                  if image_url
                  else send_message(channel, caption, reply_markup=markup))
        msg_id = str(result.get('result', {}).get('message_id', ''))
        TelegramPost.objects.create(
            content_type=content_type,
            content_id=content_id,
            content_title=title,
            telegram_message_id=msg_id,
            success=True,
        )
        logger.info(f"[NEW] Posted {content_type}: {title}")
        return True
    except Exception as e:
        TelegramPost.objects.get_or_create(
            content_type=content_type,
            content_id=content_id,
            defaults={'content_title': title, 'success': False, 'error_message': str(e)},
        )
        logger.error(f"[NEW] Failed {content_type} '{title}': {e}")
        return False


def _send_and_record_update(
    content_type: str, content_id: int, title: str,
    update_key: str, image_url: str, caption: str, channel: str,
    button_url: str = None,
) -> bool:
    """Send update post and record in TelegramUpdate."""
    markup = _download_button(button_url) if button_url else None
    try:
        result = (send_photo(channel, image_url, caption, reply_markup=markup)
                  if image_url
                  else send_message(channel, caption, reply_markup=markup))
        msg_id = str(result.get('result', {}).get('message_id', ''))
        TelegramUpdate.objects.create(
            content_type=content_type,
            content_id=content_id,
            content_title=title,
            update_key=update_key,
            telegram_message_id=msg_id,
            success=True,
        )
        logger.info(f"[UPDATE] Posted {content_type} update: {title} — {update_key}")
        return True
    except Exception as e:
        TelegramUpdate.objects.get_or_create(
            content_type=content_type,
            content_id=content_id,
            update_key=update_key,
            defaults={'content_title': title, 'success': False, 'error_message': str(e)},
        )
        logger.error(f"[UPDATE] Failed {content_type} '{title}' update '{update_key}': {e}")
        return False


# ============================================================
# INDIVIDUAL MANUAL TASKS (use from Django shell to test)
# ============================================================

@shared_task(name='automation.tasks.post_movies_to_telegram')
def post_movies_to_telegram():
    n = _post_new_movies() + _post_movie_updates()
    return f"Movies: {n} posted."

@shared_task(name='automation.tasks.post_anime_to_telegram')
def post_anime_to_telegram():
    n = _post_new_anime() + _post_anime_updates()
    return f"Anime: {n} posted."

@shared_task(name='automation.tasks.post_manga_to_telegram')
def post_manga_to_telegram():
    n = _post_new_manga() + _post_manga_updates()
    return f"Manga: {n} posted."