# main/middleware.py
# Create this new file in your main app

from django.http import HttpResponse
from django.conf import settings

class PWAMiddleware:
    """
    Middleware to add PWA-specific headers and handle offline functionality
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        
        # Add PWA-specific headers
        if request.path.endswith('.js'):
            response['Service-Worker-Allowed'] = '/'
            
        # Add cache control for static files
        if any(request.path.startswith(prefix) for prefix in ['/static/', '/media/']):
            response['Cache-Control'] = f'public, max-age={settings.CACHE_CONTROL_MAX_AGE}'
            
        # Add security headers for PWA
        response['X-Content-Type-Options'] = 'nosniff'
        response['X-Frame-Options'] = 'SAMEORIGIN'

        return response


class EdgeCacheMiddleware:
    """
    Make ANONYMOUS GET content pages cacheable at the CDN (Cloudflare), so bot +
    anonymous traffic is served from the edge instead of hitting Render (this is
    what keeps Render egress under the free 5 GB).

    Cloudflare refuses to cache any response that sets a cookie, and Django sets
    a `csrftoken` cookie on pages with forms — that's why everything is currently
    `cf-cache-status: DYNAMIC`. For anonymous visitors on public content pages we
    therefore:
      • strip Set-Cookie (they need no session/CSRF cookie — like/comment/
        watchlist are all @login_required, so no anonymous action can break), and
      • send `Cache-Control: public` with an s-maxage for the shared edge cache.

    Deliberately narrow: only GET/HEAD, only status 200, only anonymous users,
    only whitelisted public paths. Logged-in users, POST, the download/stream
    gates, /accounts/, admin and API keep their cookies and stay uncached.
    """
    CACHE_PREFIXES = (
        '/movie/', '/movies/movie/', '/category/', '/movies/category/',
        '/a-z/', '/genres/', '/actor/', '/news/',
    )
    SKIP_SUBSTR = ('/download/', '/stream/', '/like/', '/watchlist/', '/comment/')
    SKIP_PREFIXES = (
        '/accounts/', '/api/', '/ajax/', '/watch2d/', '/admin/', '/access/',
        '/resolve-download/', '/check-streamable/',
    )
    # 30 min at the edge; browsers revalidate (max-age=0) so a change still
    # reaches real users quickly once the edge entry expires.
    CACHE_CONTROL = 'public, max-age=0, s-maxage=1800'

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            if request.method not in ('GET', 'HEAD'):
                return response
            if getattr(response, 'status_code', None) != 200:
                return response
            user = getattr(request, 'user', None)
            if user is not None and user.is_authenticated:
                return response
            path = request.path
            if any(path.startswith(p) for p in self.SKIP_PREFIXES):
                return response
            if any(s in path for s in self.SKIP_SUBSTR):
                return response
            if not (path == '/' or any(path.startswith(p) for p in self.CACHE_PREFIXES)):
                return response
            # Drop cookies so Cloudflare will cache the response.
            response.cookies.clear()
            if response.has_header('Set-Cookie'):
                del response['Set-Cookie']
            response['Cache-Control'] = self.CACHE_CONTROL
        except Exception:
            # Caching is best-effort — never break a page trying to make it cacheable.
            pass
        return response