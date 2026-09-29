# Nuurani DM — Download Manager ala IDM untuk macOS

## Cara kerja (meniru IDM)
| Teknik IDM | Implementasi Nuurani DM |
|---|---|
| Dynamic file segmentation (aturan bagi dua) | Mulai dari 1 segmen; tiap koneksi baru/bebas membelah segmen aktif **terbesar** jadi dua |
| Connection reuse | Koneksi HTTP keep-alive dipakai ulang untuk segmen berikutnya (tanpa handshake TLS baru) |
| Assembling file | **Tidak perlu** — tiap koneksi menulis langsung ke offset-nya (`pwrite`) di satu file `.part` |
| Resume aman (mati listrik) | Posisi segmen disimpan ke `.nuurani` tiap 2 detik |
| Browser integration | Ekstensi Chrome/Edge/Brave/Arc + server lokal `127.0.0.1:52789` |
| Kategori, antrean, speed limiter | Subfolder otomatis, maks unduhan bersamaan, token bucket global |

Fitur khusus Mac: jendela & menu native Cocoa, Retina, Dark Mode, ikon menu bar, notifikasi macOS,
"Tampilkan di Finder", `caffeinate` agar Mac tidak tidur saat mengunduh, pintasan ⌘N / ⌘R / ⌘P / ⌘, / ⌘Q.

## Instalasi (sekali saja)
1. Pastikan Python 3.10+ ada: `python3 --version` (kalau belum: `brew install python` atau dari python.org).
2. Build aplikasi:
   ```bash
   cd NuuraniDM
   ./build_mac.sh
   ```
3. Hasil: `dist/Nuurani DM.app` dan `dist/NuuraniDM.dmg` → seret ke **Applications**.

Mode cepat tanpa build: `./run_dev.sh`

## Ekstensi browser
Chrome → `chrome://extensions` → aktifkan **Developer mode** → **Load unpacked** → pilih folder `browser_extension`.
- Klik kanan tautan → **Unduh dengan Nuurani DM**
- Unduhan ≥ 1 MB otomatis ditangkap (bisa dimatikan di popup ekstensi)

## Cara pakai
- **Tambah URL** (⌘N) — bisa banyak URL sekaligus, satu per baris
- Salin tautan file (zip, mp4, pdf, dmg, …) → dialog unduh muncul otomatis (pantau clipboard)
- Seret tautan ke jendela aplikasi
- Klik dua kali: buka file (selesai) / jeda / lanjutkan
- Tutup jendela = tetap jalan di menu bar; keluar penuh dengan ⌘Q

Baris perintah: `python3 -m nuurani_dm.cli URL -c 16 -o ~/Downloads`

## Lokasi data
`~/Library/Application Support/Nuurani DM/` (settings.json, downloads.json)

## Uji engine
`python3 tests/test_engine.py` · `python3 tests/test_manager.py`
