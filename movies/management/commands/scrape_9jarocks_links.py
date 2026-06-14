"""
Management command: scrape_9jarocks_links
==========================================
Scrape one or more specific 9jarocks post URLs — either passed directly on the
command line or read from a text file (one URL per line).

Unlike the main scraper that crawls category listing pages, this command goes
straight to the post pages you tell it about.  It uses the exact same parsers,
DB logic, and social-posting code as scrape_9jarocks.

Behaviour
─────────
• Movie (not a series)
    – If the movie already exists in the DB  → update any new fields, sync
      download links, report "unchanged / updated".
    – If it does NOT exist                   → create it, optionally post to socials.

• Series / ongoing show
    – Already exists, same episode label     → sync download links only (no social post).
    – Already exists, NEW episode label      → update title_b + title_b_updated_at,
      add any new download links, post to socials as "New Episode".
    – Does not exist                         → create it, post to socials as "New".

Category assignment
────────────────────
Because we are not crawling a category listing page, there is no "forced" DB
category.  Instead the scraper reads the post's own breadcrumb / post-cat tags
and maps them to your DB categories using the same mapping table used by the
main scraper.  You can also pass --category on the CLI to force an assignment
(same aliases as the main command).

Usage
─────
# Single URL on the CLI
python manage.py scrape_9jarocks_links \\
    --url "https://9jarocks.net/videodownload/mrs-fazilet-and-her-daughters-season-1-2-complete-turkish-id394535.html"

# Multiple URLs on the CLI
python manage.py scrape_9jarocks_links \\
    --url "https://9jarocks.net/videodownload/xxx.html" \\
    --url "https://9jarocks.net/videodownload/yyy.html"

# From a file (default: links.txt in the current working directory)
python manage.py scrape_9jarocks_links --file links.txt

# Force a DB category regardless of what the post page says
python manage.py scrape_9jarocks_links --file links.txt --category kdrama

# Skip social posting
python manage.py scrape_9jarocks_links --file links.txt --no-social

# Control request delay (seconds between fetches)
python manage.py scrape_9jarocks_links --file links.txt --delay 0.5

links.txt format
─────────────────
  # Lines starting with # are comments and are ignored.
  # Blank lines are also ignored.
  https://9jarocks.net/videodownload/movie-one-id123.html
  https://9jarocks.net/videodownload/series-two-season-3-id456.html
"""

import os
import re
import time

import urllib3
from urllib.parse import urlparse, unquote

from django.core.management.base import BaseCommand
from django.utils import timezone

from movies.models import Movie, Category, DownloadLink
from movies.scraper_utils import is_valid_download_url

# Re-use everything from the sibling scraper.
# Both files live in the same management/commands/ directory.
from .scrape_9jarocks import (
    # parsers / helpers
    _make_scraper,
    parse_post_page,
    clean_title_parts,
    find_existing_movie,
    normalize_url,
    assign_db_categories,
    # social posters
    _post_to_all_platforms,
    # constants
    SITE_URL,
    CATEGORY_DEFINITIONS,
    CATEGORY_ALIASES,
    _KEY_TO_DEF,
    _SLUG_TO_DEF,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY INFERENCE
# Map scraped post-cat / breadcrumb names → DB category names.
# This is only used when --category is not passed on the CLI.
# ══════════════════════════════════════════════════════════════════════════════

# Scraped tag  →  list of DB category names
# (lower-cased for matching)
_SCRAPED_TO_DB: dict[str, list[str]] = {
    'nollywood movie':        ['Nollywood movies'],
    'nollywood movies':       ['Nollywood movies'],
    'nollywood tv series':    ['Nollywood movies', 'Series'],
    'nollywood series':       ['Nollywood movies', 'Series'],
    'hollywood movie':        ['Hollywood movies'],
    'hollywood movies':       ['Hollywood movies'],
    'hollywood tv series':    ['Hollywood movies', 'Series'],
    'hollywood series':       ['Hollywood movies', 'Series'],
    'foreign movies':         ['Hollywood movies'],
    'other foreign movies':   ['Hollywood movies'],
    'other foreign series':   ['Hollywood movies', 'Series'],
    'foreign series':         ['Hollywood movies', 'Series'],
    'korean drama':           ['Korean drama'],
    'chinese drama':          ['Chinese drama'],
    'thai drama':             ['Thai drama'],
    'japanese drama':         ['Series'],
    'filipino drama':         ['Series'],
    'bollywood':              ['Bollywood movies'],
    'bollywood movies':       ['Bollywood movies'],
    'anime':                  ['Anime'],
    'pro wrestling':          ['wrestling'],
    'pro wrestling & fighting sports': ['wrestling'],
    'fighting sports':        ['wrestling'],
    'ongoing series':         ['Series'],
    'ongoing':                ['Series'],
    'turkish drama':          ['Series'],
    'arabic drama':           ['Series'],
    'indian drama':           ['Series'],
    '18+':                    ['18plus'],
    '18plus':                 ['18plus'],
}


def _infer_db_cats_from_scraped(scraped_cats: list[str]) -> list[str]:
    """
    Convert a list of scraped category/tag names from the post page into a
    deduplicated list of DB category names, using _SCRAPED_TO_DB.

    Falls back to ['Hollywood movies'] when nothing matches (safest generic
    bucket — won't make the movie disappear from the front page).
    """
    result: list[str] = []
    for raw in scraped_cats:
        key = raw.strip().lower()
        mapped = _SCRAPED_TO_DB.get(key)
        if mapped:
            for name in mapped:
                if name not in result:
                    result.append(name)

    if not result:
        # Last-resort fallback so the movie always gets at least one category
        result = ['Hollywood movies']

    return result


# ══════════════════════════════════════════════════════════════════════════════
# MANAGEMENT COMMAND
# ══════════════════════════════════════════════════════════════════════════════

class Command(BaseCommand):
    help = (
        'Scrape specific 9jarocks.net post URLs (from --url args or --file) '
        'and upsert them into the DB.  Handles both movies and series episode updates.'
    )

    # ── Argument definitions ───────────────────────────────────────────────

    def add_arguments(self, parser):
        parser.add_argument(
            '--url', action='append', dest='urls', default=[],
            metavar='URL',
            help=(
                'A 9jarocks post URL to scrape.  Can be repeated:\n'
                '  --url "https://9jarocks.net/videodownload/abc.html" \\\n'
                '  --url "https://9jarocks.net/videodownload/def.html"'
            ),
        )
        parser.add_argument(
            '--file', type=str, default='links.txt',
            metavar='PATH',
            help=(
                'Path to a text file containing one URL per line.\n'
                'Lines starting with # and blank lines are skipped.\n'
                'Default: links.txt in the current working directory.'
            ),
        )
        parser.add_argument(
            '--category', type=str, default=None,
            help=(
                'Force a specific DB category for all URLs in this run.\n'
                'Accepts the same aliases as the main scraper:\n'
                '  nollywood, hollywood, kdrama, chinese, thai, japanese,\n'
                '  filipino, anime, bollywood, foreign, series, wrestling,\n'
                '  ongoing, 18plus\n'
                'If omitted the category is inferred from the post page itself.'
            ),
        )
        parser.add_argument(
            '--no-social', action='store_true', default=False,
            help='Save to DB only — skip all social posts (Telegram, Facebook, Twitter).',
        )
        parser.add_argument(
            '--delay', type=float, default=0.5,
            help='Seconds to wait between HTTP requests (default: 0.5).',
        )

    # ── Entry point ────────────────────────────────────────────────────────

    def handle(self, *args, **options):
        from django.db import connection

        no_social     = options['no_social']
        delay         = options['delay']
        forced_db_cats: list[str] | None = None

        # ── Resolve --category → forced DB cat list ────────────────────────
        cat_arg = (options.get('category') or '').strip().lower()
        if cat_arg:
            forced_db_cats = self._resolve_forced_cats(cat_arg)
            if forced_db_cats is None:
                self.stderr.write(
                    f"❌  Unknown --category value '{cat_arg}'.\n"
                    f"    Valid aliases: nollywood, hollywood, kdrama, chinese, thai,\n"
                    f"    japanese, filipino, anime, bollywood, foreign, series,\n"
                    f"    wrestling, ongoing, 18plus\n"
                    f"    Or a full slug like 'videodownload/korean-drama'."
                )
                return

        # ── Collect URLs ───────────────────────────────────────────────────
        urls = list(options['urls'])                # from --url flags
        urls += self._load_urls_from_file(options['file'])
        urls = list(dict.fromkeys(u.strip() for u in urls if u.strip()))  # deduplicate, preserve order

        if not urls:
            self.stderr.write(
                "❌  No URLs provided.  Pass --url or --file with at least one URL.\n"
            )
            return

        # ── Banner ─────────────────────────────────────────────────────────
        print("=" * 60)
        print("🚀  9jarocks link scraper starting")
        print(f"    URLs      : {len(urls)}")
        print(f"    Category  : {cat_arg or '(auto-detect from post)'}")
        print(f"    Delay     : {delay}s")
        print(f"    Social    : {'DISABLED' if no_social else 'ON (Telegram + Facebook)'}")
        print("=" * 60)

        scraper = _make_scraper()

        total_created   = 0
        total_updated   = 0
        total_unchanged = 0
        total_skipped   = 0

        for idx, url in enumerate(urls, 1):
            print(f"\n{'─'*60}")
            print(f"[{idx}/{len(urls)}] 🌐 {url}")

            if delay > 0 and idx > 1:
                time.sleep(delay)

            # ── Fetch ──────────────────────────────────────────────────────
            try:
                resp = scraper.get(url, timeout=25)
                if resp.status_code != 200:
                    print(f"   ⚠️  HTTP {resp.status_code} — skipping.")
                    total_skipped += 1
                    continue
                html = resp.text
            except Exception as e:
                print(f"   ❌  Fetch error: {e}")
                total_skipped += 1
                continue

            # ── Parse ──────────────────────────────────────────────────────
            parsed = parse_post_page(html, url)
            if not parsed:
                print("   ⚠️  Could not parse post page — skipping.")
                total_skipped += 1
                continue

            if not parsed['download_links']:
                print(f"   ⛔  No download links found — skipping '{parsed['title_raw']}'")
                total_skipped += 1
                continue

            title, title_b = clean_title_parts(parsed['title_raw'])
            print(f"   📝 Title   : {title}")
            if title_b:
                print(f"   📝 Episode : {title_b}")

            # ── Resolve DB categories ──────────────────────────────────────
            if forced_db_cats is not None:
                db_cats = forced_db_cats
                print(f"   🏷  Category (forced): {', '.join(db_cats)}")
            else:
                db_cats = _infer_db_cats_from_scraped(parsed['categories'])
                print(f"   🏷  Category (inferred from '{', '.join(parsed['categories'])}'): {', '.join(db_cats)}")

            # ── DB upsert ──────────────────────────────────────────────────
            try:
                movie   = find_existing_movie(title)
                created = False
                updated = False

                # ── CREATE ─────────────────────────────────────────────────
                if not movie:
                    movie = Movie.objects.create(
                        title        = title[:200],
                        title_b      = (title_b or '')[:200] or None,
                        title_b_updated_at = timezone.now() if title_b else None,
                        description  = parsed['description'],
                        video_url    = parsed['video_url'][:500] if parsed['video_url'] else '',
                        download_url = parsed['download_links'][0]['url'][:500],
                        image_url    = parsed['image_url'][:500] if parsed['image_url'] else '',
                        completed    = parsed['is_complete'],
                        is_series    = parsed['is_series'],
                        scraped      = True,
                        vi_year      = parsed.get('vi_year', '')[:10],
                        vi_country   = parsed.get('vi_country', '')[:120],
                        vi_language  = parsed.get('vi_language', '')[:120],
                        vi_subtitle  = parsed.get('vi_subtitle', '')[:60],
                        vi_genre     = parsed.get('vi_genre', '')[:200],
                        vi_cast      = parsed.get('vi_cast', ''),
                        vi_episodes  = parsed.get('vi_episodes', '')[:20],
                        vi_status    = parsed.get('vi_status', '')[:60],
                        vi_runtime   = parsed.get('vi_runtime', '')[:30],
                        vi_filesize  = parsed.get('vi_filesize', '')[:30],
                    )
                    created = True
                    total_created += 1
                    print(f"   ✅ CREATED: {title}")

                    if not no_social:
                        _post_to_all_platforms(movie, is_new=True)

                # ── UPDATE ─────────────────────────────────────────────────
                else:
                    print(f"   🔍 EXISTS (pk={movie.pk}): checking for changes…")

                    # Title drift (unlikely but possible)
                    if movie.title != title:
                        print(f"      📝 Title changed: '{movie.title}' → '{title}'")
                        movie.title = title
                        updated = True

                    # ── Series: new episode check ──────────────────────────
                    if title_b and movie.title_b != title_b:
                        print(f"      🆕 Episode updated: '{movie.title_b}' → '{title_b}'")
                        movie.title_b            = title_b
                        movie.title_b_updated_at = timezone.now()
                        updated                  = True
                        if not no_social:
                            _post_to_all_platforms(movie, is_new=False)

                    # Back-fill empty fields
                    if not movie.video_url and parsed['video_url']:
                        movie.video_url = parsed['video_url']
                        updated = True

                    if not movie.image_url and parsed['image_url']:
                        movie.image_url = parsed['image_url']
                        updated = True

                    if parsed['download_links']:
                        new_dl_url = parsed['download_links'][0]['url']
                        if movie.download_url and normalize_url(movie.download_url) != normalize_url(new_dl_url):
                            movie.download_url = new_dl_url
                            updated = True

                    if movie.completed != parsed['is_complete']:
                        movie.completed = parsed['is_complete']
                        updated = True

                    if not getattr(movie, 'is_series', False) and parsed['is_series']:
                        movie.is_series = parsed['is_series']
                        updated = True

                    # Backfill vi_ fields only if they are currently empty
                    vi_map = {
                        'vi_year':     parsed.get('vi_year', '')[:10],
                        'vi_country':  parsed.get('vi_country', '')[:120],
                        'vi_language': parsed.get('vi_language', '')[:120],
                        'vi_subtitle': parsed.get('vi_subtitle', '')[:60],
                        'vi_genre':    parsed.get('vi_genre', '')[:200],
                        'vi_cast':     parsed.get('vi_cast', ''),
                        'vi_episodes': parsed.get('vi_episodes', '')[:20],
                        'vi_status':   parsed.get('vi_status', '')[:60],
                        'vi_runtime':  parsed.get('vi_runtime', '')[:30],
                        'vi_filesize': parsed.get('vi_filesize', '')[:30],
                    }
                    for field, value in vi_map.items():
                        if value and not getattr(movie, field, ''):
                            setattr(movie, field, value)
                            updated = True

                    if updated:
                        movie.save()
                        total_updated += 1
                    else:
                        total_unchanged += 1

                # ── Categories ─────────────────────────────────────────────
                assign_db_categories(
                    movie,
                    scraped_cats   = parsed['categories'],
                    forced_db_cats = db_cats,
                )

                # ── Download link sync ─────────────────────────────────────
                # • new links in scraped set  → add
                # • links in DB but not scraped → remove  (keeps DB clean)
                # • existing link with changed label → update label
                existing = {normalize_url(dl.url): dl for dl in movie.download_links.all()}
                current  = {normalize_url(dl['url']): dl for dl in parsed['download_links'] if is_valid_download_url(dl['url'])}
                added    = 0
                removed  = 0

                for norm, dl in current.items():
                    if norm not in existing:
                        DownloadLink.objects.create(
                            movie = movie,
                            label = dl['label'],
                            url   = dl['url'],
                        )
                        added += 1
                        print(f"      ➕ Link added : {dl['label']} → {dl['url'][:80]}")
                    else:
                        # Update label if it changed (e.g. episode numbering)
                        if existing[norm].label != dl['label']:
                            existing[norm].label = dl['label']
                            existing[norm].save()

                for norm, dl_obj in existing.items():
                    if norm not in current:
                        print(f"      ➖ Link removed: {dl_obj.label} → {dl_obj.url[:80]}")
                        dl_obj.delete()
                        removed += 1

                status = (
                    "CREATED"   if created   else
                    "UPDATED"   if updated   else
                    "UNCHANGED"
                )
                print(
                    f"   📋 {status} | "
                    f"total links: {len(parsed['download_links'])} | "
                    f"+{added} added, -{removed} removed"
                )

            except Exception as db_err:
                print(f"   💥 DB error: {db_err}")
                import traceback
                traceback.print_exc()
                connection.close()
                continue

        # ── Summary ────────────────────────────────────────────────────────
        print(f"\n\n{'=' * 60}")
        print(f"🎉  Done!")
        print(f"    URLs processed : {len(urls)}")
        print(f"    Created        : {total_created}")
        print(f"    Updated        : {total_updated}")
        print(f"    Unchanged      : {total_unchanged}")
        print(f"    Skipped/errors : {total_skipped}")
        print("=" * 60)

    # ── Helpers ────────────────────────────────────────────────────────────

    def _load_urls_from_file(self, filepath: str) -> list[str]:
        """
        Read URLs from a text file.  Each non-blank, non-comment line is a URL.
        Returns an empty list (with a warning) if the file doesn't exist.
        """
        if not os.path.isfile(filepath):
            # Only warn if the user explicitly passed a non-default path
            if filepath != 'links.txt':
                self.stderr.write(f"⚠️  File not found: {filepath}\n")
            return []

        urls = []
        with open(filepath, 'r', encoding='utf-8') as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith('#'):
                    continue
                urls.append(line)

        print(f"📄 Loaded {len(urls)} URL(s) from '{filepath}'")
        return urls

    def _resolve_forced_cats(self, cat_arg: str) -> list[str] | None:
        """
        Turn a --category value into a flat list of DB category name strings.

        Returns None if the alias / slug is unrecognised.
        """
        # 1. Friendly alias
        if cat_arg in CATEGORY_ALIASES:
            keys = CATEGORY_ALIASES[cat_arg]
            db_cats: list[str] = []
            for k in keys:
                if k in _KEY_TO_DEF:
                    for name in _KEY_TO_DEF[k]['db_cats']:
                        if name not in db_cats:
                            db_cats.append(name)
            return db_cats or None

        # 2. Full slug
        if cat_arg in _SLUG_TO_DEF:
            return list(_SLUG_TO_DEF[cat_arg]['db_cats'])

        # 3. Bare slug → try with prefix
        full_slug = f"videodownload/{cat_arg}"
        if full_slug in _SLUG_TO_DEF:
            return list(_SLUG_TO_DEF[full_slug]['db_cats'])

        # 4. Normalised key match
        normalized = cat_arg.replace('-', '_')
        if normalized in _KEY_TO_DEF:
            return list(_KEY_TO_DEF[normalized]['db_cats'])

        return None  # unrecognised
    












# # Single URL
# python manage.py scrape_9jarocks_links \
#   --url "https://9jarocks.net/videodownload/mrs-fazilet-and-her-daughters-season-1-2-complete-turkish-id394535.html"

# # From links.txt (default filename)
# python manage.py scrape_9jarocks_links

# # Custom file path
# python manage.py scrape_9jarocks_links --file /path/to/my_links.txt

# # Force category + skip socials
# python manage.py scrape_9jarocks_links --file links.txt --category kdrama --no-social