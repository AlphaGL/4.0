"""
Shared helpers for all scrapers:

  • is_valid_download_url(url)  — reject source pages, ad domains, malformed URLs
  • normalize_title(title)      — canonical key so "From S01" == "From Season 1"
  • find_duplicate_movie(title) — return an existing Movie that is the same title
                                  in a different notation/casing (dedupe on insert)

Keeping this in one place means every scraper stays consistent, and it matches
the one-off DB cleanup that was run.
"""
import re
from urllib.parse import urlparse

# Hosts that are NOT real download links (source/info sites, ad/redirect
# domains, social/video). Anything matching these is dropped.
JUNK_DOWNLOAD_HOSTS = {
    'thenkiri.com', 'asianwiki.com', 'mydramalist.com', 'deloplen.com',
    '9jarocks.net', 't.me', 'youtu.be', 'youtube.com', 'www.youtube.com',
    'bit.ly', 'oladblock.me',
}


def is_valid_download_url(url):
    """True only for plausible file-host download URLs."""
    if not url or not isinstance(url, str):
        return False
    try:
        host = (urlparse(url.strip()).netloc or '').lower()
    except Exception:
        return False
    # malformed: no host, or host isn't a domain (e.g. a title fragment)
    if not host or '.' not in host:
        return False
    # strip a leading "wwwNN." so www42.loadedfiles.org -> loadedfiles.org
    base = re.sub(r'^www\d*\.', '', host)
    for junk in JUNK_DOWNLOAD_HOSTS:
        if base == junk or host == junk or base.endswith('.' + junk):
            return False
    return True


def filter_download_urls(urls):
    """Keep only valid download URLs, de-duplicated, preserving order."""
    seen, out = set(), []
    for u in urls or []:
        if is_valid_download_url(u) and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def normalize_title(title):
    """
    Canonical comparison key. Mirrors the DB cleanup:
      - lowercase
      - "S01"/"s1"/"S01E05" -> "season 1"  (keeps the season number distinct)
      - drop "(complete)" / "completed"
      - collapse all punctuation/whitespace
    So "From S01", "From Season 1", "From S01 (Complete)" -> "from season 1",
    while "Money Heist S01" and "Money Heist S02" stay different.
    """
    if not title:
        return ''
    t = title.lower()
    t = re.sub(r'\bs0*(\d+)(e\d+)?\b', r'season \1', t)   # S01 / S1 / S01E05 -> season 1
    t = re.sub(r'\(?\s*complete[d]?\s*\)?', ' ', t)        # (complete)/(completed)
    t = re.sub(r'[^a-z0-9]+', ' ', t)                      # punctuation/space
    return t.strip()


def find_movie_by_normalized_title(title):
    """
    Fallback de-dup for scrapers: return an existing Movie whose *normalized*
    title matches this one's. Catches punctuation/spacing differences the
    scrapers' exact `title__in` match misses — e.g. a source re-titling
    "Korea No.1" as "Korea No. 1", or "It's Okay!" vs "It's Okay". Without this,
    the scraper creates a duplicate, posts it to Telegram, and cleanse_db later
    deletes the dup → dead Telegram link (404) + endless reposting.

    Narrows the candidate set by the most distinctive token so the scan stays
    small; returns the first candidate whose normalized title matches exactly.
    """
    from movies.models import Movie
    norm = normalize_title(title)
    if not norm:
        return None
    _STOP = {'season', 'complete', 'completed', 'episode', 'the', 'and',
             'movie', 'series', 'part', 'added', 'ongoing', 'download'}
    tokens = [t for t in norm.split() if len(t) >= 4 and t not in _STOP]
    if not tokens:
        tokens = [t for t in norm.split() if t not in _STOP] or norm.split()
    if not tokens:
        return None
    token = max(tokens, key=len)
    for m in Movie.objects.filter(title__icontains=token).only('id', 'title')[:500]:
        if normalize_title(m.title) == norm:
            return m
    return None


def parse_show(title):
    """
    Split a title into (show_key, season_number) so every season of a show can
    be grouped under one parent.

      "From S01"          -> ("from", 1)
      "From Season 2"     -> ("from", 2)
      "From S01 (Complete)" -> ("from", 1)
      "From"              -> ("from", None)
      "Scary Movie"       -> ("scary-movie", None)

    show_key is a hyphenated, season-stripped slug (built from normalize_title,
    so it matches the same canonical form used for dedupe). season_number is the
    int season parsed from the title, or None for movies / unseasoned titles.
    """
    norm = normalize_title(title)                       # e.g. "from season 1"
    season = None
    m = re.search(r'\bseason (\d+)\b', norm)
    if m:
        season = int(m.group(1))
        norm = re.sub(r'\bseason \d+\b', ' ', norm)     # strip the season part
    norm = re.sub(r'\s+', ' ', norm).strip()
    key = re.sub(r'\s+', '-', norm)
    return key, season


# Maps the many name variants the scrapers produce to the ONE canonical
# category name (matching the cleaned-up DB). Keys are lowercased.
CATEGORY_ALIASES = {
    'series': 'TV Series', 'tv series': 'TV Series',
    'hollywood': 'Hollywood Movies', 'hollywood movie': 'Hollywood Movies',
    'hollywood movies': 'Hollywood Movies',
    'hollywood tv series': 'Hollywood TV Series',
    'nollywood': 'Nollywood Movies', 'nollywood movie': 'Nollywood Movies',
    'nollywood movies': 'Nollywood Movies',
    'nollywood tv series': 'Nollywood TV Series',
    'k drama': 'Korean Drama', 'korean drama': 'Korean Drama',
    'movie': 'Movies', 'movies': 'Movies',
    '18+ movie': 'Adult (18+)', '18plus': 'Adult (18+)', '18+': 'Adult (18+)',
    'adult': 'Adult (18+)',
    'animation': 'Animation', 'animation movie': 'Animation',
    'bollywood': 'Bollywood Movies', 'bollywood movies': 'Bollywood Movies',
    'filipino': 'Filipino Drama', 'filipino drama': 'Filipino Drama',
    'wrestling': 'Wrestling',
    'other foreign movies': 'Other Foreign Movies',
    'other foreign series': 'Other Foreign Movies',
    'other foreign': 'Other Foreign Movies',
    'chinese drama': 'Chinese Drama',
    'chinese movie': 'Chinese Movies', 'chinese movies': 'Chinese Movies',
    'thai drama': 'Thai Drama', 'turkish drama': 'Turkish Drama',
    'spanish drama': 'Spanish Drama',
    'sa series': 'SA Series', 'south africa': 'South Africa',
    'sci-fi': 'Sci-Fi', 'sci fi': 'Sci-Fi', 'scifi': 'Sci-Fi',
    'reality-tv': 'Reality TV', 'reality tv': 'Reality TV',
    'ongoing': 'Ongoing', 'anime': 'Anime',
}


def canonical_category_name(name):
    """Return the canonical category name for any scraper-supplied variant."""
    raw = (name or '').strip()
    if not raw:
        return ''
    return CATEGORY_ALIASES.get(raw.lower(), raw)


def get_or_create_category(name, model=None):
    """
    Return the existing canonical Category (case-insensitive), creating it only
    if it genuinely doesn't exist. Prevents the "Series" vs "TV Series" and
    "Hollywood movies" vs "Hollywood Movies" duplicate-category problem.
    """
    if model is None:
        from movies.models import Category as model
    canonical = canonical_category_name(name)
    if not canonical:
        return None
    obj = model.objects.filter(name__iexact=canonical).first()
    if obj:
        return obj
    return model.objects.create(name=canonical)


def telegram_download_buttons(movie) -> dict:
    """
    Inline keyboard for a movie/episode Telegram channel post. Shared by
    automation/tasks.py and every scraper's own _post_movie_to_telegram, so
    the button behavior stays identical everywhere it's built.

    '⬇️ Download on Website' always goes straight to the plain website —
    Telegram's Mini App WebView can't handle file downloads or free page
    navigation, so there's no benefit routing this one through it.

    '⚡ Download on Telegram' — a deep link into the bot's ad-gated delivery
    flow (run_telegram_bot.py) — is always shown alongside it now that the
    bot is deployed and verified working.
    """
    from django.conf import settings

    site_url = getattr(settings, 'SITE_URL', 'https://watch2d.org').rstrip('/')
    slug = getattr(movie, 'slug', '') or ''
    view_url = f"{site_url}/movie/{movie.pk}/{slug}/" if slug else f"{site_url}/movie/{movie.pk}/"
    bot_username = getattr(settings, 'TELEGRAM_BOT_USERNAME', 'watch2d_bot')

    rows = [
        [{'text': '⬇️ Download on Website', 'url': view_url}],
        [{
            'text': '⚡ Download on Telegram',
            'url': f"https://t.me/{bot_username}?start=movie{movie.pk}",
        }],
    ]

    rows.append([{
        'text': '📲 Get the Watch2D App',
        'url': 'https://dl.watch2d.org/watch2d-latest.apk',
    }])

    return {'inline_keyboard': rows}


def find_duplicate_movie(title, model=None):
    """
    Return an existing Movie whose normalized title matches `title`, or None.
    Pass the Movie model to avoid an import cycle, or it's imported lazily.
    """
    if model is None:
        from movies.models import Movie as model
    key = normalize_title(title)
    if not key:
        return None
    # Fast path: exact title (case-insensitive) match first.
    exact = model.objects.filter(title__iexact=title).first()
    if exact:
        return exact
    # Fallback: scan candidates sharing the first word (cheap prefix filter),
    # then compare normalized keys in Python.
    first_word = key.split(' ', 1)[0]
    if not first_word:
        return None
    for m in model.objects.filter(title__icontains=first_word).only('id', 'title')[:200]:
        if normalize_title(m.title) == key:
            return m


def sync_download_links(movie, parsed_download_links, uploaded_message_id=None, uploaded_landing_url=None):
    """
    Sync a movie's DownloadLink rows to match a freshly scraped list, and
    detect the "stream-only movie just got its first download link" moment.

    Every scraper (9jarocks, thenkiri, ...) had its own copy of this exact
    sync loop — pulled into one place so the resurface behavior below only
    needs to exist once.

    Resurfacing: if this movie had a stream_url but zero download links
    before this call, and now has at least one, it's treated as newsworthy
    again — created_at is bumped so it reappears at the top of "latest"
    listings, and the caller is told (via the returned `resurfaced` flag)
    to re-post it to Telegram/social exactly like a brand-new movie.
    """
    from movies.models import DownloadLink
    from django.utils import timezone

    had_links_before = movie.download_links.exists()

    existing = {normalize_url(dl.url): dl for dl in movie.download_links.all()}
    current  = {normalize_url(dl['url']): dl for dl in parsed_download_links if is_valid_download_url(dl['url'])}
    added = 0

    for norm, dl in current.items():
        if norm not in existing:
            new_link = DownloadLink.objects.create(movie=movie, label=dl['label'], url=dl['url'])
            added += 1
            if uploaded_message_id and uploaded_landing_url and norm == normalize_url(uploaded_landing_url):
                new_link.telegram_message_id = uploaded_message_id
                new_link.save(update_fields=['telegram_message_id'])
        else:
            if existing[norm].label != dl['label']:
                existing[norm].label = dl['label']
                existing[norm].save()

    for norm in set(existing) - set(current):
        existing[norm].delete()

    resurfaced = False
    if not had_links_before and current and movie.stream_url:
        movie.created_at = timezone.now()
        movie.save(update_fields=['created_at'])
        resurfaced = True

    return added, resurfaced


# Captures season + episode straight from a scraped filename/label, e.g.
# "Attack.On.Titan.S02E01.540p..." — independent of which Movie record the
# link happens to be attached to. This is what split_merged_seasons used to
# find ~7,000 episodes that had been dumped onto the wrong season; the same
# check now runs at scrape time so it doesn't happen again going forward.
_SXE_RE = re.compile(r'[Ss](\d{1,2})[Ee](\d{1,3})')

# Matches the season token in a human title so a new season's title can be
# built from a sibling's by swapping just the number, e.g.
#   "Attack on Titan Season 1 (Complete)" -> "...Season 2 (Complete)"
#   "The Bear S01 (Complete)"             -> "...S02 (Complete)"
_TITLE_SEASON_RE = re.compile(r'\b(Season\s*|S)(\d{1,2})\b', re.IGNORECASE)


def build_season_title(template_title: str, new_season: int) -> str:
    """Swap the season number in an existing title to build a sibling season's title."""
    def repl(m):
        prefix, digits = m.group(1), m.group(2)
        if prefix.strip().lower().startswith('season'):
            return f'Season {new_season}'
        return f'S{new_season:0{len(digits)}d}'

    new_title, n = _TITLE_SEASON_RE.subn(repl, template_title, count=1)
    if n == 0:
        new_title = f'{template_title} Season {new_season}'
    return new_title


def get_or_create_sibling_season(template_movie, new_season: int):
    """
    Find the Movie for (template_movie.show_key, new_season), or create one
    by cloning template_movie's metadata (image, description, genre, cast,
    country, language, categories) and swapping the season number/title.
    """
    from movies.models import Movie

    target = Movie.objects.filter(
        show_key=template_movie.show_key, season_number=new_season,
    ).first()
    if target:
        return target, False

    new_title = build_season_title(template_movie.title, new_season)
    if Movie.objects.filter(title=new_title).exists():
        # Rare exact-title collision with an unrelated movie — disambiguate
        # rather than crash on the unique constraint.
        new_title = f"{new_title} ({template_movie.show_key})"

    new_movie = Movie.objects.create(
        title=new_title,
        is_series=True,
        completed=template_movie.completed,
        show_key=template_movie.show_key,
        season_number=new_season,
        description=template_movie.description,
        video_url='',
        image_url=template_movie.image_url,
        scraped=True,
        vi_country=template_movie.vi_country,
        vi_language=template_movie.vi_language,
        vi_cast=template_movie.vi_cast,
        vi_genre=template_movie.vi_genre,
        vi_year=template_movie.vi_year,
        vi_subtitle=template_movie.vi_subtitle,
    )
    new_movie.categories.set(template_movie.categories.all())
    return new_movie, True


def route_and_sync_download_links(movie, parsed_download_links, uploaded_message_id=None, uploaded_landing_url=None):
    """
    Like sync_download_links(), but first splits the scraped links by the
    season each one's own url/label actually indicates — so a source page
    that bundles multiple seasons' episodes into one post (a real pattern
    seen on 9jarocks/thenkiri) doesn't dump them all onto whichever season
    the page's own title happened to match.

    Links matching `movie`'s own season sync onto `movie` as normal. Links
    for a different season of the same show are routed to that season's
    Movie record instead (found by show_key + season_number, or created by
    cloning `movie`'s metadata if it doesn't exist yet).

    Only applies when `movie` is an actual season record (is_series, a known
    season_number, and a show_key) — plain movies and unseasoned series are
    untouched and behave exactly like sync_download_links().

    Returns a list of (target_movie, added, resurfaced, is_new_movie) —
    normally just one entry for `movie` itself, with extra entries for any
    sibling-season movie that received routed links this call. Callers
    should post to social/Telegram for every entry where resurfaced or
    is_new_movie is True, not just check the first one.
    """
    if not (movie.is_series and movie.season_number is not None and movie.show_key):
        added, resurfaced = sync_download_links(
            movie, parsed_download_links, uploaded_message_id, uploaded_landing_url)
        return [(movie, added, resurfaced, False)]

    own_links = []
    by_season = {}
    for dl in parsed_download_links:
        text = dl.get('url') or dl.get('label') or ''
        m = _SXE_RE.search(text)
        if not m:
            own_links.append(dl)
            continue
        found_season = int(m.group(1))
        if found_season == movie.season_number:
            own_links.append(dl)
        else:
            by_season.setdefault(found_season, []).append(dl)

    results = []
    added, resurfaced = sync_download_links(
        movie, own_links, uploaded_message_id, uploaded_landing_url)
    results.append((movie, added, resurfaced, False))

    for season, links in by_season.items():
        target, is_new_movie = get_or_create_sibling_season(movie, season)
        t_added, t_resurfaced = sync_download_links(
            target, links, uploaded_message_id, uploaded_landing_url)
        results.append((target, t_added, t_resurfaced or is_new_movie, is_new_movie))

    return results


def normalize_url(url: str) -> str:
    from urllib.parse import unquote
    parsed = urlparse(url)
    return unquote(f"{parsed.scheme}://{parsed.netloc}{parsed.path}").lower()
    return None
