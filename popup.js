const st = document.getElementById("st"), auto = document.getElementById("auto");
chrome.runtime.sendMessage({ type: "ping" }, r => {
  st.innerHTML = r?.ok ? '<span class="ok">● Terhubung ke aplikasi</span>' : '<span class="no">● Aplikasi Nuurani DM belum dibuka</span>';
});
chrome.storage.local.get({ autoCapture: true }, v => auto.checked = v.autoCapture);
auto.onchange = () => chrome.storage.local.set({ autoCapture: auto.checked });
document.getElementById("go").onclick = () => {
  const url = document.getElementById("url").value.trim();
  if (!/^https?:\/\//.test(url)) return;
  chrome.runtime.sendMessage({ type: "send", url }, r => st.textContent = r?.ok ? "✅ Terkirim" : "❌ Gagal mengirim");
};
