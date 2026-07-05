"""
"Scan a Scene" — identify a movie / TV title from screenshot(s) or a social
clip link, using a vision model (Gemini, free tier) canonicalised through TMDB.

Pipeline:  image(s) | social URL  →  Gemini vision (title/year/cast guesses)
           →  TMDB search + details (canonical poster/cast/year/tmdb_id).

The app then matches the returned `tmdb_id` against its OWN Supabase catalogue
to decide "Watch now" vs "Request title" — so this endpoint stays DB-agnostic.

Config (server-side only):
  GEMINI_API_KEY   free key from https://aistudio.google.com
  GEMINI_MODEL     optional, default 'gemini-2.0-flash'
  TMDB_API_KEY     already used by the enrichment pipeline
"""
import base64
import io
import json

import requests
from bs4 import BeautifulSoup
from decouple import config
from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import tmdb

GEMINI_MODEL = config('GEMINI_MODEL', default='gemini-2.5-flash')
MAX_IMAGES = 4
MAX_DIM = 1280  # downscale frames before sending (higher = better detail for
#                 identification, still small enough for quota + bandwidth)

UA = ('Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120 Mobile Safari/537.36')


def _gemini_keys():
    """Pool of Gemini keys. Prefer GEMINI_API_KEYS (comma-separated) for
    rotation/failover; fall back to a single GEMINI_API_KEY."""
    multi = config('GEMINI_API_KEYS', default='')
    if multi.strip():
        return [k.strip() for k in multi.split(',') if k.strip()]
    single = config('GEMINI_API_KEY', default='')
    return [single] if single else []


def is_configured():
    return bool(_gemini_keys())


def _call_gemini(body):
    """POST a generateContent request, rotating through the key pool and moving
    to the next key on a rate-limit / quota error (429/403). Returns the parsed
    JSON dict the model produced, or {'error': ...}, or None."""
    keys = _gemini_keys()
    if not keys:
        return None
    url = (f'https://generativelanguage.googleapis.com/v1beta/models/'
           f'{GEMINI_MODEL}:generateContent')
    # Round-robin starting offset so load spreads evenly across the keys.
    start = cache.get('gemini_key_rr', 0)
    cache.set('gemini_key_rr', (start + 1) % len(keys), 3600)
    last_status = None
    for i in range(len(keys)):
        key = keys[(start + i) % len(keys)]
        try:
            r = requests.post(url, params={'key': key}, json=body, timeout=40)
        except Exception:
            continue
        if r.status_code == 200:
            try:
                data = r.json()
                text = data['candidates'][0]['content']['parts'][0]['text']
                return json.loads(text)
            except Exception:
                return None
        last_status = r.status_code
        if r.status_code in (429, 403):
            continue  # this key is rate-limited/exhausted → try the next
        return {'error': f'vision_http_{r.status_code}'}
    return {'error': f'vision_http_{last_status or "exhausted"}'}


# ── Image helpers ────────────────────────────────────────────────────────────
def _prep_image(raw):
    """Downscale + re-encode to JPEG so payloads stay small. Returns a base64
    string, or None if the bytes aren't a valid image."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw)).convert('RGB')
        w, h = im.size
        scale = min(1.0, MAX_DIM / float(max(w, h)))
        if scale < 1.0:
            im = im.resize((int(w * scale), int(h * scale)))
        out = io.BytesIO()
        im.save(out, format='JPEG', quality=85)
        return base64.b64encode(out.getvalue()).decode()
    except Exception:
        return None


# ── Social link → a representative frame + caption ───────────────────────────
def resolve_social_url(url):
    """Fetch a public TikTok/IG/FB/YouTube post's preview image (og:image) and
    caption (og:description). Returns (image_bytes|None, caption|'')."""
    try:
        r = requests.get(url, headers={'User-Agent': UA}, timeout=15,
                         allow_redirects=True)
        if r.status_code != 200:
            return None, ''
        soup = BeautifulSoup(r.text, 'lxml')

        def meta(prop):
            tag = (soup.find('meta', property=prop)
                   or soup.find('meta', attrs={'name': prop}))
            return (tag.get('content') if tag else '') or ''

        img_url = meta('og:image') or meta('twitter:image')
        caption = (meta('og:description') or meta('twitter:description')
                   or meta('og:title'))

        # TikTok oEmbed gives a more reliable thumbnail than og:image.
        if (not img_url) and 'tiktok.com' in url:
            try:
                o = requests.get('https://www.tiktok.com/oembed',
                                 params={'url': url},
                                 headers={'User-Agent': UA}, timeout=12)
                if o.status_code == 200:
                    j = o.json()
                    img_url = j.get('thumbnail_url', '')
                    caption = caption or j.get('title', '')
            except Exception:
                pass

        img_bytes = None
        if img_url:
            ir = requests.get(img_url, headers={'User-Agent': UA}, timeout=15)
            if ir.status_code == 200:
                img_bytes = ir.content
        return img_bytes, (caption or '').strip()
    except Exception:
        return None, ''


# ── Gemini vision call ───────────────────────────────────────────────────────
_PROMPT = (
    "You are a film & TV identification expert. Look VERY carefully at the "
    "frame(s) below, taken from a movie or TV show, and identify EXACTLY what "
    "it is.\n"
    "{hint}"
    "Use EVERY clue: the actors' faces, any on-screen text or subtitles, "
    "signage/logos, channel or streaming watermarks, costumes, setting, time "
    "period, colour grade and visual style. If several frames are given, they "
    "are from the SAME title — combine them.\n"
    "Be precise and literal: identify the SPECIFIC film or show and the correct "
    "entry in a franchise/sequel, with the correct release year. Distinguish "
    "remakes and same-named titles by their year and cast. For a TV show, name "
    "the SHOW itself (not just its genre). Do NOT guess wildly — if you are not "
    "sure, LOWER the confidence and give alternates rather than inventing a "
    "title.\n"
    "Return STRICT JSON only, no prose, in exactly this shape:\n"
    '{"matches":[{"title":"<exact title>","year":<release year or null>,'
    '"media_type":"movie or tv","cast":["actor names you recognise"],'
    '"confidence":"high or medium or low"}],"note":"<short note if unsure, '
    'else empty>"}\n'
    "Give up to 3 candidate matches, MOST LIKELY FIRST. Only mark a match "
    "'high' when you are genuinely certain. If you truly cannot tell, return an "
    "empty matches list."
)


_QUOTE_PROMPT = (
    "The following is a line of dialogue (a quote) from a movie or TV show:\n"
    '"{quote}"\n'
    "Identify which title(s) it comes from. Put the title MOST strongly "
    "associated with this exact line first, then list other movies/shows that "
    "used the same or a very similar line. For each, name the character who "
    "said it.\n"
    "Return STRICT JSON only, no prose:\n"
    '{"matches":[{"title":"<title>","year":<year or null>,'
    '"media_type":"movie or tv","character":"<who said it>",'
    '"cast":["lead actors"],"confidence":"high or medium or low"}],'
    '"note":"<short note if unsure, else empty>"}\n'
    "Give up to 5 matches, best first. If the line is too generic to attribute, "
    "return an empty matches list."
)


def identify_from_quote(quote):
    """Identify which film/TV a famous line of dialogue is from (text-only)."""
    if not quote:
        return None
    body = {
        'contents': [
            {'parts': [{'text': _QUOTE_PROMPT.replace('{quote}', quote[:300])}]}
        ],
        'generationConfig': {
            'temperature': 0.3,
            'response_mime_type': 'application/json',
        },
    }
    return _call_gemini(body)


def identify_from_images(images_b64, hint=''):
    """Send prepared frames to Gemini; return the parsed dict or None."""
    if not images_b64:
        return None
    hint_line = (f'Context/caption from where the clip was shared: "{hint}".\n'
                 if hint else '')
    parts = [{'text': _PROMPT.replace('{hint}', hint_line)}]
    for b64 in images_b64:
        parts.append({'inline_data': {'mime_type': 'image/jpeg', 'data': b64}})
    body = {
        'contents': [{'parts': parts}],
        'generationConfig': {
            'temperature': 0.2,
            'response_mime_type': 'application/json',
        },
    }
    return _call_gemini(body)


# ── TMDB canonicalise ────────────────────────────────────────────────────────
def _enrich_match(m):
    """Turn one Gemini guess into a richer result via TMDB. The app does the
    catalogue match itself (by tmdb_id) against its own Supabase DB."""
    title = (m.get('title') or '').strip()
    if not title:
        return None
    is_series = (m.get('media_type') == 'tv')
    year = m.get('year')
    result = {
        'title': title,
        'year': year,
        'media_type': m.get('media_type') or 'movie',
        'cast': m.get('cast') or [],
        'confidence': m.get('confidence') or 'low',
        # For quote searches: who said the line (e.g. "Said by Thanos").
        'context': (m.get('character') or m.get('context') or '').strip(),
        'poster_url': None,
        'overview': '',
        'tmdb_id': None,
    }
    found = tmdb.search(title, year=year, is_series=is_series)
    if found:
        tmdb_id, media = found
        result['tmdb_id'] = tmdb_id
        det = tmdb.details(tmdb_id, media)
        if det:
            result['year'] = det.get('year') or year
            result['poster_url'] = det.get('poster_url')
            result['overview'] = det.get('overview')
            if det.get('cast'):
                result['cast'] = [c.strip() for c in det['cast'].split(',')
                                  if c.strip()]
    return result


# ── HTTP endpoint ────────────────────────────────────────────────────────────
@csrf_exempt
@require_POST
def identify_scene(request):
    if not is_configured():
        return JsonResponse({'ok': False, 'error': 'not_configured'},
                            status=503)

    # Light per-IP throttle to protect the free vision quota from abuse.
    ip = request.META.get('HTTP_X_FORWARDED_FOR',
                          request.META.get('REMOTE_ADDR', '')).split(',')[0]
    ck = f'scene_id_rl_{ip.strip()}'
    n = cache.get(ck, 0)
    if n >= 30:
        return JsonResponse({'ok': False, 'error': 'rate_limited'}, status=429)
    cache.set(ck, n + 1, 3600)

    # 0) Quote branch — identify a title from a famous line of dialogue (text).
    quote = (request.POST.get('quote') or '').strip()
    if quote:
        parsed = identify_from_quote(quote)
    else:
        images_b64 = []
        hint = (request.POST.get('hint') or '').strip()

        # 1) Social URL branch (TikTok / IG / FB / YouTube link).
        url = (request.POST.get('url') or '').strip()
        if url:
            img_bytes, caption = resolve_social_url(url)
            if not img_bytes:
                return JsonResponse({
                    'ok': False, 'error': 'link_unreachable',
                    'message': 'Could not read that link — it may be private '
                               'or unsupported.'})
            b64 = _prep_image(img_bytes)
            if b64:
                images_b64.append(b64)
            if caption and not hint:
                hint = caption

        # 2) Uploaded frames (screenshots, or clip frames the app extracted).
        for f in request.FILES.getlist('images')[:MAX_IMAGES]:
            b64 = _prep_image(f.read())
            if b64:
                images_b64.append(b64)

        if not images_b64:
            return JsonResponse({'ok': False, 'error': 'no_image',
                                 'message': 'No usable image was provided.'})

        parsed = identify_from_images(images_b64[:MAX_IMAGES], hint=hint)

    if not parsed or parsed.get('error'):
        return JsonResponse({'ok': False, 'error': 'vision_failed'})

    results = []
    for m in (parsed.get('matches') or [])[:5]:
        enriched = _enrich_match(m)
        if enriched:
            results.append(enriched)

    return JsonResponse({
        'ok': True,
        'results': results,
        'note': parsed.get('note') or (
            '' if results else "Couldn't confidently identify this scene."),
    })
