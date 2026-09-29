#!/bin/bash
# Build "Nuurani DM.app" + DMG untuk macOS
set -e
cd "$(dirname "$0")"
APP="Nuurani DM"

echo "▶ Menyiapkan virtualenv…"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q --default-timeout=180 --retries 10 -r requirements.txt pyinstaller

echo "▶ Memeriksa ikon…"
mkdir -p assets build
if ! sips -g pixelWidth assets/icon.png >/dev/null 2>&1; then
  echo "  assets/icon.png tidak ada/rusak -> membuat ikon baru"
  QT_QPA_PLATFORM=offscreen python3 - <<'PY'
from PySide6.QtGui import QGuiApplication, QImage, QPainter, QColor, QLinearGradient, QPolygonF, QBrush
from PySide6.QtCore import Qt, QPointF, QRectF
app = QGuiApplication([])
S = 1024
img = QImage(S, S, QImage.Format.Format_ARGB32); img.fill(Qt.GlobalColor.transparent)
p = QPainter(img); p.setRenderHint(QPainter.RenderHint.Antialiasing); p.setPen(Qt.PenStyle.NoPen)
g = QLinearGradient(0, 0, 0, S); g.setColorAt(0, QColor("#1A3B5D")); g.setColorAt(1, QColor("#2C7A7B"))
p.setBrush(QBrush(g)); p.drawRoundedRect(QRectF(100, 100, 824, 824), 185, 185)
p.setBrush(QColor("white")); p.drawRoundedRect(QRectF(452, 250, 120, 310), 30, 30)
p.drawPolygon(QPolygonF([QPointF(330, 520), QPointF(694, 520), QPointF(512, 720)]))
p.setBrush(QColor("#F4C542")); p.drawRoundedRect(QRectF(300, 760, 424, 50), 25, 25)
p.end(); img.save("assets/icon.png")
PY
fi

ICON_ARG=""
rm -rf build/icon.iconset; mkdir -p build/icon.iconset
if sips -g pixelWidth assets/icon.png >/dev/null 2>&1; then
  for s in 16 32 128 256 512; do
    sips -s format png -z $s $s assets/icon.png --out build/icon.iconset/icon_${s}x${s}.png >/dev/null
    sips -s format png -z $((s*2)) $((s*2)) assets/icon.png --out build/icon.iconset/icon_${s}x${s}@2x.png >/dev/null
  done
  if iconutil -c icns build/icon.iconset -o build/NuuraniDM.icns 2>/dev/null; then
    ICON_ARG="--icon build/NuuraniDM.icns"
  else
    echo "  ⚠️ Gagal membuat .icns, lanjut tanpa ikon"
  fi
fi

echo "▶ Build aplikasi…"
pyinstaller --noconfirm --clean --windowed \
  --name "$APP" $ICON_ARG \
  --osx-bundle-identifier id.nuurani.dm \
  --add-data "assets/icon.png:assets" \
  --exclude-module tkinter \
  run_app.py

PLIST="dist/$APP.app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString 1.0.0" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :NSHighResolutionCapable bool true" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :LSMinimumSystemVersion string 11.0" "$PLIST" 2>/dev/null || true
codesign --force --deep --sign - "dist/$APP.app"

echo "▶ Membuat DMG…"
rm -f dist/NuuraniDM.dmg
hdiutil create -volname "$APP" -srcfolder "dist/$APP.app" -ov -format UDZO dist/NuuraniDM.dmg >/dev/null
echo "✅ Selesai: dist/$APP.app  &  dist/NuuraniDM.dmg"
