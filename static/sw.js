const CACHE_NAME = 'as-academy-v2'; // تم تغيير الإصدار لفرض تحديث الكاش لدى المستخدمين

// الملفات الأساسية التي نريد تخزينها لتعمل بدون إنترنت
const ASSETS_TO_CACHE = [
    '/',
    '/static/manifest.json',
    'https://i.postimg.cc/mDFqRtsJ/logo.png'
];

self.addEventListener('install', (event) => {
    // تخزين الملفات بمجرد تثبيت الـ Service Worker
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then((cache) => {
                console.log('جاري تخزين الملفات الأساسية...');
                // نستخدم catch لتجنب توقف التثبيت إذا فشل تحميل أحد الروابط
                return cache.addAll(ASSETS_TO_CACHE).catch(err => console.log('خطأ في تخزين بعض الملفات:', err));
            })
            .then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', (event) => {
    // تنظيف أي كاش قديم لا يطابق الإصدار الحالي
    event.waitUntil(
        caches.keys().then((cacheNames) => {
            return Promise.all(
                cacheNames.map((cacheName) => {
                    if (cacheName !== CACHE_NAME) {
                        console.log('جاري حذف الكاش القديم:', cacheName);
                        return caches.delete(cacheName);
                    }
                })
            );
        }).then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    // استراتيجية: الإنترنت أولاً، ثم الكاش (Network first, falling back to cache)
    event.respondWith(
        fetch(event.request).catch(() => caches.match(event.request))
    );
});
