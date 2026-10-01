// The one place the dashboard's "Tracking pop-up" switch can get Chrome's answer from: an
// extension page, where this click is the user gesture permissions.request() requires.
// Background registers pill.js on permissions.onAdded (pill-everywhere.js); this window
// only asks and closes.
document.getElementById("allow").addEventListener("click", () => {
  chrome.permissions.request({ origins: ["<all_urls>"] })
    .then((granted) => { if (granted) window.close(); })
    .catch(() => {});
});
