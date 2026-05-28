from django.utils.text import slugify
from movies.models import Movie
from django.db.models import Q

existing_slugs = set(
    Movie.objects.exclude(
        Q(slug='') | Q(slug__isnull=True)
    ).values_list('slug', flat=True)
)

BATCH_SIZE = 1000
done = 0
last_id = 0

while True:
    movies = list(
        Movie.objects.filter(
            Q(slug='') | Q(slug__isnull=True),
            id__gt=last_id
        ).only('id', 'title').order_by('id')[:BATCH_SIZE]
    )

    if not movies:
        break

    for movie in movies:
        base = slugify(movie.title) or f"movie-{movie.id}"
        slug = base
        n = 1

        while slug in existing_slugs:
            n += 1
            slug = f"{base}-{n}"

        existing_slugs.add(slug)
        movie.slug = slug

    Movie.objects.bulk_update(movies, ['slug'])

    done += len(movies)
    last_id = movies[-1].id

    print(f"{done} updated...")

print(f"Done! {done} slugs generated.")