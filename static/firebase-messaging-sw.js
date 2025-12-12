importScripts('https://www.gstatic.com/firebasejs/9.23.0/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/9.23.0/firebase-messaging-compat.js');

const firebaseConfig = {
    apiKey: "AIzaSyBkzEHM9fXRoCYSgEW-hk_lN7sL_nSLDfU",
    authDomain: "cst-institute-app.firebaseapp.com",
    projectId: "cst-institute-app",
    storageBucket: "cst-institute-app.firebasestorage.app",
    messagingSenderId: "485166460200",
    appId: "1:485166460200:web:4eae20e2010ab92baeb41d"
};

firebase.initializeApp(firebaseConfig);
const messaging = firebase.messaging();

// 1. Handle Background Notifications
messaging.onBackgroundMessage((payload) => {
    console.log('[SW] Background Message:', payload);
    
    // Construct the FULL URL to ensure mobile finds it
    const fullUrl = self.location.origin + '/student';

    const notificationTitle = payload.notification.title;
    const notificationOptions = {
        body: payload.notification.body,
        icon: '/static/icons/icon-192x192.png',
        badge: '/static/icons/badge.png',
        data: { url: fullUrl }, // Store the Full URL
        tag: 'cst-notification' // Replaces old notifications with new ones
    };

    self.registration.showNotification(notificationTitle, notificationOptions);
});

// 2. Handle CLICK (The Fix)
self.addEventListener('notificationclick', function(event) {
    console.log('[SW] Notification Clicked');
    event.notification.close();

    // Get the Full URL we stored above
    const targetUrl = event.notification.data.url || (self.location.origin + '/student');

    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(clientList) {
            // A. If app is open, refresh it
            for (let i = 0; i < clientList.length; i++) {
                const client = clientList[i];
                if (client.url.includes('/student') && 'focus' in client) {
                    return client.focus().then(c => c.navigate(targetUrl));
                }
            }
            // B. If app is closed, open it
            if (clients.openWindow) {
                return clients.openWindow(targetUrl);
            }
        })
    );
});