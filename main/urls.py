# main/urls.py
from django.urls import path
from django.views.generic import RedirectView
from . import views
from .pwa_views import (
    manifest_view, 
    service_worker_view, 
    offline_view, 
    push_subscribe_view
)

app_name = 'main'

urlpatterns = [
    
    path('access/', views.ping_view, name='ping'),
    # Main homepage retired — the real home is the movies app at '/'. Redirect,
    # because this vestigial page referenced the now-removed anime/manga URLs.
    path('', RedirectView.as_view(url='/', permanent=False), name='home'),
    
    # PWA URLs
    path('manifest.json', manifest_view, name='pwa_manifest'),
    path('sw.js', service_worker_view, name='service_worker'),
    path('offline.html', offline_view, name='offline'),
    path('api/push-subscribe/', push_subscribe_view, name='push_subscribe'),
]