# movies/models.py
from urllib.parse import quote

from django.db import models
from django.contrib.auth.models import User
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone
from django.urls import reverse
from django.utils.text import slugify


class Profile(models.Model):
    """Public identity layer on top of Django's User — nobody's real name,
    email or account username is ever shown publicly. Every user gets a fun
    auto-generated nickname + avatar at signup (see signals.py), and can
    change the nickname later from their profile page."""
    user         = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    display_name = models.CharField(max_length=30, unique=True)
    avatar_seed  = models.CharField(max_length=40, blank=True, default='',
                                    help_text="Seed for the generated avatar — stays fixed even if display_name changes.")
    custom_avatar_url = models.URLField(max_length=500, blank=True, default='',
                                    help_text="Fixed avatar (e.g. the site logo for the official Watch2D account) — takes priority over the generated one.")
    created_at   = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.display_name

    @property
    def avatar_url(self):
        if self.custom_avatar_url:
            return self.custom_avatar_url
        seed = self.avatar_seed or self.display_name
        return f'https://api.dicebear.com/9.x/adventurer/svg?seed={quote(seed)}&backgroundType=gradientLinear'

    # ── Verification tick ────────────────────────────────────────────────
    # 'gold' = official Watch2D staff account. 'blue' = earned by real usage
    # (written reviews + account age + watched count), so it can't be farmed
    # by a fresh account posting empty ratings. Computed live rather than a
    # manually-flipped flag so it appears the moment the bar is met.
    BLUE_TICK_REVIEWS = 10
    BLUE_TICK_ACCOUNT_AGE_DAYS = 14
    BLUE_TICK_WATCHED = 5

    def blue_tick_progress(self):
        """Raw counts + pass/fail per requirement — used for the profile's
        progress display. Not cached: only called on a profile page view."""
        written_reviews = self.user.reviews.exclude(content='').count()
        account_age_days = (timezone.now() - self.user.date_joined).days
        watched_count = self.user.watched_movies.count()
        pct = lambda have, needed: min(100, round(have * 100 / needed))
        return {
            'reviews': written_reviews, 'reviews_needed': self.BLUE_TICK_REVIEWS,
            'reviews_met': written_reviews >= self.BLUE_TICK_REVIEWS,
            'reviews_pct': pct(written_reviews, self.BLUE_TICK_REVIEWS),
            'age_days': account_age_days, 'age_needed': self.BLUE_TICK_ACCOUNT_AGE_DAYS,
            'age_met': account_age_days >= self.BLUE_TICK_ACCOUNT_AGE_DAYS,
            'age_pct': pct(account_age_days, self.BLUE_TICK_ACCOUNT_AGE_DAYS),
            'watched': watched_count, 'watched_needed': self.BLUE_TICK_WATCHED,
            'watched_met': watched_count >= self.BLUE_TICK_WATCHED,
            'watched_pct': pct(watched_count, self.BLUE_TICK_WATCHED),
        }

    @property
    def verification_tier(self):
        """'gold', 'blue', or None. This is evaluated everywhere a display
        name is shown (nav, comments, reviews), so the blue check is cached
        briefly per-user to avoid a burst of COUNT queries on busy pages."""
        if self.user.is_staff:
            return 'gold'
        from django.core.cache import cache
        cache_key = f'verify_tier_blue_{self.user_id}'
        cached = cache.get(cache_key)
        if cached is not None:
            return cached or None
        progress = self.blue_tick_progress()
        is_blue = progress['reviews_met'] and progress['age_met'] and progress['watched_met']
        cache.set(cache_key, 'blue' if is_blue else '', 300)
        return 'blue' if is_blue else None


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
    backdrop_url = models.URLField("Backdrop Image URL", blank=True, null=True, max_length=500,
                                   help_text="Wide landscape still from TMDB, used as the movie "
                                             "detail page's hero banner (separate from the poster).")
    # ── TMDB enrichment (rating / trailer / matched id) ───────────────
    tmdb_id      = models.IntegerField(null=True, blank=True, db_index=True,
                                       help_text="Matched TheMovieDB id, if any.")
    tmdb_synced  = models.BooleanField(default=False,
                                       help_text="TMDB enrichment has been attempted.")
    genres_synced = models.BooleanField(default=False,
                                        help_text="TMDB genres have been linked as categories.")
    tmdb_seasons = models.TextField(
        blank=True, default='',
        help_text='JSON map of season_number → episode_count for series, from '
                  'TMDB (e.g. {"1": 10, "2": 8}). Powers the episode selector.')
    rating       = models.FloatField(null=True, blank=True,
                                     help_text="TMDB rating (0–10).")
    imdb_id      = models.CharField(max_length=20, blank=True, null=True, db_index=True,
                                    help_text="IMDb id (e.g. 'tt1234567'), from TMDB external_ids. "
                                              "Used to fetch IMDb/Rotten Tomatoes ratings via OMDb.")
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
    # Self-reported "I've seen this" diary log (Letterboxd-style) — set via the
    # Watched button, independent of rating/review.
    watched_by = models.ManyToManyField(User, related_name='watched_movies', blank=True)
    # Set ONLY when a logged-in user actually passes through the stream/download
    # gate for this title — proof they watched it *on Watch2D*, not just a
    # self-reported claim. Powers the "Watched on Watch2D" badge on reviews,
    # something a pure review-aggregator (RT/Letterboxd) can never verify.
    verified_watched_by = models.ManyToManyField(User, related_name='verified_watched_movies', blank=True)
    views = models.PositiveIntegerField(default=0, db_index=True)
    # Same counter but reset weekly (see reset_weekly_views management command) —
    # powers "Trending This Week" without disturbing the all-time `views` total.
    weekly_views = models.PositiveIntegerField(default=0, db_index=True)

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
            seo_suffix = seo_suffix.strip('-')
            # Guard against "…-download-download": when the suffix is already the
            # generic 'download' (or itself ends in it), don't append '-download'.
            if seo_suffix == 'download' or seo_suffix.endswith('-download'):
                base = f"{base}-{seo_suffix}"
            else:
                base = f"{base}-{seo_suffix}-download"
        base = base.strip('-')
        if not base:
            # Non-Latin titles (Chinese/Korean/Japanese/…) slugify to '' — an empty
            # slug can't be reversed by the movie_detail URL (NoReverseMatch → the
            # page 500s), so fall back to a generic, always-reversible base.
            base = 'movie'
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
        # Canonical URL: /movie/<id>/<slug>/. Guard against a blank slug (which
        # can't be reversed → NoReverseMatch) so a stray row can never 500 a page.
        return reverse('movies:movie_detail', args=[str(self.pk), self.slug or 'movie'])


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

    # Set when this exact file has been archived to the private Telegram
    # "Watch2D File Storage" channel — the message id the bot copyMessage()s
    # from when a user requests it. Null until the upload pipeline succeeds.
    telegram_message_id = models.BigIntegerField(null=True, blank=True, db_index=True)

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

    def reaction_counts(self):
        """{'🔥': 3, ...} — uses the prefetch cache when reactions was
        prefetched, so this costs zero extra queries on the comment list."""
        counts = {}
        for r in self.reactions.all():
            counts[r.emoji] = counts.get(r.emoji, 0) + 1
        return counts


class CommentReaction(models.Model):
    """One emoji reaction per user per comment/reply — same reaction set as
    the app's 'Gist' feed, for brand consistency. Picking a different emoji
    replaces the old one; picking the same one again removes it."""
    REACTION_CHOICES = [
        ('🔥', 'Fire'), ('😂', 'Laugh'), ('😮', 'Wow'), ('💀', 'Dead'), ('😍', 'Love'),
    ]
    comment = models.ForeignKey(Comment, on_delete=models.CASCADE, related_name='reactions')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='comment_reactions')
    emoji = models.CharField(max_length=8, choices=REACTION_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('comment', 'user')

    def __str__(self):
        return f"{self.user.username} reacted {self.emoji} to comment #{self.comment_id}"


class ExternalRating(models.Model):
    """A critic score for a Movie, pulled from an external source (IMDb/Rotten
    Tomatoes via OMDb, Letterboxd via scrape). Refreshed periodically by the
    fetch_external_ratings management command — never fetched on request."""
    SOURCE_CHOICES = [
        ('imdb', 'IMDb'),
        ('rt', 'Rotten Tomatoes'),
        ('letterboxd', 'Letterboxd'),
    ]
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name='external_ratings')
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES)
    score = models.FloatField(help_text="Normalized 0–10 score, for averaging across sources.")
    display = models.CharField(max_length=20, blank=True, default='',
                               help_text="Native display string, e.g. '87%', '7.8/10'.")
    # Only populated for source='rt' — the Popcornmeter (audience) side.
    audience_score = models.FloatField(null=True, blank=True)
    audience_display = models.CharField(max_length=20, blank=True, default='')
    url = models.URLField(max_length=500, blank=True, default='')
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('movie', 'source')

    def __str__(self):
        return f"{self.get_source_display()} rating for {self.movie.title}: {self.display}"


class Review(models.Model):
    """A logged-in user's star rating + optional written review for a Movie.
    Unlike Comment, no guest reviews — ratings must be attributable to a real
    account so they can fairly count toward the community average."""
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name='reviews')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reviews')
    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text="1–10 scale, displayed as 0.5–5.0 stars (rating / 2)."
    )
    content = models.TextField(blank=True, default='', help_text="Optional written review.")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ('movie', 'user')

    def __str__(self):
        return f"{self.user.username}'s review of {self.movie.title} ({self.rating}/10)"

    @property
    def stars(self):
        return self.rating / 2

    @property
    def full_stars(self):
        return self.rating // 2

    @property
    def has_half_star(self):
        return self.rating % 2 == 1


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


class MovieCrew(models.Model):
    """Links a Movie to a Person on the crew side (Director/Writer/Producer),
    from TMDB credits.crew. Powers the Cast/Director/Writers/Producers tabs."""
    DEPARTMENT_CHOICES = [
        ('directing', 'Directing'),
        ('writing', 'Writing'),
        ('production', 'Production'),
    ]
    movie      = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name='crew_credits')
    person     = models.ForeignKey(Person, on_delete=models.CASCADE, related_name='crew_roles')
    job        = models.CharField(max_length=100, help_text="e.g. Director, Writer, Producer")
    department = models.CharField(max_length=20, choices=DEPARTMENT_CHOICES)
    order      = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ('movie', 'person', 'job')
        ordering = ['order']

    def __str__(self):
        return f"{self.person.name} ({self.job}) on {self.movie.title}"


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


class NotifyRequest(models.Model):
    """A user asking to be pinged the moment an upcoming title becomes
    watchable on Watch2D. Deliberately keyed by tmdb_id (a snapshot), NOT a
    FK to UpcomingTitle — that row gets deleted the instant a matching Movie
    is scraped in (see fetch_upcoming.py), which would otherwise wipe this
    request before anyone could act on it. The notify_web_arrivals command
    checks Movie.tmdb_id directly, independent of UpcomingTitle's lifecycle,
    then deletes these rows once sent (one-time notification)."""
    user       = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notify_requests')
    tmdb_id    = models.IntegerField(db_index=True)
    title      = models.CharField(max_length=255)
    media_type = models.CharField(max_length=10, default='movie')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'tmdb_id')

    def __str__(self):
        return f"{self.user.username} wants to know about {self.title}"


# ── "Gist": entertainment-news engagement feed ───────────────────────────────
class NewsPost(models.Model):
    """One scraped entertainment-news item, AI-enriched and (when possible)
    linked to a catalogue title so it drives watches. Reactions live in
    NewsReaction; the counts here are denormalised for the TRENDING sort."""
    title      = models.CharField(max_length=300)
    summary    = models.TextField(blank=True, default='')
    hot_take   = models.TextField(blank=True, default='')   # AI discussion starter
    url        = models.URLField(max_length=600, unique=True)  # source (dedup key)
    source     = models.CharField(max_length=80, blank=True, default='')
    image_url  = models.URLField(max_length=600, blank=True, default='')
    # e.g. 'Nollywood', 'Hollywood', 'K-Drama', 'Music', 'Celebrity'.
    category   = models.CharField(max_length=60, blank=True, default='', db_index=True)
    # The ecosystem loop: link to a title we carry, so users can jump to Watch.
    movie      = models.ForeignKey(Movie, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name='news')
    tmdb_id    = models.IntegerField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at   = models.DateTimeField(auto_now_add=True, db_index=True)
    # Denormalised engagement for ranking (kept fresh by refresh_news_counts).
    reaction_count = models.PositiveIntegerField(default=0)
    comment_count  = models.PositiveIntegerField(default=0)
    is_published   = models.BooleanField(default=True)

    class Meta:
        ordering = ['-published_at', '-created_at']
        indexes = [
            models.Index(fields=['is_published', '-published_at']),
            models.Index(fields=['category', '-published_at']),
        ]

    def __str__(self):
        return self.title[:60]


class NewsReaction(models.Model):
    """A single user's reaction to a news item. Written by the app via Supabase
    (user_id = the Supabase auth uid). One reaction per user per post."""
    news    = models.ForeignKey(NewsPost, on_delete=models.CASCADE,
                                related_name='reactions')
    user_id = models.CharField(max_length=64, db_index=True)  # Supabase auth uid
    emoji   = models.CharField(max_length=8)  # 🔥 😂 😮 💀 😍
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('news', 'user_id')  # one reaction per user per post
        indexes = [models.Index(fields=['news', 'emoji'])]


class ContactMessage(models.Model):
    """A complaint / message a user sends from the app's Contact screen. Stored
    here (so it's never lost) and emailed to the admin via Brevo."""
    email      = models.EmailField(blank=True, default='')  # optional reply-to
    subject    = models.CharField(max_length=140, blank=True, default='')
    message    = models.TextField()
    user_id    = models.CharField(max_length=64, blank=True, default='')
    app_version = models.CharField(max_length=20, blank=True, default='')
    handled    = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.subject or 'Message'} — {self.email or 'anon'}"


class NewsComment(models.Model):
    """A user's 'shade'/comment on a news item (the live-room style thread)."""
    news    = models.ForeignKey(NewsPost, on_delete=models.CASCADE,
                                related_name='comments')
    user_id = models.CharField(max_length=64, db_index=True)  # Supabase auth uid
    user_name = models.CharField(max_length=80, blank=True, default='')
    body    = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['created_at']
