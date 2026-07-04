"""
Management command: scrape_news  ("Gist" feed)

Gist is an ENGAGEMENT feature, not a news platform — so this doesn't dump every
article. It gathers candidates from entertainment RSS feeds, then lets Gemini
rank them for "juiciness" (celebrity drama, scandals, feuds, shocking reveals,
big castings/trailers — the stuff people actually react to) and keeps only the
top few per run. Dry trade/box-office/business news is filtered out.

~keep per run × runs/day ≈ a handful of juicy items daily.

Config:
  GEMINI_API_KEY / GEMINI_API_KEYS   (needed for curation; without it, falls
                                      back to newest-N, uncurated)

Usage:
  python manage.py scrape_news                       # top 3 juiciest this run
  python manage.py scrape_news --keep 3 --candidates-per-feed 15
"""
import re
from datetime import timezone as _tz
from email.utils import parsedate_to_datetime

import requests
from bs4 import BeautifulSoup
from django.core.management.base import BaseCommand
from django.utils import timezone

from movies.models import Movie, NewsPost

try:
    from movies.scene_id import _call_gemini, is_configured as _gemini_ready
except Exception:  # pragma: no cover
    _call_gemini = None
    def _gemini_ready():
        return False

UA = {'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                     'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 '
                     'Safari/537.36')}

# The ONLY categories allowed into the Gist feed. Anything that doesn't clearly
# fall into one of these is dropped (no "Other"/uncategorized bucket).
ALLOWED_CATEGORIES = {
    'Nollywood', 'Hollywood', 'K-Drama', 'Bollywood', 'Celebrity', 'Anime',
}

# (source name, fallback category, RSS url). Every fallback category MUST be in
# ALLOWED_CATEGORIES. (Music & Gaming feeds were removed on purpose.)
NEWS_FEEDS = [
    ('Variety',          'Hollywood', 'https://variety.com/feed/'),
    ('ScreenRant',       'Hollywood', 'https://screenrant.com/feed/'),
    ('SlashFilm',        'Hollywood', 'https://www.slashfilm.com/feed/'),
    ('Deadline',         'Hollywood', 'https://deadline.com/feed/'),
    ('ComingSoon',       'Hollywood', 'https://www.comingsoon.net/feed'),
    ('BellaNaija',       'Nollywood', 'https://www.bellanaija.com/feed/'),
    ('AnimeNewsNetwork', 'Anime',     'https://www.animenewsnetwork.com/newsroom/rss.xml'),
    ('Soompi',           'K-Drama',   'https://www.soompi.com/feed'),
]

_CURATE_PROMPT = (
    "You are the editor of a movie FAN app's gossip/buzz feed (NOT a trade "
    "paper). From the numbered headlines below, pick the JUICIEST, most "
    "reaction-worthy stories — celebrity drama, scandals, breakups, feuds, "
    "shocking reveals, huge castings, major trailers, big wins.\n"
    "IMPORTANT: SPREAD the picks ACROSS categories — up to {per_cat} per "
    "category — so Anime, K-Drama, Nollywood, Hollywood, Bollywood and Celebrity "
    "fans all get something. A category with nothing genuinely juicy: skip it "
    "(don't pad). IGNORE dry business / box-office / executive news, and IGNORE "
    "anything about music or gaming.\n"
    "Only use these categories: Nollywood, Hollywood, K-Drama, Bollywood, "
    "Celebrity, Anime. If a story fits NONE of them, DROP it (do not invent an "
    "'Other' category).\n"
    "For each pick return the item's number, a fun NON-abusive one-line hot "
    "take (<120 chars), its category, and the movie/show it's about (or null).\n"
    'Return STRICT JSON only: {"picks":[{"n":<number>,"category":"Nollywood|'
    'Hollywood|K-Drama|Bollywood|Celebrity|Anime",'
    '"hot_take":"...","title":"... or null"}]}\n\nHeadlines:\n{headlines}'
)


def _strip_html(html):
    if not html:
        return ''
    txt = BeautifulSoup(html, 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', txt).strip()


def _first_image(item):
    for tag, attr in (('enclosure', 'url'), ('media:content', 'url'),
                      ('media:thumbnail', 'url')):
        el = item.find(tag)
        if el and el.get(attr):
            return el.get(attr)
    for field in ('content:encoded', 'description'):
        el = item.find(field)
        if el and el.text:
            m = re.search(r'<img[^>]+src=["\']([^"\']+)', el.text)
            if m:
                return m.group(1)
    return ''


def _parse_date(item):
    el = item.find('pubDate') or item.find('published') or item.find('updated')
    if el and el.text:
        try:
            dt = parsedate_to_datetime(el.text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            return dt
        except Exception:
            pass
    return timezone.now()


def _match_movie(film_title):
    if not film_title or len(film_title) < 3:
        return None
    return (Movie.objects.filter(title__iexact=film_title).first()
            or Movie.objects.filter(title__icontains=film_title).first())


def _curate(candidates, per_cat):
    """Gemini → list of picks [{n, category, hot_take, title}] spread across
    categories, or None if it can't run (caller then falls back to newest-N)."""
    if not (_call_gemini and _gemini_ready()):
        return None
    lines = '\n'.join(
        f"{i}. [{c['source']}] {c['title']} — {c['summary'][:120]}"
        for i, c in enumerate(candidates))
    prompt = (_CURATE_PROMPT.replace('{per_cat}', str(per_cat))
              .replace('{headlines}', lines))
    body = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {
            'temperature': 0.4, 'response_mime_type': 'application/json'},
    }
    res = _call_gemini(body)
    if isinstance(res, dict) and isinstance(res.get('picks'), list):
        return res['picks']
    return None


class Command(BaseCommand):
    help = "Scrape + curate the juiciest entertainment news into the Gist feed."

    def add_arguments(self, parser):
        parser.add_argument('--per-category', type=int, default=2,
                            help='Max juiciest items kept per category per run.')
        parser.add_argument('--candidates-per-feed', type=int, default=12)
        parser.add_argument('--max-total', type=int, default=15,
                            help='Hard cap on total items saved per run.')

    def handle(self, *args, **opts):
        per_cat = opts['per_category']
        cpf = opts['candidates_per_feed']
        max_total = opts['max_total']

        # ── Gather NEW candidates from every feed ──────────────────────────────
        candidates = []
        for source, default_cat, url in NEWS_FEEDS:
            try:
                r = requests.get(url, headers=UA, timeout=20)
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.content, 'xml')
                items = (soup.find_all('item') or soup.find_all('entry'))[:cpf]
            except Exception:
                continue

            for it in items:
                link_el = it.find('link')
                link = (link_el.get('href') if link_el and link_el.get('href')
                        else (link_el.text.strip() if link_el else ''))
                title = _strip_html(it.title.text if it.title else '')
                if not link or not title:
                    continue
                if NewsPost.objects.filter(url=link).exists():
                    continue  # already have it
                desc = it.find('description') or it.find('summary')
                candidates.append({
                    'source': source,
                    'default_cat': default_cat,
                    'title': title,
                    'summary': _strip_html(desc.text if desc else '')[:500],
                    'url': link,
                    'image': _first_image(it),
                    'published': _parse_date(it),
                })

        if not candidates:
            self.stdout.write('Gist: no new candidates.')
            return

        # ── Pick the juiciest, spread across categories ───────────────────────
        picks = _curate(candidates, per_cat)
        chosen = []  # (candidate, enrichment)
        if picks is None:
            # No Gemini → just take the newest, uncurated.
            candidates.sort(key=lambda c: c['published'], reverse=True)
            chosen = [(c, {}) for c in candidates[:max_total]]
        else:
            per_cat_count = {}
            for p in picks:
                n = p.get('n')
                cat = ((p.get('category') or '').strip() or 'Other')
                if not (isinstance(n, int) and 0 <= n < len(candidates)):
                    continue
                if per_cat_count.get(cat, 0) >= per_cat:
                    continue  # enforce the per-category cap defensively
                per_cat_count[cat] = per_cat_count.get(cat, 0) + 1
                chosen.append((candidates[n], p))
                if len(chosen) >= max_total:
                    break

        # ── Save ──────────────────────────────────────────────────────────────
        added = 0
        skipped_cat = 0
        for c, enr in chosen:
            if NewsPost.objects.filter(url=c['url']).exists():
                continue
            # Only allow the whitelisted categories — drop anything else (incl.
            # music, gaming, or an 'Other'/blank category) so it never shows.
            category = (enr.get('category') or c['default_cat'] or '').strip()
            if category not in ALLOWED_CATEGORIES:
                skipped_cat += 1
                continue
            movie = _match_movie(enr.get('title'))
            NewsPost.objects.create(
                title=c['title'][:300],
                summary=c['summary'],
                hot_take=(enr.get('hot_take') or '')[:280],
                url=c['url'][:600],
                source=c['source'],
                image_url=(c['image'] or '')[:600],
                category=category[:60],
                movie=movie,
                tmdb_id=movie.tmdb_id if movie else None,
                published_at=c['published'],
            )
            added += 1

        cats = ', '.join(sorted({
            (enr.get('category') or c['default_cat']) for c, enr in chosen
        })) or '-'
        self.stdout.write(self.style.SUCCESS(
            f'Gist: reviewed {len(candidates)} candidate(s), kept {added} '
            f'juicy item(s) across [{cats}]'
            + (f', dropped {skipped_cat} off-category.' if skipped_cat else '.')))
