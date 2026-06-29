# movies/models.py
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from django.urls import reverse
from django.utils.text import slugify


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=120, unique=True, blank=True,
                            help_text="Auto-generated from name. Used in SEO URLs.")

    def _generate_unique_slug(self):
        base = slugify(self.name)
        slug = base
        n = 1
        qs = Category.objects.exclude(pk=self.pk)
        while qs.filter(slug=slug).exists():
            n += 1
            slug = f"{base}-{n}"
        return slug

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._generate_unique_slug()
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse('movies:category_movies', args=[self.pk, self.slug])

    def __str__(self):
        return self.name


class Movie(models.Model):
    title = models.CharField(max_length=200, unique=True)
    slug  = models.SlugField(max_length=250, unique=True, blank=True,
                             help_text="Auto-generated from title. Used in SEO URLs.")
    title_b = models.CharField(max_length=200, blank=True, null=True,
                               help_text="Stores new episode info")
    title_b_updated_at = models.DateTimeField(null=True, blank=True, db_index=True)
    is_series  = models.BooleanField(default=False)
    completed  = models.BooleanField(default=False, help_text="Mark if series is complete")
    # ── Show grouping: every season of a show shares one show_key ──────
    show_key = models.CharField(
        max_length=250, blank=True, default='', db_index=True,
        help_text="Normalized, season-stripped key grouping all seasons of a show "
                  "(e.g. 'from'). Auto-derived from the title on save."
    )
    season_number = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text="Season number parsed from the title, if any."
    )
    description = models.TextField(blank=True)
    video_url   = models.URLField("Video/Embed URL", max_length=500)
    download_url = models.URLField("Download URL", blank=True, null=True, max_length=500)
    stream_url   = models.URLField(
        "Stream/Embed URL", blank=True, null=True, max_length=600,
        help_text="Embeddable streaming player URL (e.g. moviebox / streamimdb). "
                  "Separate from download — powers the stream gate. A movie can "
                  "have downloads AND streaming at the same time."
    )
    image_url    = models.URLField("Cover Image URL", blank=True, null=True, max_length=500)
    # ── TMDB enrichment (rating / trailer / matched id) ───────────────
    tmdb_id      = models.IntegerField(null=True, blank=True, db_index=True,
                                       help_text="Matched TheMovieDB id, if any.")
    tmdb_synced  = models.BooleanField(default=False,
                                       help_text="TMDB enrichment has been attempted.")
    tmdb_seasons = models.TextField(
        blank=True, default='',
        help_text='JSON map of season_number → episode_count for series, from '
                  'TMDB (e.g. {"1": 10, "2": 8}). Powers the episode selector.')
    rating       = models.FloatField(null=True, blank=True,
                                     help_text="TMDB rating (0–10).")
    trailer_url  = models.URLField("Trailer URL", blank=True, null=True, max_length=500,
                                   help_text="Official YouTube trailer (from TMDB).")
    # ── Streaming availability (TMDB watch/providers → JustWatch data) ─
    on_netflix   = models.BooleanField(default=False, db_index=True,
                                       help_text="Also streaming on Netflix (region NG).")
    on_prime     = models.BooleanField(default=False, db_index=True,
                                       help_text="Also streaming on Amazon Prime Video (region NG).")
    providers_checked_at = models.DateTimeField(null=True, blank=True,
                                       help_text="When watch-provider availability was last refreshed.")
    categories   = models.ManyToManyField(Category, blank=True, related_name='movies')
    added_by     = models.ForeignKey(
        User, null=True, blank=True,
        on_delete=models.SET_NULL,
        help_text="If user-submitted, the submitting user"
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    scraped    = models.BooleanField(default=False,
                                     help_text="True if movie was scraped from external API")

    # Social relations
    liked_by       = models.ManyToManyField(User, related_name='liked_movies', blank=True)
    is_blockbuster = models.BooleanField(
        default=False,
        help_text="Legacy flag — blockbusters are now auto-computed by views (>=1000)"
    )
    watchlisted_by = models.ManyToManyField(User, related_name='watchlist_movies', blank=True)
    views = models.PositiveIntegerField(default=0, db_index=True)

    # ── Video info (scraped from nkiri / 9jarocks metadata) ──────────
    vi_country  = models.CharField(max_length=120, blank=True, default='', help_text="e.g. South Korea")
    vi_language = models.CharField(max_length=120, blank=True, default='', help_text="e.g. Korean")
    vi_cast     = models.TextField(blank=True, default='',     help_text="Comma-separated cast names")
    vi_genre    = models.CharField(max_length=200, blank=True, default='', help_text="e.g. Drama, Romance")
    vi_year     = models.CharField(max_length=10,  blank=True, default='', help_text="e.g. 2026")
    vi_episodes = models.CharField(max_length=20,  blank=True, default='', help_text="e.g. 12 or Ongoing")
    vi_status   = models.CharField(max_length=60,  blank=True, default='', help_text="e.g. Completed / On Going")
    vi_runtime  = models.CharField(max_length=30,  blank=True, default='', help_text="e.g. 00:46:43")
    vi_filesize = models.CharField(max_length=30,  blank=True, default='', help_text="e.g. 102 MB")
    vi_subtitle = models.CharField(max_length=60,  blank=True, default='', help_text="e.g. English")

    def _compute_seo_suffix(self):
        """
        Derive a short SEO suffix from categories/vi_country.
        Returns a slugified label like 'korean-drama', 'hollywood-movie', etc.
        Called from the reslug_movies management command and the scraper post-save hook.
        """
        cat_names = [c.name.lower() for c in self.categories.all()]
        country = (self.vi_country or '').lower()

        if 'chinese drama' in cat_names or 'chinese' in country:
            return 'chinese-drama'
        elif 'korean drama' in cat_names or 'k drama' in cat_names or 'korean' in country:
            return 'korean-drama'
        elif 'thai drama' in cat_names or 'thai' in country:
            return 'thai-drama'
        elif 'turkish drama' in cat_names or 'turkish' in country:
            return 'turkish-drama'
        elif 'spanish drama' in cat_names or 'spanish' in country:
            return 'spanish-drama'
        elif 'filipino drama' in cat_names or 'filipino' in cat_names:
            return 'filipino-drama'
        elif 'anime' in cat_names:
            return 'anime-series'
        elif 'nollywood tv series' in cat_names:
            return 'nollywood-series'
        elif 'hollywood tv series' in cat_names:
            return 'hollywood-tv-series'
        elif 'sa series' in cat_names or 'south africa' in cat_names:
            return 'sa-series'
        elif 'tv series' in cat_names or 'series' in cat_names:
            return 'tv-series'
        elif 'japanese movie' in cat_names:
            return 'japanese-movie'
        elif 'animation movie' in cat_names:
            return 'animation-movie'
        elif 'bollywood' in cat_names or 'bollywood movies' in cat_names:
            return 'bollywood-movie'
        elif 'nollywood movie' in cat_names or 'nollywood movies' in cat_names or 'nollywood' in cat_names:
            return 'nollywood-movie'
        elif 'hollywood movie' in cat_names or 'hollywood movies' in cat_names or 'hollywood' in cat_names:
            return 'hollywood-movie'
        else:
            return 'download'

    def _generate_unique_slug(self, seo_suffix=''):
        """
        Build a slug from the title (+ optional seo_suffix) and append a
        numeric suffix only if a collision exists.
        e.g. "rick-and-morty-s09-hollywood-tv-series-download"
             "filing-for-love-s01-korean-drama-download-2"  (if collision)
        """
        base = slugify(self.title)
        if seo_suffix:
            base = f"{base}-{seo_suffix}-download"
        slug = base
        n = 1
        qs = Movie.objects.exclude(pk=self.pk)
        while qs.filter(slug=slug).exists():
            n += 1
            slug = f"{base}-{n}"
        return slug

    def save(self, *args, **kwargs):
        # Only generate slug if the field is blank (first save, or blank override).
        # Preserves manually-set slugs and never rewrites an existing one.
        if not self.slug:
            self.slug = self._generate_unique_slug()
        # Auto-derive the show grouping key so all seasons of a show line up.
        if not self.show_key:
            from movies.scraper_utils import parse_show
            key, season = parse_show(self.title)
            self.show_key = key
            if self.season_number is None:
                self.season_number = season
        super().save(*args, **kwargs)


    def __str__(self):
        return self.title

    def get_absolute_url(self):
        # Canonical URL: /movie/<id>/<slug>/
        return reverse('movies:movie_detail', args=[str(self.pk), self.slug])


class DownloadLink(models.Model):
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name='download_links')
    label = models.CharField(max_length=255, blank=True)
    url   = models.URLField()

    # ── Multi-source fallback metadata ───────────────────────────────────────
    # Which site this link came from (e.g. '9jarocks', 'thenkiri', 'naijaprey').
    source = models.CharField(max_length=40, blank=True, default='', db_index=True)
    # Lower = tried first. The primary source = 1, fallbacks = 2, 3 … so the app
    # tries the main link, then fails over to the next working server.
    priority = models.PositiveSmallIntegerField(default=100)
    # For series: the episode this link is for, as integers (NOT a parsed label),
    # so the same episode from different sources groups together for fallback.
    season_number  = models.PositiveSmallIntegerField(null=True, blank=True)
    episode_number = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        # Within a movie, order by episode then by priority — so each episode's
        # links come out main-first, fallbacks after.
        ordering = ['season_number', 'episode_number', 'priority']
        indexes = [
            models.Index(fields=['movie', 'season_number', 'episode_number']),
        ]

    def __str__(self):
        return f"{self.label or 'Link'} – {self.url}"


# PWA models
class PWAInstallation(models.Model):
    user       = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    user_agent = models.TextField()
    installed_at = models.DateTimeField(auto_now_add=True)
    platform   = models.CharField(max_length=50)

    class Meta:
        db_table = 'pwa_installations'


class PushSubscription(models.Model):
    user      = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    endpoint  = models.URLField()
    p256dh_key = models.TextField()
    auth_key  = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'push_subscriptions'
        unique_together = ('user', 'endpoint')


class OfflineAction(models.Model):
    user        = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    action_type = models.CharField(max_length=50)
    action_data = models.JSONField()
    created_at  = models.DateTimeField(auto_now_add=True)
    synced      = models.BooleanField(default=False)

    class Meta:
        db_table = 'offline_actions'


class Comment(models.Model):
    user       = models.ForeignKey(User, on_delete=models.CASCADE, related_name='comments',
                                   null=True, blank=True)
    guest_name = models.CharField(max_length=100, blank=True, null=True,
                                  help_text="Name for anonymous comments")
    movie  = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name='comments')
    parent = models.ForeignKey('self', on_delete=models.CASCADE, related_name='replies',
                               null=True, blank=True)
    content    = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        author = self.user.username if self.user else self.guest_name
        return f"Comment by {author} on {self.movie.title}"

    @property
    def is_reply(self):
        return self.parent is not None

class Person(models.Model):
    """An actor/cast member, matched from TMDB."""
    tmdb_id     = models.IntegerField(unique=True, db_index=True)
    name        = models.CharField(max_length=200)
    profile_url = models.URLField(max_length=500, blank=True, null=True,
                                  help_text="Headshot, re-hosted to R2.")

    def __str__(self):
        return self.name

    @property
    def slug(self):
        return slugify(self.name) or 'actor'

    def get_absolute_url(self):
        from django.urls import reverse
        return reverse('movies:actor', kwargs={'pk': self.pk, 'slug': self.slug})


class MovieCast(models.Model):
    """Links a Movie to a Person (cast member) with their character + billing."""
    movie     = models.ForeignKey(Movie, on_delete=models.CASCADE,
                                  related_name='cast_credits')
    person    = models.ForeignKey(Person, on_delete=models.CASCADE,
                                  related_name='roles')
    character = models.CharField(max_length=200, blank=True)
    order     = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ('movie', 'person')
        ordering = ['order']

    def __str__(self):
        return f"{self.person.name} in {self.movie.title}"


class UpcomingTitle(models.Model):
    """A not-yet-released TMDB movie/show shown in 'Coming Soon'. Auto-removed
    once a Movie with the same tmdb_id enters the catalogue."""
    tmdb_id      = models.IntegerField(unique=True, db_index=True)
    media_type   = models.CharField(max_length=10)  # movie / tv
    title        = models.CharField(max_length=255)
    overview     = models.TextField(blank=True)
    release_date = models.CharField(max_length=20, blank=True)
    poster_url   = models.URLField(max_length=500, blank=True, null=True)
    trailer_url  = models.URLField(max_length=500, blank=True, null=True)
    rating       = models.FloatField(null=True, blank=True)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['release_date']

    def __str__(self):
        return f"{self.title} ({self.release_date})"
