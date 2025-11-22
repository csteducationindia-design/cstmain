importScripts('https://www.gstatic.com/firebasejs/9.0.0/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/9.0.0/firebase-messaging-compat.js');

// PASTE YOUR "const firebaseConfig = { ... }" CODE HERE (from Step 1)
// It should look like this:
const firebaseConfig = {
  apiKey: "AIzaSyBk...",
  authDomain: "cst-institute-app.firebaseapp.com",
  projectId: "cst-institute-app",
  storageBucket: "cst-institute-app.firebasestorage.app",
  messagingSenderId: "485166460200",
  appId: "1:485166460200:web:..."
};

firebase.initializeApp(firebaseConfig);
const messaging = firebase.messaging();

messaging.onBackgroundMessage(function(payload) {
  console.log('Received background message ', payload);
  const notificationTitle = payload.notification.title;
  const notificationOptions = {
    body: payload.notification.body,
    icon: '/static/logo.png' // Make sure you have a logo.png in static folder
  };

  self.registration.showNotification(notificationTitle, notificationOptions);
});