// Service Worker لـ PWA - دعم iPhone

const CACHE_NAME = 'asa-v1';
const ASSETS = [
  '/',
  '/static/manifest.json',
  'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css',
  'https://fonts.googleapis.com/css2?family=Tajawal:wght@400;500;700;800;900&family=Outfit:wght@400;700;800;900&display=swap'
];

// تثبيت الـ Service Worker
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => {
        console.log('✅ تم فتح الكاش');
        return cache.addAll(ASSETS);
      })
      .catch(err => console.error('❌ خطأ في التثبيت:', err))
  );
  self.skipWaiting();
});

// تفعيل الـ Service Worker
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => {
      return Promise.all(
        keys.filter(key => key !== CACHE_NAME)
          .map(key => caches.delete(key))
      );
    })
  );
  self.clients.claim();
});

// اعتراض الطلبات
self.addEventListener('fetch', event => {
  // تجاهل طلبات الـ API (لا نخزنها في الكاش)
  if (event.request.url.includes('/api/') || 
      event.request.url.includes('/admin/') ||
      event.request.url.includes('/student/') ||
      event.request.url.includes('/webauthn/')) {
    event.respondWith(fetch(event.request));
    return;
  }

  // تجاهل طلبات البصمة (WebAuthn)
  if (event.request.url.includes('/webauthn/')) {
    event.respondWith(fetch(event.request));
    return;
  }

  // استراتيجية: Cache First ثم Network
  event.respondWith(
    caches.match(event.request)
      .then(cachedResponse => {
        if (cachedResponse) {
          return cachedResponse;
        }
        return fetch(event.request)
          .then(response => {
            // لا نخزن الصفحات الديناميكية
            if (event.request.url.includes('/planner') || 
                event.request.url.includes('?')) {
              return response;
            }
            // تخزين الملفات الثابتة
            const clone = response.clone();
            caches.open(CACHE_NAME).then(cache => {
              cache.put(event.request, clone);
            });
            return response;
          })
          .catch(() => {
            // عرض صفحة خطأ عند فقدان الاتصال (اختياري)
            return caches.match('/offline');
          });
      })
  );
});
