const API = "http://127.0.0.1:52789";
const MIN_SIZE = 1024 * 1024; // tangkap otomatis hanya file >= 1 MB
const SKIP = /^(blob:|data:|filesystem:)/;

async function alive() {
  try { return (await fetch(API + "/ping", { cache: "no-store" })).ok; } catch { return false; }
}
async function cookiesFor(url) {
  try { return (await chrome.cookies.getAll({ url })).map(c => `${c.name}=${c.value}`).join("; "); }
  catch { return ""; }
}
async function send(url, referrer = "", filename = "") {
  const body = { url, referrer, filename, cookies: await cookiesFor(url), userAgent: navigator.userAgent };
  const r = await fetch(API + "/add", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return r.ok;
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({ id: "nuurani-link", title: "Unduh dengan Nuurani DM", contexts: ["link", "video", "audio", "image"] });
  chrome.storage.local.get({ autoCapture: true }, v => chrome.storage.local.set(v));
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const url = info.linkUrl || info.srcUrl;
  if (url) await send(url, tab?.url || "");
});

// Tangkap unduhan browser secara otomatis (seperti IDM)
chrome.downloads.onCreated.addListener(async item => {
  const { autoCapture } = await chrome.storage.local.get({ autoCapture: true });
  const url = item.finalUrl || item.url;
  if (!autoCapture || SKIP.test(url) || (item.totalBytes > 0 && item.totalBytes < MIN_SIZE)) return;
  if (!(await alive())) return; // aplikasi tidak jalan -> biarkan browser mengunduh
  try {
    await chrome.downloads.cancel(item.id);
    await chrome.downloads.erase({ id: item.id });
    const name = (item.filename || "").split(/[\\/]/).pop();
    await send(url, item.referrer || "", name);
  } catch (e) { console.warn("Nuurani DM:", e); }
});

chrome.runtime.onMessage.addListener((msg, _s, reply) => {
  if (msg.type === "ping") alive().then(ok => reply({ ok }));
  if (msg.type === "send") send(msg.url).then(ok => reply({ ok }));
  return true;
});
