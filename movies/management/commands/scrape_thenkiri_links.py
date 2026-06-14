"""
Management command: scrape_thenkiri_links
==========================================
Scrape one or more specific thenkiri.com post URLs — either passed directly on
the command line or read from a text file (one URL per line).

Unlike the main scraper that crawls category listing pages, this command goes
straight to the post pages you tell it about.  It uses the exact same parsers,
DB logic, and social-posting code as scrape_thenkiri.

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
category.  Instead the scraper reads the post's own breadcrumb / category tags
and maps them to your DB categories using the same mapping table used by the
main scraper.  You can also pass --category on the CLI to force an assignment
(same aliases as the main command).

thenkiri category inference
────────────────────────────
thenkiri uses OceanWP theme.  The post's category is embedded in the <body>
class as  post-in-category-<slug>  (e.g. post-in-category-download-k-drama).
We also check breadcrumbs and <a rel="category tag"> anchors.

Usage
─────
# Single URL on the CLI
python manage.py scrape_thenkiri_links \\
    --url "https://thenkiri.com/recipe-for-love-s01-complete-korean-drama/"

# Multiple URLs on the CLI
python manage.py scrape_thenkiri_links \\
    --url "https://thenkiri.com/mortal-kombat-ii-2026-download-hollywood-movie/" \\
    --url "https://thenkiri.com/recipe-for-love-s01-complete-korean-drama/"

# From a file (default: links.txt in the current working directory)
python manage.py scrape_thenkiri_links --file links.txt

# Force a DB category regardless of what the post page says
python manage.py scrape_thenkiri_links --file links.txt --category kdrama

# Skip social posting
python manage.py scrape_thenkiri_links --file links.txt --no-social

# Control request delay (seconds between fetches)
python manage.py scrape_thenkiri_links --file links.txt --delay 0.5

links.txt format
─────────────────
  # Lines starting with # are comments and are ignored.
  # Blank lines are also ignored.
  https://thenkiri.com/some-movie-download-hollywood-movie/
  https://thenkiri.com/some-drama-s01-complete-korean-drama/
"""

import os
import re
import time

import urllib3
from django.core.management.base import BaseCommand
from django.utils import timezone

from movies.models import Movie, Category, DownloadLink
from movies.scraper_utils import is_valid_download_url

# Re-use everything from the sibling thenkiri scraper.
# Both files live in the same management/commands/ directory.
from .scrape_thenkiri import (
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
# Map thenkiri body-class slugs / scraped tag names → DB category names.
# Used when --category is NOT passed on the CLI.
# ══════════════════════════════════════════════════════════════════════════════

# Body-class slug  →  list of DB category names
# Slugs come from  post-in-category-<slug>  in the <body> class attribute.
_SLUG_TO_DB: dict[str, list[str]] = {
    # thenkiri WP category slugs
    'international':                    ['Hollywood movies'],
    'download-k-drama':                 ['Korean drama'],
    'k-variety':                        ['Korean drama', 'Series'],
    'asian-movies':                     ['Hollywood movies'],
    'download-korean-movies':           ['Korean drama'],
    'download-bollywood-movies':        ['Bollywood movies'],
    'download-philippine-movies':       ['Filipino drama', 'Series'],
    'chinese-movie':                    ['Chinese drama'],
    'chinese-dramas':                   ['Chinese drama'],
    'tv-series':                        ['Hollywood movies', 'Series'],
}

# Scraped display name / tag  →  list of DB category names
# (lower-cased for matching)
_SCRAPED_TO_DB: dict[str, list[str]] = {
    # thenkiri post tags and breadcrumb names
    'international':                    ['Hollywood movies'],
    'k-drama':                          ['Korean drama'],
    'k drama':                          ['Korean drama'],
    'korean drama':                     ['Korean drama'],
    'korean movies':                    ['Korean drama'],
    'k-variety':                        ['Korean drama', 'Series'],
    'bollywood':                        ['Bollywood movies'],
    'bollywood movies':                 ['Bollywood movies'],
    'chinese movie':                    ['Chinese drama'],
    'chinese movies':                   ['Chinese drama'],
    'chinese drama':                    ['Chinese drama'],
    'chinese dramas':                   ['Chinese drama'],
    'philippine movies':                ['Filipino drama', 'Series'],
    'asian movies':                     ['Hollywood movies'],
    'tv series':                        ['Hollywood movies', 'Series'],
    'series':                           ['Series'],
    # Genre tags used on thenkiri posts — these alone don't tell us the
    # main category so we skip them here (they don't map to a useful DB cat).
    # e.g. 'action', 'drama', 'comedy', 'fantasy' → not enough info
}


def _infer_db_cats_from_page(html: str, scraped_cats: list[str]) -> list[str]:
    """
    Determine the DB category list from the page HTML + scraped tag names.

    Strategy (in priority order):
    1. Extract body class  post-in-category-<slug>  and map using _SLUG_TO_DB.
    2. Fall back to scraped_cats mapped through _SCRAPED_TO_DB.
    3. Final fallback: ['Hollywood movies']
    """
    result: list[str] = []

    # ── Strategy 1: <body class="... post-in-category-<slug> ..."> ──────────
    body_match = re.search(
        r'<body[^>]+class=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    if body_match:
        body_classes = body_match.group(1).split()
        for cls in body_classes:
            # body class format: post-in-category-download-k-drama
            m = re.match(r'^post-in-category-(.+)$', cls)
            if m:
                slug = m.group(1)
                mapped = _SLUG_TO_DB.get(slug)
                if mapped:
                    for name in mapped:
                        if name not in result:
                            result.append(name)

    # ── Strategy 2: scraped breadcrumb / tag names ──────────────────────────
    if not result:
        for raw in scraped_cats:
            key = raw.strip().lower()
            mapped = _SCRAPED_TO_DB.get(key)
            if mapped:
                for name in mapped:
                    if name not in result:
                        result.append(name)

    # ── Strategy 3: fallback ─────────────────────────────────────────────────
    if not result:
        result = ['Hollywood movies']

    return result


# ══════════════════════════════════════════════════════════════════════════════
# MANAGEMENT COMMAND
# ══════════════════════════════════════════════════════════════════════════════

class Command(BaseCommand):
    help = (
        'Scrape specific thenkiri.com post URLs (from --url args or --file) '
        'and upsert them into the DB.  Handles both movies and series episode updates.'
    )

    # ── Argument definitions ───────────────────────────────────────────────

    def add_arguments(self, parser):
        parser.add_argument(
            '--url', action='append', dest='urls', default=[],
            metavar='URL',
            help=(
                'A thenkiri.com post URL to scrape.  Can be repeated:\n'
                '  --url "https://thenkiri.com/mortal-kombat-ii-2026-download-hollywood-movie/" \\\n'
                '  --url "https://thenkiri.com/recipe-for-love-s01-complete-korean-drama/"'
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
                '  hollywood, kdrama, korean_movie, bollywood, chinese,\n'
                '  chinese_drama, philippine, k_variety, series\n'
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

        no_social           = options['no_social']
        delay               = options['delay']
        forced_db_cats: list[str] | None = None

        # ── Resolve --category → forced DB cat list ────────────────────────
        cat_arg = (options.get('category') or '').strip().lower()
        if cat_arg:
            forced_db_cats = self._resolve_forced_cats(cat_arg)
            if forced_db_cats is None:
                self.stderr.write(
                    f"❌  Unknown --category value '{cat_arg}'.\n"
                    f"    Valid aliases: hollywood, kdrama, korean_movie, bollywood,\n"
                    f"    chinese, chinese_drama, philippine, k_variety, series\n"
                    f"    Or a full slug like 'download-k-drama'.\n"
                )
                return

        # ── Collect URLs ───────────────────────────────────────────────────
        urls = list(options['urls'])
        urls += self._load_urls_from_file(options['file'])
        urls = list(dict.fromkeys(u.strip() for u in urls if u.strip()))

        if not urls:
            self.stderr.write(
                "❌  No URLs provided.  Pass --url or --file with at least one URL.\n"
            )
            return

        # ── Banner ─────────────────────────────────────────────────────────
        print("=" * 60)
        print("🚀  thenkiri link scraper starting")
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
            print(f"\n{'─' * 60}")
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
                # Use body-class slug inference (primary) + scraped tags (fallback)
                db_cats = _infer_db_cats_from_page(html, parsed['categories'])
                print(
                    f"   🏷  Category (inferred): {', '.join(db_cats)}"
                    f"  ← scraped tags: {', '.join(parsed['categories']) or 'none'}"
                )

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

                    # Title drift
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

        Accepts:
          • A friendly alias key (e.g. 'kdrama', 'hollywood')
          • A full thenkiri category slug (e.g. 'download-k-drama')
          • A bare slug without prefix (e.g. 'k-drama')

        Returns None if the alias / slug is unrecognised.
        """
        # 1. Friendly alias  (e.g. 'kdrama', 'all')
        if cat_arg in CATEGORY_ALIASES:
            keys = CATEGORY_ALIASES[cat_arg]
            db_cats: list[str] = []
            for k in keys:
                if k in _KEY_TO_DEF:
                    for name in _KEY_TO_DEF[k]['db_cats']:
                        if name not in db_cats:
                            db_cats.append(name)
            return db_cats or None

        # 2. Full slug  (e.g. 'download-k-drama')
        if cat_arg in _SLUG_TO_DEF:
            return list(_SLUG_TO_DEF[cat_arg]['db_cats'])

        # 3. Normalised key match  (e.g. 'k_variety')
        normalized = cat_arg.replace('-', '_')
        if normalized in _KEY_TO_DEF:
            return list(_KEY_TO_DEF[normalized]['db_cats'])

        # 4. Partial / lowercase slug match
        for slug, defn in _SLUG_TO_DEF.items():
            if cat_arg in slug or slug.endswith(cat_arg):
                return list(defn['db_cats'])

        return None  # unrecognised