// Service Worker for Watch2D PWA
const CACHE_NAME = 'Watch2D-v1.0.0';
const RUNTIME_CACHE = 'Watch2D-runtime-v1.0.0';

// Core files to cache immediately
const CORE_CACHE_FILES = [
  '/',
  '/static/css/main.css',
  '/static/js/main.js',
  '/static/js/pwa.js',
  'https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEgGDg63ESTUKkQx6xcxK4dBd8LDkHo5VjiLkh1drq5WGGSG1dLVGQdwY7eXuVQ6Rxtz2mVSkcVvK7f7pFk5_4UVQc8uuX5HI_2J5IUZxR7uhvdmjxb-LEBmqR7zDjqiwjJVSmzv1fKtAt6nHr0EiDAMNPTNMq1yUnkdcMsA_9Z4Dasfc8bxJ0pnFLwafJk/s320/logo%20(3).png',
  'https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEgGDg63ESTUKkQx6xcxK4dBd8LDkHo5VjiLkh1drq5WGGSG1dLVGQdwY7eXuVQ6Rxtz2mVSkcVvK7f7pFk5_4UVQc8uuX5HI_2J5IUZxR7uhvdmjxb-LEBmqR7zDjqiwjJVSmzv1fKtAt6nHr0EiDAMNPTNMq1yUnkdcMsA_9Z4Dasfc8bxJ0pnFLwafJk/s320/logo%20(3).png',
  'https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEgGDg63ESTUKkQx6xcxK4dBd8LDkHo5VjiLkh1drq5WGGSG1dLVGQdwY7eXuVQ6Rxtz2mVSkcVvK7f7pFk5_4UVQc8uuX5HI_2J5IUZxR7uhvdmjxb-LEBmqR7zDjqiwjJVSmzv1fKtAt6nHr0EiDAMNPTNMq1yUnkdcMsA_9Z4Dasfc8bxJ0pnFLwafJk/s320/logo%20(3).png',
  'https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEgGDg63ESTUKkQx6xcxK4dBd8LDkHo5VjiLkh1drq5WGGSG1dLVGQdwY7eXuVQ6Rxtz2mVSkcVvK7f7pFk5_4UVQc8uuX5HI_2J5IUZxR7uhvdmjxb-LEBmqR7zDjqiwjJVSmzv1fKtAt6nHr0EiDAMNPTNMq1yUnkdcMsA_9Z4Dasfc8bxJ0pnFLwafJk/s320/logo%20(3).png',
  '/offline.html',
  'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css',
  'https://cdn.tailwindcss.com',
  'https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap'
];

// Install Event - Cache core files
self.addEventListener('install', (event) => {
  console.log('Service Worker installing...');
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => {
        console.log('Caching core files');
        return cache.addAll(CORE_CACHE_FILES);
      })
      .then(() => {
        console.log('Service Worker installed successfully');
        return self.skipWaiting();
      })
      .catch((error) => {
        console.error('Service Worker installation failed:', error);
      })
  );
});

// Activate Event - Clean up old caches
self.addEventListener('activate', (event) => {
  console.log('Service Worker activating...');
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.map((cacheName) => {
          if (cacheName !== CACHE_NAME && cacheName !== RUNTIME_CACHE) {
            console.log('Deleting old cache:', cacheName);
            return caches.delete(cacheName);
          }
        })
      );
    }).then(() => {
      console.log('Service Worker activated');
      return self.clients.claim();
    })
  );
});

// Fetch Event - Serve cached content when offline
self.addEventListener('fetch', (event) => {
  // Skip non-GET requests
  if (event.request.method !== 'GET') {
    return;
  }

  // Skip Chrome extensions
  if (event.request.url.startsWith('chrome-extension://')) {
    return;
  }

  event.respondWith(
    caches.match(event.request)
      .then((cachedResponse) => {
        // Return cached version if available
        if (cachedResponse) {
          // Fetch updated version in background
          fetchAndCache(event.request);
          return cachedResponse;
        }

        // Try to fetch from network
        return fetch(event.request)
          .then((response) => {
            // Don't cache non-successful responses
            if (!response || response.status !== 200 || response.type !== 'basic') {
              return response;
            }

            // Cache the response
            if (shouldCache(event.request.url)) {
              cacheResponse(event.request, response.clone());
            }

            return response;
          })
          .catch(() => {
            // If network fails, try to serve offline page for navigation requests
            if (event.request.mode === 'navigate') {
              return caches.match('/offline.html');
            }
            
            // For images, return placeholder
            if (event.request.destination === 'image') {
              return new Response(
                '<svg width="200" height="300" xmlns="http://www.w3.org/2000/svg"><rect width="100%" height="100%" fill="#f3f4f6"/><text x="50%" y="50%" dominant-baseline="middle" text-anchor="middle" fill="#9ca3af">Image Offline</text></svg>',
                { headers: { 'Content-Type': 'image/svg+xml' } }
              );
            }
          });
      })
  );
});

// Helper function to fetch and cache
function fetchAndCache(request) {
  return fetch(request)
    .then((response) => {
      if (response && response.status === 200 && shouldCache(request.url)) {
        cacheResponse(request, response.clone());
      }
      return response;
    })
    .catch(() => {
      // Ignore errors for background fetches
    });
}

// Helper function to cache responses
function cacheResponse(request, response) {
  caches.open(RUNTIME_CACHE)
    .then((cache) => {
      cache.put(request, response);
    });
}

// Helper function to determine if URL should be cached
function shouldCache(url) {
  // Cache movie pages, images, and API responses
  return url.includes('/movie/') ||
         url.includes('/category/') ||
         url.includes('/search/') ||
         url.includes('/static/') ||
         url.includes('cdnjs.cloudflare.com') ||
         url.includes('googleapis.com') ||
         url.endsWith('.jpg') ||
         url.endsWith('.png') ||
         url.endsWith('.webp') ||
         url.endsWith('.css') ||
         url.endsWith('.js');
}

// Background sync for offline actions
self.addEventListener('sync', (event) => {
  console.log('Background sync triggered:', event.tag);
  
  if (event.tag === 'watchlist-sync') {
    event.waitUntil(syncWatchlist());
  } else if (event.tag === 'like-sync') {
    event.waitUntil(syncLikes());
  }
});

// Sync watchlist when back online
async function syncWatchlist() {
  try {
    // Get pending watchlist actions from IndexedDB
    const pendingActions = await getPendingWatchlistActions();
    
    for (const action of pendingActions) {
      try {
        const response = await fetch(`/movie/${action.movieId}/watchlist/`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': action.csrfToken
          },
          body: JSON.stringify({ action: action.type })
        });
        
        if (response.ok) {
          // Remove from pending actions
          await removePendingWatchlistAction(action.id);
        }
      } catch (error) {
        console.error('Failed to sync watchlist action:', error);
      }
    }
  } catch (error) {
    console.error('Watchlist sync failed:', error);
  }
}

// Sync likes when back online
async function syncLikes() {
  try {
    const pendingActions = await getPendingLikeActions();
    
    for (const action of pendingActions) {
      try {
        const response = await fetch(`/movie/${action.movieId}/like/`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': action.csrfToken
          },
          body: JSON.stringify({ action: action.type })
        });
        
        if (response.ok) {
          await removePendingLikeAction(action.id);
        }
      } catch (error) {
        console.error('Failed to sync like action:', error);
      }
    }
  } catch (error) {
    console.error('Like sync failed:', error);
  }
}

// Push notification handler
self.addEventListener('push', (event) => {
  console.log('Push notification received');
  
  let notificationData = {
    title: 'Watch2D',
    body: 'New movies available!',
    icon: 'https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEgGDg63ESTUKkQx6xcxK4dBd8LDkHo5VjiLkh1drq5WGGSG1dLVGQdwY7eXuVQ6Rxtz2mVSkcVvK7f7pFk5_4UVQc8uuX5HI_2J5IUZxR7uhvdmjxb-LEBmqR7zDjqiwjJVSmzv1fKtAt6nHr0EiDAMNPTNMq1yUnkdcMsA_9Z4Dasfc8bxJ0pnFLwafJk/s320/logo%20(3).png',
    badge: 'https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEgGDg63ESTUKkQx6xcxK4dBd8LDkHo5VjiLkh1drq5WGGSG1dLVGQdwY7eXuVQ6Rxtz2mVSkcVvK7f7pFk5_4UVQc8uuX5HI_2J5IUZxR7uhvdmjxb-LEBmqR7zDjqiwjJVSmzv1fKtAt6nHr0EiDAMNPTNMq1yUnkdcMsA_9Z4Dasfc8bxJ0pnFLwafJk/s320/logo%20(3).png',
    tag: 'Watch2D-notification'
  };
  
  if (event.data) {
    try {
      notificationData = { ...notificationData, ...event.data.json() };
    } catch (error) {
      console.error('Error parsing push data:', error);
    }
  }
  
  event.waitUntil(
    self.registration.showNotification(notificationData.title, {
      body: notificationData.body,
      icon: notificationData.icon,
      badge: notificationData.badge,
      tag: notificationData.tag,
      data: notificationData.data,
      actions: [
        {
          action: 'view',
          title: 'View Movie',
          icon: 'https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEgGDg63ESTUKkQx6xcxK4dBd8LDkHo5VjiLkh1drq5WGGSG1dLVGQdwY7eXuVQ6Rxtz2mVSkcVvK7f7pFk5_4UVQc8uuX5HI_2J5IUZxR7uhvdmjxb-LEBmqR7zDjqiwjJVSmzv1fKtAt6nHr0EiDAMNPTNMq1yUnkdcMsA_9Z4Dasfc8bxJ0pnFLwafJk/s320/logo%20(3).png'
        },
        {
          action: 'close',
          title: 'Close',
          icon: 'https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEgGDg63ESTUKkQx6xcxK4dBd8LDkHo5VjiLkh1drq5WGGSG1dLVGQdwY7eXuVQ6Rxtz2mVSkcVvK7f7pFk5_4UVQc8uuX5HI_2J5IUZxR7uhvdmjxb-LEBmqR7zDjqiwjJVSmzv1fKtAt6nHr0EiDAMNPTNMq1yUnkdcMsA_9Z4Dasfc8bxJ0pnFLwafJk/s320/logo%20(3).png'
        }
      ]
    })
  );
});

// Notification click handler
self.addEventListener('notificationclick', (event) => {
  console.log('Notification clicked:', event);
  
  event.notification.close();
  
  if (event.action === 'view' && event.notification.data?.url) {
    event.waitUntil(
      clients.openWindow(event.notification.data.url)
    );
  } else if (event.action !== 'close') {
    event.waitUntil(
      clients.openWindow('/')
    );
  }
});

// Helper functions for IndexedDB operations
async function getPendingWatchlistActions() {
  // Implement IndexedDB operations for offline sync
  return [];
}

async function removePendingWatchlistAction(id) {
  // Implement IndexedDB removal
}

async function getPendingLikeActions() {
  // Implement IndexedDB operations for offline sync
  return [];
}

async function removePendingLikeAction(id) {
  // Implement IndexedDB removal
}

// Handle skip waiting message
self.addEventListener('message', (event) => {
  if (event.data?.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});