// PWA Registration and Management for Watch2D
class Watch2DPWA {
  constructor() {
    this.deferredPrompt = null;
    this.isInstalled = false;
    this.swRegistration = null;
    this.init();
  }

  async init() {
    // Check if PWA is already installed
    this.checkInstallStatus();
    
    // Register service worker
    await this.registerServiceWorker();
    
    // Setup install prompt handling
    this.setupInstallPrompt();
    
    // Setup UI elements
    this.setupUI();
    
    // Handle app updates
    this.handleUpdates();
    
    // Setup offline/online detection
    this.setupNetworkStatus();
  }

  // Check if PWA is installed
  checkInstallStatus() {
    // Check if running in standalone mode
    this.isInstalled = window.matchMedia('(display-mode: standalone)').matches || 
                      window.navigator.standalone === true;
    
    if (this.isInstalled) {
      console.log('Watch2D PWA is installed');
      this.hideInstallPrompts();
    }
  }

  // Register service worker
  async registerServiceWorker() {
    if ('serviceWorker' in navigator) {
      try {
        this.swRegistration = await navigator.serviceWorker.register('/static/js/sw.js', {
          scope: '/'
        });
        
        console.log('Service Worker registered successfully:', this.swRegistration);
        
        // Check for updates
        this.swRegistration.addEventListener('updatefound', () => {
          this.handleServiceWorkerUpdate();
        });
        
      } catch (error) {
        console.error('Service Worker registration failed:', error);
      }
    } else {
      console.warn('Service Workers not supported in this browser');
    }
  }

  // Handle service worker updates
  handleServiceWorkerUpdate() {
    const newWorker = this.swRegistration.installing;
    
    newWorker.addEventListener('statechange', () => {
      if (newWorker.state === 'installed' && navigator.serviceWorker.controller) {
        // New content is available
        this.showUpdateAvailable();
      }
    });
  }

  // Setup install prompt handling
  setupInstallPrompt() {
    window.addEventListener('beforeinstallprompt', (event) => {
      console.log('PWA install prompt available');
      event.preventDefault();
      this.deferredPrompt = event;
      this.showInstallPrompt();
    });

    // Handle successful installation
    window.addEventListener('appinstalled', () => {
      console.log('Watch2D PWA installed successfully');
      this.isInstalled = true;
      this.hideInstallPrompts();
      this.showInstallSuccess();
      
      // Track installation
      this.trackEvent('pwa_installed');
    });
  }

  // Show install prompt UI
  showInstallPrompt() {
    // Top install bar disabled — it covered the search/nav. The floating
    // bottom banner (#pwa-bar in base.html) is now the only install prompt.
    return;
  }

  // Create install banner
  createInstallBanner() {
    const banner = document.createElement('div');
    banner.id = 'pwa-install-banner';
    banner.className = 'pwa-install-banner';
    banner.innerHTML = `
      <div class="pwa-banner-content">
        <div class="pwa-banner-icon">
          <img src="https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEgGDg63ESTUKkQx6xcxK4dBd8LDkHo5VjiLkh1drq5WGGSG1dLVGQdwY7eXuVQ6Rxtz2mVSkcVvK7f7pFk5_4UVQc8uuX5HI_2J5IUZxR7uhvdmjxb-LEBmqR7zDjqiwjJVSmzv1fKtAt6nHr0EiDAMNPTNMq1yUnkdcMsA_9Z4Dasfc8bxJ0pnFLwafJk/s320/logo%20(3).png" alt="Watch2D" />
        </div>
        <div class="pwa-banner-text">
          <h3>Install Watch2D App</h3>
          <p>Get the full app experience with offline access</p>
        </div>
        <div class="pwa-banner-actions">
          <button id="pwa-install-btn" class="pwa-btn-install">Install</button>
          <button id="pwa-dismiss-btn" class="pwa-btn-dismiss">×</button>
        </div>
      </div>
    `;

    // Add event listeners
    banner.querySelector('#pwa-install-btn').addEventListener('click', () => {
      this.installApp();
    });

    banner.querySelector('#pwa-dismiss-btn').addEventListener('click', () => {
      this.dismissInstallPrompt();
    });

    return banner;
  }

  // Install the PWA
  async installApp() {
    if (!this.deferredPrompt) return;

    try {
      const result = await this.deferredPrompt.prompt();
      console.log('PWA install prompt result:', result);
      
      if (result.outcome === 'accepted') {
        this.trackEvent('pwa_install_accepted');
      } else {
        this.trackEvent('pwa_install_dismissed');
      }
      
      this.deferredPrompt = null;
      
    } catch (error) {
      console.error('PWA installation failed:', error);
      this.trackEvent('pwa_install_error');
    }
  }

  // Dismiss install prompt
  dismissInstallPrompt() {
    const banner = document.getElementById('pwa-install-banner');
    if (banner) {
      banner.classList.remove('show');
      setTimeout(() => {
        banner.style.display = 'none';
      }, 300);
    }
    
    this.trackEvent('pwa_install_banner_dismissed');
    
    // Don't show again for 24 hours
    localStorage.setItem('pwa-install-dismissed', Date.now().toString());
  }

  // Hide install prompts
  hideInstallPrompts() {
    const banner = document.getElementById('pwa-install-banner');
    if (banner) {
      banner.style.display = 'none';
    }
  }

  // Show install success message
  showInstallSuccess() {
    this.showToast('Watch2D app installed successfully! 🎉', 'success');
  }

  // Show app update available
  showUpdateAvailable() {
    const updateBanner = this.createUpdateBanner();
    document.body.appendChild(updateBanner);
    
    setTimeout(() => updateBanner.classList.add('show'), 100);
  }

  // Create update banner
  createUpdateBanner() {
    const banner = document.createElement('div');
    banner.className = 'pwa-update-banner';
    banner.innerHTML = `
      <div class="pwa-banner-content">
        <div class="pwa-banner-text">
          <h3>Update Available</h3>
          <p>A new version of Watch2D is ready</p>
        </div>
        <div class="pwa-banner-actions">
          <button id="pwa-update-btn" class="pwa-btn-update">Update Now</button>
          <button id="pwa-update-dismiss-btn" class="pwa-btn-dismiss">Later</button>
        </div>
      </div>
    `;

    banner.querySelector('#pwa-update-btn').addEventListener('click', () => {
      this.applyUpdate();
    });

    banner.querySelector('#pwa-update-dismiss-btn').addEventListener('click', () => {
      banner.remove();
    });

    return banner;
  }

  // Apply service worker update
  applyUpdate() {
    if (this.swRegistration?.waiting) {
      this.swRegistration.waiting.postMessage({ type: 'SKIP_WAITING' });
      
      navigator.serviceWorker.addEventListener('controllerchange', () => {
        window.location.reload();
      });
    }
  }

  // Setup network status detection
  setupNetworkStatus() {
    const updateNetworkStatus = () => {
      const isOnline = navigator.onLine;
      document.body.classList.toggle('offline', !isOnline);
      
      if (!isOnline) {
        this.showToast('You are offline. Some features may be limited.', 'warning');
      }
    };

    window.addEventListener('online', () => {
      this.showToast('Connection restored! 🌐', 'success');
      this.syncOfflineActions();
    });

    window.addEventListener('offline', updateNetworkStatus);
    
    // Initial status
    updateNetworkStatus();
  }

  // Sync offline actions when back online
  async syncOfflineActions() {
    if ('serviceWorker' in navigator && this.swRegistration) {
      try {
        await this.swRegistration.sync.register('watchlist-sync');
        await this.swRegistration.sync.register('like-sync');
      } catch (error) {
        console.error('Background sync registration failed:', error);
      }
    }
  }

  // Setup UI enhancements
  setupUI() {
    // Add PWA-specific styles
    this.addPWAStyles();
    
    // Enhance form interactions for offline use
    this.enhanceOfflineInteractions();
    
    // Add pull-to-refresh for mobile
    if (this.isInstalled && this.isMobile()) {
      this.setupPullToRefresh();
    }
  }

  // Add PWA-specific styles
  addPWAStyles() {
    const styles = `
      <style>
        .pwa-install-banner, .pwa-update-banner {
          position: fixed;
          top: 0;
          left: 0;
          right: 0;
          background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
          color: white;
          padding: 16px;
          transform: translateY(-100%);
          transition: transform 0.3s ease;
          z-index: 1000;
          box-shadow: 0 4px 12px rgba(0,0,0,0.2);
        }
        
        .pwa-install-banner.show, .pwa-update-banner.show {
          transform: translateY(0);
        }
        
        .pwa-banner-content {
          display: flex;
          align-items: center;
          max-width: 1200px;
          margin: 0 auto;
          gap: 16px;
        }
        
        .pwa-banner-icon img {
          width: 48px;
          height: 48px;
          border-radius: 12px;
        }
        
        .pwa-banner-text h3 {
          margin: 0;
          font-size: 16px;
          font-weight: 600;
        }
        
        .pwa-banner-text p {
          margin: 4px 0 0 0;
          font-size: 14px;
          opacity: 0.9;
        }
        
        .pwa-banner-actions {
          display: flex;
          gap: 12px;
          margin-left: auto;
        }
        
        .pwa-btn-install, .pwa-btn-update {
          background: rgba(255,255,255,0.2);
          border: 1px solid rgba(255,255,255,0.3);
          color: white;
          padding: 8px 16px;
          border-radius: 8px;
          font-size: 14px;
          cursor: pointer;
          transition: background 0.2s;
        }
        
        .pwa-btn-install:hover, .pwa-btn-update:hover {
          background: rgba(255,255,255,0.3);
        }
        
        .pwa-btn-dismiss {
          background: transparent;
          border: none;
          color: white;
          font-size: 20px;
          cursor: pointer;
          padding: 4px 8px;
          border-radius: 4px;
          transition: background 0.2s;
        }
        
        .pwa-btn-dismiss:hover {
          background: rgba(255,255,255,0.2);
        }
        
        .offline {
          filter: grayscale(20%);
        }
        
        .offline::after {
          content: 'Offline Mode';
          position: fixed;
          bottom: 20px;
          left: 50%;
          transform: translateX(-50%);
          background: #f59e0b;
          color: white;
          padding: 8px 16px;
          border-radius: 20px;
          font-size: 12px;
          z-index: 1000;
          animation: pulse 2s infinite;
        }
        
        .pwa-toast {
          position: fixed;
          bottom: 20px;
          left: 50%;
          transform: translateX(-50%) translateY(100px);
          background: #1f2937;
          color: white;
          padding: 12px 20px;
          border-radius: 8px;
          font-size: 14px;
          z-index: 1001;
          transition: transform 0.3s ease;
          box-shadow: 0 4px 12px rgba(0,0,0,0.3);
        }
        
        .pwa-toast.show {
          transform: translateX(-50%) translateY(0);
        }
        
        .pwa-toast.success {
          background: #10b981;
        }
        
        .pwa-toast.warning {
          background: #f59e0b;
        }
        
        .pwa-toast.error {
          background: #ef4444;
        }
        
        @keyframes pulse {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.5; }
        }
        
        @media (max-width: 768px) {
          .pwa-banner-content {
            flex-direction: column;
            text-align: center;
            gap: 12px;
          }
          
          .pwa-banner-actions {
            margin-left: 0;
          }
        }
      </style>
    `;
    
    document.head.insertAdjacentHTML('beforeend', styles);
  }

  // Enhance offline interactions
  enhanceOfflineInteractions() {
    // Store offline actions for sync later
    document.addEventListener('click', (event) => {
      if (!navigator.onLine) {
        const target = event.target.closest('[data-offline-action]');
        if (target) {
          event.preventDefault();
          this.storeOfflineAction(target);
          this.showToast('Action saved. Will sync when online.', 'warning');
        }
      }
    });
  }

  // Store offline action for later sync
  storeOfflineAction(element) {
    const action = {
      id: Date.now(),
      type: element.dataset.offlineAction,
      url: element.href || element.dataset.url,
      method: element.dataset.method || 'POST',
      data: element.dataset.actionData || '{}',
      timestamp: Date.now()
    };

    const offlineActions = JSON.parse(localStorage.getItem('pwa-offline-actions') || '[]');
    offlineActions.push(action);
    localStorage.setItem('pwa-offline-actions', JSON.stringify(offlineActions));
  }

  // Setup pull-to-refresh for mobile PWA
  setupPullToRefresh() {
    let startY = 0;
    let currentY = 0;
    let pulling = false;

    document.addEventListener('touchstart', (event) => {
      if (window.scrollY === 0) {
        startY = event.touches[0].clientY;
      }
    });

    document.addEventListener('touchmove', (event) => {
      if (window.scrollY === 0 && startY) {
        currentY = event.touches[0].clientY;
        if (currentY > startY + 50) {
          pulling = true;
          event.preventDefault();
          this.showPullToRefreshIndicator();
        }
      }
    });

    document.addEventListener('touchend', () => {
      if (pulling) {
        pulling = false;
        this.hidePullToRefreshIndicator();
        this.refreshContent();
      }
      startY = 0;
      currentY = 0;
    });
  }

  // Show pull-to-refresh indicator
  showPullToRefreshIndicator() {
    let indicator = document.getElementById('pull-refresh-indicator');
    if (!indicator) {
      indicator = document.createElement('div');
      indicator.id = 'pull-refresh-indicator';
      indicator.innerHTML = '↓ Pull to refresh';
      indicator.style.cssText = `
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        background: #0ea5e9;
        color: white;
        text-align: center;
        padding: 10px;
        z-index: 1002;
        font-size: 14px;
      `;
      document.body.appendChild(indicator);
    }
  }

  // Hide pull-to-refresh indicator
  hidePullToRefreshIndicator() {
    const indicator = document.getElementById('pull-refresh-indicator');
    if (indicator) {
      indicator.remove();
    }
  }

  // Refresh content
  async refreshContent() {
    try {
      // Force update service worker cache
      if (this.swRegistration) {
        await this.swRegistration.update();
      }
      
      // Reload current page
      window.location.reload();
      
    } catch (error) {
      console.error('Content refresh failed:', error);
      this.showToast('Refresh failed', 'error');
    }
  }

  // Check if device is mobile
  isMobile() {
    return /Android|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);
  }

  // Show toast notification
  showToast(message, type = 'info', duration = 3000) {
    const toast = document.createElement('div');
    toast.className = `pwa-toast ${type}`;
    toast.textContent = message;
    
    document.body.appendChild(toast);
    
    // Show toast
    setTimeout(() => toast.classList.add('show'), 100);
    
    // Hide and remove toast
    setTimeout(() => {
      toast.classList.remove('show');
      setTimeout(() => toast.remove(), 300);
    }, duration);
  }

  // Track events for analytics
  trackEvent(eventName, data = {}) {
    try {
      // Google Analytics 4
      if (typeof gtag !== 'undefined') {
        gtag('event', eventName, {
          event_category: 'PWA',
          ...data
        });
      }
      
      // Custom analytics
      console.log(`PWA Event: ${eventName}`, data);
      
    } catch (error) {
      console.error('Event tracking failed:', error);
    }
  }

  // Handle app updates
  handleUpdates() {
    // Listen for app updates
    if (this.swRegistration) {
      this.swRegistration.addEventListener('updatefound', () => {
        console.log('New app version available');
        this.showUpdateAvailable();
      });
    }
  }

  // Get app info
  getAppInfo() {
    return {
      isInstalled: this.isInstalled,
      isOnline: navigator.onLine,
      serviceWorkerReady: !!this.swRegistration,
      supportsNotifications: 'Notification' in window,
      supportsPushMessages: 'PushManager' in window,
      supportsBackgroundSync: 'serviceWorker' in navigator && 'sync' in window.ServiceWorkerRegistration.prototype
    };
  }

  // Request notification permission
  async requestNotificationPermission() {
    if ('Notification' in window && Notification.permission === 'default') {
      try {
        const permission = await Notification.requestPermission();
        this.trackEvent('notification_permission', { result: permission });
        return permission === 'granted';
      } catch (error) {
        console.error('Notification permission request failed:', error);
        return false;
      }
    }
    return Notification.permission === 'granted';
  }

  // Subscribe to push notifications
  async subscribeToPush() {
    if (!this.swRegistration || !('PushManager' in window)) {
      return null;
    }

    try {
      const subscription = await this.swRegistration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: this.urlBase64ToUint8Array('your-vapid-public-key-here')
      });

      // Send subscription to server
      await fetch('/api/push-subscribe/', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken': this.getCSRFToken()
        },
        body: JSON.stringify(subscription)
      });

      this.trackEvent('push_subscription_success');
      return subscription;

    } catch (error) {
      console.error('Push subscription failed:', error);
      this.trackEvent('push_subscription_failed');
      return null;
    }
  }

  // Helper to convert VAPID key
  urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - base64String.length % 4) % 4);
    const base64 = (base64String + padding)
      .replace(/\-/g, '+')
      .replace(/_/g, '/');

    const rawData = window.atob(base64);
    const outputArray = new Uint8Array(rawData.length);

    for (let i = 0; i < rawData.length; ++i) {
      outputArray[i] = rawData.charCodeAt(i);
    }
    return outputArray;
  }

  // Get CSRF token
  getCSRFToken() {
    return document.querySelector('[name=csrfmiddlewaretoken]')?.value ||
           document.querySelector('meta[name=csrf-token]')?.getAttribute('content') ||
           '';
  }

  // Cleanup
  destroy() {
    // Remove event listeners and cleanup
    if (this.swRegistration) {
      this.swRegistration.removeEventListener('updatefound', this.handleServiceWorkerUpdate);
    }
  }
}

// Initialize PWA when DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
  // Only initialize PWA in modern browsers
  if ('serviceWorker' in navigator && 'Promise' in window) {
    window.Watch2DPWA = new Watch2DPWA();
    console.log('Watch2D PWA initialized');
  } else {
    console.warn('PWA features not supported in this browser');
  }
});

// Export for global access
window.Watch2DPWA = Watch2DPWA;