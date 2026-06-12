"""
Management command: scrape_streamimdb_links
============================================
Scrape one or more specific streamimdb.ru title URLs — passed directly on the
command line or read from a text file (one URL per line).

Unlike scrape_streamimdb (which crawls listing pages), this goes straight to the
title pages you give it. It reuses the exact same parsers, stream resolver, DB
upsert, and social-posting code as scrape_streamimdb.

Accepts both movie and tv URLs:
  https://streamimdb.ru/movie/84s7q-scary-movie
  https://streamimdb.ru/tv/19cem-jungle-cubs

What gets stored
────────────────
The durable embed URL (https://streamimdb.ru/embed/<movie|tv>/<tmdb>) in
Movie.video_url. The real stream is resolved only as a liveness check — see the
scrape_streamimdb docstring for the full rationale.

Usage
─────
# Single URL
python manage.py scrape_streamimdb_links --url "https://streamimdb.ru/movie/84s7q-scary-movie"

# Multiple URLs
python manage.py scrape_streamimdb_links \\
    --url "https://streamimdb.ru/movie/84s7q-scary-movie" \\
    --url "https://streamimdb.ru/tv/19cem-jungle-cubs"

# From a file (default: links.txt in the current working directory)
python manage.py scrape_streamimdb_links --file links.txt

# Force a DB category, skip socials, keep titles with no live stream
python manage.py scrape_streamimdb_links --file links.txt --category hollywood --no-social --allow-unverified

links.txt format
────────────────
  # Lines starting with # are comments and are ignored. Blank lines too.
  https://streamimdb.ru/movie/84s7q-scary-movie
  https://streamimdb.ru/tv/19cem-jungle-cubs
"""

import os
import time

import urllib3
from django.core.management.base import BaseCommand

# Re-use everything from the sibling crawler — same management/commands/ dir.
from .scrape_streamimdb import (
    _make_scraper,
    parse_page,
    resolve_stream,
    build_embed_url,
    infer_db_cats,
    save_item,
    resolve_category_arg,
    SITE_URL,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class Command(BaseCommand):
    help = (
        'Scrape specific streamimdb.ru title URLs (from --url args or --file) and '
        'upsert them into the DB as streaming entries.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--url', action='append', dest='urls', default=[], metavar='URL',
                            help='A streamimdb.ru /movie/ or /tv/ URL. Can be repeated.')
        parser.add_argument('--file', type=str, default='links.txt', metavar='PATH',
                            help='Text file with one URL per line (default: links.txt). '
                                 'Lines starting with # and blank lines are skipped.')
        parser.add_argument('--category', type=str, default=None,
                            help='Force a DB category for all URLs (alias or raw name). '
                                 'Aliases: hollywood, kdrama, chinese, thai, bollywood, '
                                 'nollywood, anime, animation, series.')
        parser.add_argument('--no-social', action='store_true', default=False,
                            help='Save to DB only — skip all social posts.')
        parser.add_argument('--update-only', action='store_true', default=False,
                            help='Only ADD streaming to movies that already exist; skip titles '
                                 'with no match instead of creating stream-only entries.')
        parser.add_argument('--allow-unverified', action='store_true', default=False,
                            help='Store titles even when the stream liveness check fails.')
        parser.add_argument('--delay', type=float, default=0.5,
                            help='Seconds between HTTP requests (default: 0.5).')

    def handle(self, *args, **options):
        from django.db import connection

        no_social   = options['no_social']
        update_only = options['update_only']
        allow_unver = options['allow_unverified']
        delay       = options['delay']

        forced_cats = None
        if options['category']:
            forced_cats = resolve_category_arg(options['category'])

        urls = list(options['urls'])
        urls += self._load_urls_from_file(options['file'])
        urls = [u.strip() for u in urls if u.strip()]
        # Keep only this site's title URLs, de-duplicated, order preserved.
        urls = list(dict.fromkeys(
            u for u in urls
            if u.startswith(SITE_URL) and ('/movie/' in u or '/tv/' in u)
        ))

        if not urls:
            self.stderr.write('❌  No valid streamimdb.ru title URLs provided.\n')
            return

        print('=' * 60)
        print('🚀  streamimdb.ru link scraper starting')
        print(f'    URLs     : {len(urls)}')
        print(f'    Category : {", ".join(forced_cats) if forced_cats else "(auto-infer)"}')
        print(f'    Social   : {"DISABLED" if no_social else "ON (Telegram + Facebook)"}')
        print(f'    Verify   : {"liveness optional" if allow_unver else "skip dead streams"}')
        print('=' * 60)

        scraper = _make_scraper()
        total_created = total_enriched = total_unchanged = total_skipped = 0

        for idx, url in enumerate(urls, 1):
            print(f'\n{"─" * 60}')
            print(f'[{idx}/{len(urls)}] 🌐 {url}')

            if delay > 0 and idx > 1:
                time.sleep(delay)

            try:
                resp = scraper.get(url, timeout=25)
                if resp.status_code != 200:
                    print(f'   ⚠️  HTTP {resp.status_code} — skipping.')
                    total_skipped += 1
                    continue
            except Exception as e:
                print(f'   ❌  Fetch error: {e}')
                total_skipped += 1
                continue

            parsed = parse_page(resp.text, url)
            if not parsed:
                print('   ⚠️  Could not parse page — skipping.')
                total_skipped += 1
                continue

            print(f'   📝 {parsed["title_raw"]}  '
                  f'({parsed.get("vi_year") or "?"}, {parsed["media_type"]})')

            info = resolve_stream(scraper, parsed['tmdb_id'], parsed['media_type'])
            if info:
                print(f'   🎞  Stream OK — {info["stream_count"]} source(s) | {info["file_name"][:60]}')
            elif not allow_unver:
                print('   ⛔  No live stream — skipping (use --allow-unverified to keep).')
                total_skipped += 1
                continue
            else:
                print('   ⚠️  No live stream — storing anyway (--allow-unverified).')

            embed_url = build_embed_url(parsed['media_type'], parsed['tmdb_id'])
            db_cats   = forced_cats if forced_cats is not None else infer_db_cats(parsed)

            try:
                _movie, status = save_item(parsed, embed_url, db_cats,
                                           no_social=no_social, update_only=update_only)
            except Exception as db_err:
                print(f'   💥 DB error: {db_err}')
                import traceback; traceback.print_exc()
                connection.close()
                total_skipped += 1
                continue

            print(f'   📋 {status.upper()} | embed: {embed_url}')
            if status == 'created':
                total_created += 1
            elif status == 'enriched':
                total_enriched += 1
            elif status == 'unchanged':
                total_unchanged += 1
            else:  # skipped-no-match
                total_skipped += 1

        print(f'\n\n{"=" * 60}')
        print('🎉  Done!')
        print(f'    URLs processed : {len(urls)}')
        print(f'    Enriched       : {total_enriched}')
        print(f'    Created        : {total_created}')
        print(f'    Unchanged      : {total_unchanged}')
        print(f'    Skipped/errors : {total_skipped}')
        print('=' * 60)

    # ── Helpers ────────────────────────────────────────────────

    def _load_urls_from_file(self, filepath: str) -> list[str]:
        if not os.path.isfile(filepath):
            if filepath != 'links.txt':
                self.stderr.write(f'⚠️  File not found: {filepath}\n')
            return []
        urls = []
        with open(filepath, 'r', encoding='utf-8') as fh:
            for raw in fh:
                line = raw.strip()
                if line and not line.startswith('#'):
                    urls.append(line)
        print(f"📄 Loaded {len(urls)} URL(s) from '{filepath}'")
        return urls
