importScripts('https://www.gstatic.com/firebasejs/9.23.0/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/9.23.0/firebase-messaging-compat.js');

// 1. Firebase Config (Must match your project)
const firebaseConfig = {
    apiKey: "AIzaSyBkzEHM9fXRoCYSgEW-hk_lN7sL_nSLDfU",
    authDomain: "cst-institute-app.firebaseapp.com",
    projectId: "cst-institute-app",
    storageBucket: "cst-institute-app.firebasestorage.app",
    messagingSenderId: "485166460200",
    appId: "1:485166460200:web:4eae20e2010ab92baeb41d"
};

// 2. Initialize
firebase.initializeApp(firebaseConfig);
const messaging = firebase.messaging();

// 3. Handle Background Notifications (Display)
messaging.onBackgroundMessage((payload) => {
    console.log('[Service Worker] Background Message:', payload);
    
    const notificationTitle = payload.notification.title;
    const notificationOptions = {
        body: payload.notification.body,
        icon: '/static/icons/icon-192x192.png', // Ensure this icon exists
        badge: '/static/icons/badge.png',
        data: { url: '/student' } // <--- IMPORTANT: We store the URL here
    };

    self.registration.showNotification(notificationTitle, notificationOptions);
});

// 4. Handle Notification CLICK (The Fix)
self.addEventListener('notificationclick', function(event) {
    console.log('[Service Worker] Notification Clicked');
    
    // A. Close the notification immediately
    event.notification.close();

    // B. Determine where to go (Default to /student if no URL provided)
    const targetUrl = (event.notification.data && event.notification.data.url) ? event.notification.data.url : '/student';

    // C. Focus existing tab or open new one
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(clientList) {
            // 1. If the app is already open, focus it and refresh
            for (let i = 0; i < clientList.length; i++) {
                const client = clientList[i];
                if (client.url.includes(targetUrl) && 'focus' in client) {
                    return client.focus().then(c => c.navigate(targetUrl)); 
                }
            }
            // 2. If app is closed, open a new window/app instance
            if (clients.openWindow) {
                return clients.openWindow(targetUrl);
            }
        })
    );
});