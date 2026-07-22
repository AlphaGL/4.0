"""
Repoint every stored download link from the dead `loadedfiles.org` host to the
new `loadedfiles.net`.

9jarocks' file host was renamed in 2026-07. `loadedfiles.org` no longer resolves
in DNS at all, so every link still pointing at it is dead. The file IDs and paths
are IDENTICAL on the new host — only the domain changed — so a straight substring
swap revives the link (verified: the swapped URL resolves through the token chain
to a real CDN file).

Runs as ONE bulk SQL UPDATE, so it's fast even on 100k+ rows, and is safe to
re-run (rows already on .net simply don't match the filter).

    python manage.py fix_loadedfiles_domain            # apply
    python manage.py fix_loadedfiles_domain --dry-run  # count only
"""
from django.core.management.base import BaseCommand
from django.db.models import Value
from django.db.models.functions import Replace

from movies.models import DownloadLink

OLD = 'loadedfiles.org'
NEW = 'loadedfiles.net'


class Command(BaseCommand):
    help = "Repoint dead loadedfiles.org download links to loadedfiles.net."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help="Report how many links would change, without writing.",
        )

    def handle(self, *args, **opts):
        qs = DownloadLink.objects.filter(url__contains=OLD)
        total = qs.count()
        already = DownloadLink.objects.filter(url__contains=NEW).count()
        self.stdout.write(f"Links on {OLD} (dead): {total}")
        self.stdout.write(f"Links already on {NEW}: {already}")

        if not total:
            self.stdout.write(self.style.SUCCESS("Nothing to do."))
            return

        for d in qs[:3]:
            self.stdout.write(f"  e.g. {d.url}")
            self.stdout.write(f"    -> {d.url.replace(OLD, NEW)}")

        if opts['dry_run']:
            self.stdout.write(self.style.WARNING(
                f"DRY RUN — would repoint {total} link(s)."
            ))
            return

        updated = qs.update(url=Replace('url', Value(OLD), Value(NEW)))
        self.stdout.write(self.style.SUCCESS(
            f"Repointed {updated} link(s) from {OLD} to {NEW}."
        ))
