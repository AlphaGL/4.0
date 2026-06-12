"""
Management command: scrape_moviebox_links
==========================================
Scrape one or more specific moviebox.ph titles — passed as detail URLs, bare
slugs, or raw numeric subjectIds — on the command line or from a text file.

Reuses the signed API client, slug decoder, parser, and DB upsert from
scrape_moviebox. See that file's docstring for the full reverse-engineered
signing/id scheme and why video_url stores the embeddable netfilm.world player
URL (streams are not extractable server-side; the player plays them in-browser).

Accepted inputs (any mix):
  https://moviebox.ph/moviedetail/mortal-kombat-0ISdrp8hJl3
  mortal-kombat-0ISdrp8hJl3
  0ISdrp8hJl3
  2812062564983656232

Usage
─────
python manage.py scrape_moviebox_links --url "https://moviebox.ph/moviedetail/mortal-kombat-0ISdrp8hJl3"
python manage.py scrape_moviebox_links --url 0ISdrp8hJl3 --url 2812062564983656232
python manage.py scrape_moviebox_links --file moviebox_links.txt
python manage.py scrape_moviebox_links --file links.txt --category hollywood --no-social
"""

import os
import time

import urllib3
from django.core.management.base import BaseCommand

from .scrape_moviebox import (
    MovieboxClient,
    subject_id_from,
    parse_detail,
    infer_db_cats,
    is_foreign_variant,
    save_item,
    resolve_category_arg,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class Command(BaseCommand):
    help = (
        'Scrape specific moviebox.ph titles (URLs / slugs / subjectIds from --url '
        'or --file) via the signed wefeed API and upsert them into the DB.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--url', action='append', dest='urls', default=[], metavar='INPUT',
                            help='A moviebox detail URL, slug, or numeric subjectId. Repeatable.')
        parser.add_argument('--file', type=str, default='moviebox_links.txt', metavar='PATH',
                            help='Text file with one URL/slug/subjectId per line '
                                 '(default: moviebox_links.txt). # comments and blanks skipped.')
        parser.add_argument('--category', type=str, default=None,
                            help='Force a DB category (alias or raw name).')
        parser.add_argument('--no-social', action='store_true', default=False,
                            help='Save to DB only — skip all social posts.')
        parser.add_argument('--update-only', action='store_true', default=False,
                            help='Only ADD streaming to movies that already exist; skip titles '
                                 'with no match instead of creating stream-only entries.')
        parser.add_argument('--include-dubs', action='store_true', default=False,
                            help='Include non-English dub/version titles. Default: skip them.')
        parser.add_argument('--delay', type=float, default=0.4,
                            help='Seconds between detail requests (default: 0.4).')

    def handle(self, *args, **options):
        from django.db import connection

        no_social   = options['no_social']
        delay       = options['delay']
        update_only = options['update_only']
        include_dubs = options['include_dubs']
        forced_cats = resolve_category_arg(options['category']) if options['category'] else None

        raw_inputs = list(options['urls']) + self._load_from_file(options['file'])
        # Resolve each input to a numeric subjectId, de-duplicated, order preserved.
        subject_ids: list[int] = []
        seen: set[int] = set()
        for item in raw_inputs:
            sid = subject_id_from(item)
            if sid and sid not in seen:
                seen.add(sid)
                subject_ids.append(sid)
            elif not sid:
                self.stderr.write(f'⚠️  Could not resolve subjectId from: {item}\n')

        if not subject_ids:
            self.stderr.write('❌  No valid moviebox inputs provided.\n')
            return

        print('=' * 60)
        print('🚀  moviebox.ph link scraper starting')
        print(f'    Titles   : {len(subject_ids)}')
        print(f'    Category : {", ".join(forced_cats) if forced_cats else "(auto-infer)"}')
        print(f'    Social   : {"DISABLED" if no_social else "ON (Telegram + Facebook)"}')
        print('=' * 60)

        client = MovieboxClient()
        print(f'    Player   : {client.player_base}/movies/<id>')

        total_created = total_enriched = total_unchanged = total_skipped = 0

        for idx, sid in enumerate(subject_ids, 1):
            print(f'\n{"─" * 60}')
            print(f'[{idx}/{len(subject_ids)}] 🎬 subjectId={sid}')
            if delay > 0 and idx > 1:
                time.sleep(delay)

            try:
                data = client.detail(sid)
                if not data:
                    print('   ⚠️  No detail returned — skipping.')
                    total_skipped += 1
                    continue
                parsed = parse_detail(data)
                if not parsed:
                    print('   ⚠️  Unparseable detail — skipping.')
                    total_skipped += 1
                    continue

                if not include_dubs and is_foreign_variant(parsed['title_raw']):
                    print(f"   ⏭  {parsed['title_raw'][:45]} — foreign-language dub, skipping.")
                    total_skipped += 1
                    continue

                kind = 'TV' if parsed['is_series'] else 'MOVIE'
                print(f"   📝 [{kind}] {parsed['title_raw']}  "
                      f"({parsed.get('vi_year') or '?'}, {parsed.get('vi_country') or '?'})")

                stream_url = client.player_url(parsed['detail_path'], sid, parsed['is_series'])
                db_cats    = forced_cats if forced_cats is not None else infer_db_cats(parsed)
                _movie, status = save_item(parsed, stream_url, db_cats,
                                           no_social=no_social, update_only=update_only)
                print(f'   📋 {status.upper()} | {stream_url}')

                if status == 'created':
                    total_created += 1
                elif status == 'enriched':
                    total_enriched += 1
                elif status == 'unchanged':
                    total_unchanged += 1
                else:  # skipped-no-match
                    total_skipped += 1
            except Exception as e:
                print(f'   💥 DB/API error: {e}')
                import traceback; traceback.print_exc()
                connection.close()
                total_skipped += 1

        print(f'\n\n{"=" * 60}')
        print('🎉  Done!')
        print(f'    Titles processed  : {len(subject_ids)}')
        print(f'    Enriched existing : {total_enriched}')
        print(f'    Created stream-only: {total_created}')
        print(f'    Unchanged         : {total_unchanged}')
        print(f'    Skipped/no-match  : {total_skipped}')
        print('=' * 60)

    def _load_from_file(self, filepath: str) -> list[str]:
        if not os.path.isfile(filepath):
            if filepath != 'moviebox_links.txt':
                self.stderr.write(f'⚠️  File not found: {filepath}\n')
            return []
        out = []
        with open(filepath, 'r', encoding='utf-8') as fh:
            for raw in fh:
                line = raw.strip()
                if line and not line.startswith('#'):
                    out.append(line)
        print(f"📄 Loaded {len(out)} input(s) from '{filepath}'")
        return out
