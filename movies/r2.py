"""
Re-host scraped poster images on Cloudflare R2 so the app no longer depends on
the source site staying up. Downloads the source image once and uploads it to
your R2 bucket; returns the public URL on your own domain.

Needs these env vars (same values as the APK build):
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY,
    R2_BUCKET, R2_PUBLIC_URL   (e.g. https://dl.watch2d.org)
"""
import os
import hashlib
import mimetypes
from urllib.parse import urlparse

from decouple import config

# R2 rejects the integrity checksums newer botocore adds by default — opt out.
os.environ.setdefault('AWS_REQUEST_CHECKSUM_CALCULATION', 'when_required')
os.environ.setdefault('AWS_RESPONSE_CHECKSUM_VALIDATION', 'when_required')

_client = None
_scraper = None


def _r2():
    global _client
    if _client is not None:
        return _client
    account = config('R2_ACCOUNT_ID', default='')
    key = config('R2_ACCESS_KEY_ID', default='')
    secret = config('R2_SECRET_ACCESS_KEY', default='')
    if not (account and key and secret):
        return None
    import boto3
    from botocore.config import Config
    _client = boto3.client(
        's3',
        endpoint_url=f'https://{account}.r2.cloudflarestorage.com',
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        config=Config(signature_version='s3v4', region_name='auto'),
    )
    return _client


def _downloader():
    global _scraper
    if _scraper is None:
        import cloudscraper
        _scraper = cloudscraper.create_scraper()
    return _scraper


def is_configured():
    return bool(config('R2_ACCOUNT_ID', default='') and
                config('R2_ACCESS_KEY_ID', default='') and
                config('R2_BUCKET', default='') and
                config('R2_PUBLIC_URL', default=''))


def rehost_image(image_url, movie_id=None):
    """Download [image_url] and upload to R2. The key is a hash of the source
    URL, so the same poster is stored once and shared across both DBs (no movie
    -id collisions). Returns the public URL, or None on any failure."""
    bucket = config('R2_BUCKET', default='')
    public = config('R2_PUBLIC_URL', default='').rstrip('/')
    client = _r2()
    if not (client and bucket and public and image_url):
        return None
    try:
        resp = _downloader().get(image_url, timeout=30)
        if resp.status_code != 200 or not resp.content:
            return None
        ctype = (resp.headers.get('Content-Type') or '').split(';')[0].strip()
        if not ctype.startswith('image'):
            ctype = 'image/jpeg'
        ext = (mimetypes.guess_extension(ctype)
               or os.path.splitext(urlparse(image_url).path)[1]
               or '.jpg')
        if ext in ('.jpe', '.jpeg'):
            ext = '.jpg'
        digest = hashlib.md5(image_url.encode('utf-8')).hexdigest()[:20]
        key = f'posters/{digest}{ext}'
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=resp.content,
            ContentType=ctype,
            CacheControl='public, max-age=31536000, immutable',
        )
        return f'{public}/{key}'
    except Exception:
        return None
