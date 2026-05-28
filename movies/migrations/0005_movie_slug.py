# movies/migrations/0005_movie_slug.py
from django.db import migrations, models
from django.utils.text import slugify


def forwards_generate_slugs(apps, schema_editor):
    Movie = apps.get_model('movies', 'Movie')
    seen = {}

    for movie in Movie.objects.order_by('pk'):
        base = slugify(movie.title) or f"movie-{movie.pk}"
        if base not in seen:
            slug = base
            seen[base] = 1
        else:
            seen[base] += 1
            slug = f"{base}-{seen[base]}"
        movie.slug = slug
        movie.save(update_fields=['slug'])


def backwards_clear_slugs(apps, schema_editor):
    Movie = apps.get_model('movies', 'Movie')
    Movie.objects.update(slug='')


class Migration(migrations.Migration):

    dependencies = [
        ('movies', '0004_alter_movie_download_url_alter_movie_image_url_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='movie',
            name='slug',
            field=models.SlugField(
                max_length=250,
                blank=True,
                null=True,
                help_text='Auto-generated from title. Used in SEO URLs.',
            ),
        ),
        migrations.RunPython(forwards_generate_slugs, backwards_clear_slugs),
        migrations.AlterField(
            model_name='movie',
            name='slug',
            field=models.SlugField(
                max_length=250,
                unique=True,
                blank=True,
                null=False,
                default='',
                help_text='Auto-generated from title. Used in SEO URLs.',
            ),
        ),
    ]