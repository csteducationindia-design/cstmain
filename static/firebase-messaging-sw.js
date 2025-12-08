importScripts('https://www.gstatic.com/firebasejs/9.23.0/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/9.23.0/firebase-messaging-compat.js');

// --- YOUR REAL CONFIGURATION ---
const firebaseConfig = {
  apiKey: "AIzaSyBkZEHM9fXRoCYSgEW-hk_lN7sL_nSLDfU",
  authDomain: "cst-institute-app.firebaseapp.com",
  projectId: "cst-institute-app",
  storageBucket: "cst-institute-app.firebasestorage.app",
  messagingSenderId: "485166460200",
  appId: "1:485166460200:web:4eae20e2010ab92baeb41d"
};

firebase.initializeApp(firebaseConfig);
const messaging = firebase.messaging();

messaging.onBackgroundMessage(function(payload) {
  console.log('[firebase-messaging-sw.js] Received background message ', payload);
  
  const notificationTitle = payload.notification.title;
  const notificationOptions = {
    body: payload.notification.body,
    icon: '/static/icon.png', // Ensure icon.png exists in static folder
    badge: '/static/icon.png'
  };

  self.registration.showNotification(notificationTitle, notificationOptions);
});