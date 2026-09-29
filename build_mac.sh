#!/bin/bash
# Build "Nuurani DM.app" + DMG untuk macOS (Apple Silicon & Intel — sesuai Mac tempat build)
set -e
cd "$(dirname "$0")"
APP="Nuurani DM"

echo "▶ Menyiapkan virtualenv…"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt pyinstaller

echo "▶ Membuat ikon .icns…"
mkdir -p build/icon.iconset
for s in 16 32 128 256 512; do
  sips -z $s $s assets/icon.png --out build/icon.iconset/icon_${s}x${s}.png >/dev/null
  sips -z $((s*2)) $((s*2)) assets/icon.png --out build/icon.iconset/icon_${s}x${s}@2x.png >/dev/null
done
iconutil -c icns build/icon.iconset -o build/NuuraniDM.icns

echo "▶ Build aplikasi…"
pyinstaller --noconfirm --clean --windowed \
  --name "$APP" \
  --icon build/NuuraniDM.icns \
  --osx-bundle-identifier id.nuurani.dm \
  --add-data "assets/icon.png:assets" \
  --exclude-module tkinter \
  --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtWebEngineWidgets \
  --exclude-module PySide6.QtQml --exclude-module PySide6.QtQuick --exclude-module PySide6.Qt3DCore \
  --exclude-module PySide6.QtMultimedia --exclude-module PySide6.QtCharts \
  run_app.py

PLIST="dist/$APP.app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString 1.0.0" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :NSHighResolutionCapable bool true" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :LSMinimumSystemVersion string 11.0" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :NSRequiresAquaSystemAppearance bool false" "$PLIST" 2>/dev/null || true
# tanda tangan ad-hoc agar Gatekeeper & Apple Silicon tidak rewel
codesign --force --deep --sign - "dist/$APP.app"

echo "▶ Membuat DMG…"
rm -f "dist/NuuraniDM.dmg"
hdiutil create -volname "$APP" -srcfolder "dist/$APP.app" -ov -format UDZO "dist/NuuraniDM.dmg" >/dev/null

echo "✅ Selesai: dist/$APP.app  &  dist/NuuraniDM.dmg"
echo "   Pasang: seret ke /Applications, atau jalankan:  cp -R \"dist/$APP.app\" /Applications/"
