"""
Management command: scrape_naijaprey_links
==========================================
Scrape one or more specific naijaprey.tv post URLs — either passed directly on
the command line or read from a text file (one URL per line).

Unlike the main scraper that pages through the whole WordPress REST API, this
command goes straight to the individual posts you tell it about.  It reuses the
exact same parsers, DB logic, category detection and social-posting code as
scrape_naijaprey.

How it works
─────────────
naijaprey.tv exposes a WordPress REST API, so instead of fetching+parsing the
post's HTML page we resolve each URL to its slug and pull the post JSON via:
    /wp-json/wp/v2/posts?slug=<slug>
That single object is then fed through the same parse_post() used by the main
scraper, guaranteeing identical behaviour.

Behaviour
─────────
• Movie (not a series)
    – Already exists  → update any new fields, sync download links.
    – Does not exist  → create it, optionally post to socials.

• Series / ongoing show
    – Exists, same latest-episode marker  → sync download links only.
    – Exists, NEW episode marker          → update title_b, add new links,
      post to socials as "New Episode".
    – Does not exist                      → create it, post as "New".

Category assignment
────────────────────
DB categories are auto-detected from each post's Country / Language / Genre
(same as the main scraper).  Pass --category movies|series to force the
series flag, or --db-category "Korean drama" to hard-set the DB category.

Usage
─────
# Single URL on the CLI
python manage.py scrape_naijaprey_links \\
    --url "https://www.naijaprey.tv/a-castle-of-our-own-2026/"

# Multiple URLs on the CLI
python manage.py scrape_naijaprey_links \\
    --url "https://www.naijaprey.tv/glenrothan-2026/" \\
    --url "https://www.naijaprey.tv/one-piece-season-23/"

# From a file (default: links.txt in the current working directory)
python manage.py scrape_naijaprey_links --file links.txt

# Force the series flag / a DB category, skip social, control delay
python manage.py scrape_naijaprey_links --file links.txt --category series
python manage.py scrape_naijaprey_links --file links.txt --db-category "Anime"
python manage.py scrape_naijaprey_links --file links.txt --no-social
python manage.py scrape_naijaprey_links --file links.txt --delay 0.5

links.txt format
─────────────────
  # Lines starting with # are comments and are ignored.
  # Blank lines are also ignored.
  https://www.naijaprey.tv/a-castle-of-our-own-2026/
  https://www.naijaprey.tv/one-piece-season-23/
"""

import os
import time
from urllib.parse import urlparse

import urllib3
from django.core.management.base import BaseCommand
from django.utils import timezone

from movies.models import Movie, Category, DownloadLink
from movies.scraper_utils import is_valid_download_url

# Re-use everything from the sibling naijaprey scraper.
# Both files live in the same management/commands/ directory.
from .scrape_naijaprey import (
    # parsers / helpers
    _make_scraper,
    parse_post,
    clean_title_parts,
    find_existing_movie,
    normalize_url,
    _is_own_link,
    detect_db_categories,
    assign_db_categories,
    # social posters
    _post_to_all_platforms,
    # constants
    SITE_URL,
    API_POSTS,
    SOURCE_NAME,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# naijaprey content-category IDs (see scrape_naijaprey.CATEGORY_DEFINITIONS)
MOVIES_CAT_ID = 1228
SERIES_CAT_ID = 1518


def _slug_from_url(url: str) -> str:
    """
    Turn a naijaprey post URL into its WordPress slug.
      https://www.naijaprey.tv/a-castle-of-our-own-2026/  → a-castle-of-our-own-2026
    """
    path = urlparse(url).path.strip('/')
    if not path:
        return ''
    return path.split('/')[-1]


def _fetch_post_by_slug(scraper, slug: str) -> dict | None:
    """Fetch a single post object from the REST API by its slug."""
    params = {
        'slug':     slug,
        '_fields':  'id,link,title,content,excerpt,meta,categories,'
                    'jetpack_featured_media_url',
    }
    resp = scraper.get(API_POSTS, params=params, timeout=25)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list) and data:
        return data[0]
    return None


class Command(BaseCommand):
    help = (
        'Scrape specific naijaprey.tv post URLs (from --url args or --file) '
        'and upsert them into the DB.  Handles both movies and series episode updates.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--url', action='append', dest='urls', default=[],
            metavar='URL',
            help=(
                'A naijaprey.tv post URL to scrape.  Can be repeated:\n'
                '  --url "https://www.naijaprey.tv/glenrothan-2026/" \\\n'
                '  --url "https://www.naijaprey.tv/one-piece-season-23/"'
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
                'Force the series flag for all URLs: "movies" or "series".\n'
                'If omitted, series/movie is inferred from each post.'
            ),
        )
        parser.add_argument(
            '--db-category', type=str, default=None,
            help=(
                'Hard-set the DB category for all URLs (e.g. "Korean drama", '
                '"Anime", "Hollywood movies").  Comma-separate for multiple.\n'
                'If omitted, the DB category is auto-detected from each post '
                'Country / Genre.'
            ),
        )
        parser.add_argument(
            '--no-social', action='store_true', default=False,
            help='Save to DB only — skip all social posts (Telegram, Facebook, Twitter).',
        )
        parser.add_argument(
            '--delay', type=float, default=0.5,
            help='Seconds to wait between API requests (default: 0.5).',
        )

    def handle(self, *args, **options):
        from django.db import connection

        no_social = options['no_social']
        delay     = options['delay']

        # ── Optional forced series flag ────────────────────────────────────
        cat_arg = (options.get('category') or '').strip().lower()
        forced_series: bool | None = None
        if cat_arg in ('movie', 'movies'):
            forced_series = False
        elif cat_arg in ('series', 'tv'):
            forced_series = True
        elif cat_arg:
            self.stderr.write(
                f"❌  Unknown --category value '{cat_arg}'. Use 'movies' or 'series'.\n"
            )
            return

        # ── Optional forced DB category list ───────────────────────────────
        forced_db_cats: list[str] | None = None
        if options.get('db_category'):
            forced_db_cats = [
                c.strip() for c in options['db_category'].split(',') if c.strip()
            ] or None

        # ── Collect URLs ───────────────────────────────────────────────────
        urls = list(options['urls'])
        urls += self._load_urls_from_file(options['file'])
        urls = list(dict.fromkeys(u.strip() for u in urls if u.strip()))

        if not urls:
            self.stderr.write(
                "❌  No URLs provided.  Pass --url or --file with at least one URL.\n"
            )
            return

        print("=" * 60)
        print("🚀  naijaprey link scraper starting")
        print(f"    URLs      : {len(urls)}")
        print(f"    Series    : {cat_arg or '(auto-detect from post)'}")
        print(f"    DB cat    : {', '.join(forced_db_cats) if forced_db_cats else '(auto-detect)'}")
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

            slug = _slug_from_url(url)
            if not slug:
                print("   ⚠️  Could not extract slug from URL — skipping.")
                total_skipped += 1
                continue

            # ── Fetch the post JSON via REST API ───────────────────────────
            try:
                post = _fetch_post_by_slug(scraper, slug)
            except Exception as e:
                print(f"   ❌  Fetch error: {e}")
                total_skipped += 1
                continue

            if not post:
                print(f"   ⚠️  No post found for slug '{slug}' — skipping.")
                total_skipped += 1
                continue

            # ── Determine series flag ──────────────────────────────────────
            if forced_series is not None:
                default_series = forced_series
            else:
                post_cats = post.get('categories') or []
                default_series = SERIES_CAT_ID in post_cats

            # ── Parse ──────────────────────────────────────────────────────
            parsed = parse_post(post, default_is_series=default_series)
            if not parsed:
                print("   ⚠️  Could not parse post — skipping.")
                total_skipped += 1
                continue

            if not parsed['download_links']:
                print(f"   ⛔  No download links found — skipping '{parsed['title_raw']}'")
                total_skipped += 1
                continue

            title, title_b = clean_title_parts(parsed['title_raw'])
            if parsed['is_series'] and parsed['episode_meta']:
                title_b = parsed['episode_meta']
            print(f"   📝 Title   : {title}")
            if title_b:
                print(f"   📝 Episode : {title_b}")

            # ── Resolve DB categories ──────────────────────────────────────
            if forced_db_cats is not None:
                db_cats = list(forced_db_cats)
                if parsed['is_series'] and 'Series' not in db_cats:
                    db_cats.append('Series')
                print(f"   🏷  Category (forced): {', '.join(db_cats)}")
            else:
                db_cats = detect_db_categories(parsed, parsed['is_series'])
                print(f"   🏷  Category (detected): {', '.join(db_cats)}")

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

                    if movie.title != title:
                        print(f"      📝 Title changed: '{movie.title}' → '{title}'")
                        movie.title = title
                        updated = True

                    if title_b and movie.title_b != title_b:
                        print(f"      🆕 Episode updated: '{movie.title_b}' → '{title_b}'")
                        movie.title_b            = title_b
                        movie.title_b_updated_at = timezone.now()
                        updated                  = True
                        if not no_social:
                            _post_to_all_platforms(movie, is_new=False)

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
                assign_db_categories(movie, db_cats)

                # ── Download link sync ─────────────────────────────────────
                existing = {normalize_url(dl.url): dl for dl in movie.download_links.all()}
                current  = {normalize_url(dl['url']): dl for dl in parsed['download_links'] if is_valid_download_url(dl['url'])}
                added    = 0
                removed  = 0

                for norm, dl in current.items():
                    if norm not in existing:
                        DownloadLink.objects.create(
                            movie  = movie,
                            label  = dl['label'],
                            url    = dl['url'],
                            source = SOURCE_NAME,
                        )
                        added += 1
                        print(f"      ➕ Link added : {dl['label']} → {dl['url'][:80]}")
                    else:
                        if existing[norm].label != dl['label']:
                            existing[norm].label = dl['label']
                            existing[norm].save()

                # Only prune OUR OWN stale links — never delete links a different
                # source added (keeps cross-source fallbacks intact).
                for norm, dl_obj in existing.items():
                    if norm not in current and _is_own_link(dl_obj.url):
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
