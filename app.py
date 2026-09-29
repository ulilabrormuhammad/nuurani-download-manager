"""Nuurani DM — antarmuka desktop macOS (PySide6 / Qt 6, native Cocoa, Retina, Dark Mode)."""
from __future__ import annotations

import os
import queue
import re
import sys
import time

from PySide6.QtCore import (QAbstractTableModel, QItemSelectionModel, QModelIndex, QRectF, QSize, Qt, QTimer)
from PySide6.QtGui import (QAction, QBrush, QColor, QFont, QGuiApplication, QIcon, QKeySequence,
                           QPainter, QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFrame, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
                               QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QStyle,
                               QStyledItemDelegate, QSystemTrayIcon, QTableView, QToolBar, QVBoxLayout,
                               QWidget)

from . import APP_NAME, __version__
from .engine import CATEGORIES, Status, human_size, human_time
from .manager import Manager, open_file, reveal_in_finder
from .server import item_headers, start_server

ACCENT = QColor("#2C7A7B")      # teal Nuurani
ACCENT2 = QColor("#1A3B5D")     # biru tua
DONE = QColor("#2F9E44")
ERR = QColor("#C92A2A")
PAUSE = QColor("#E8A317")

SIDEBAR = ["Semua", "Sedang Berjalan", "Belum Selesai", "Selesai", None,
           *CATEGORIES.keys(), "Lainnya"]
SIDEBAR_ICONS = {"Semua": "📥", "Sedang Berjalan": "⚡", "Belum Selesai": "⏸", "Selesai": "✅",
                 "Video": "🎬", "Musik": "🎵", "Dokumen": "📄", "Arsip": "🗜", "Program": "💿",
                 "Gambar": "🖼", "Lainnya": "📦"}
CLIP_EXT = {e for exts in CATEGORIES.values() for e in exts} - {"txt", "csv", "html", "svg"}
URL_RE = re.compile(r"^https?://\S+$", re.I)

COLUMNS = ["Nama File", "Ukuran", "Progres", "Kecepatan", "Sisa Waktu", "Status", "Ditambahkan"]


def status_color(st: str) -> QColor:
    return {Status.COMPLETED: DONE, Status.ERROR: ERR, Status.PAUSED: PAUSE}.get(st, ACCENT)


# ============================================================ gambar bar segmen (ciri khas IDM)
def paint_segment_bar(p: QPainter, rect: QRectF, snap: dict, show_text=True, radius=4.0):
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    p.setClipPath(path)
    p.fillRect(rect, QColor(128, 128, 128, 55))
    size, st = snap["size"], snap["status"]
    col = status_color(st)
    if st == Status.COMPLETED:
        p.fillRect(rect, col)
    elif size > 0 and snap["segments"]:
        w = rect.width()
        for s, pos, e in snap["segments"]:
            if pos > s:
                x0 = rect.left() + w * s / size
                x1 = rect.left() + w * pos / size
                p.fillRect(QRectF(x0, rect.top(), max(1.0, x1 - x0), rect.height()), col)
        pen = QPen(QColor("#E64980"))  # garis merah muda = titik awal segmen, seperti IDM
        pen.setWidthF(1.2)
        p.setPen(pen)
        for s, pos, e in snap["segments"]:
            if s > 0 and pos <= e:
                x = rect.left() + w * s / size
                p.drawLine(int(x), int(rect.top()), int(x), int(rect.bottom()))
    elif snap["progress"] > 0:
        p.fillRect(QRectF(rect.left(), rect.top(), rect.width() * snap["progress"], rect.height()), col)
    p.setClipping(False)
    if show_text:
        p.setPen(QColor("white") if snap["progress"] > 0.45 else QApplication.palette().text().color())
        f = p.font()
        f.setPointSizeF(max(9.0, f.pointSizeF() - 1))
        f.setBold(True)
        p.setFont(f)
        txt = "100%" if st == Status.COMPLETED else (f"{snap['progress']*100:.1f}%" if size > 0 else human_size(snap["downloaded"]))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, txt)
    p.restore()


class ProgressDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        snap = index.data(Qt.ItemDataRole.UserRole)
        if not snap:
            return super().paint(painter, option, index)
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
        r = QRectF(option.rect).adjusted(6, 6, -6, -6)
        paint_segment_bar(painter, r, snap)


class SegmentBar(QWidget):
    def __init__(self):
        super().__init__()
        self.snap = None
        self.setMinimumHeight(22)

    def set_snap(self, snap):
        self.snap = snap
        self.update()

    def paintEvent(self, _):
        if not self.snap:
            return
        p = QPainter(self)
        paint_segment_bar(p, QRectF(self.rect()).adjusted(1, 1, -1, -1), self.snap, radius=6)
        p.end()


# ============================================================ model tabel
class DownloadModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.rows: list[dict] = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        return None

    def update_rows(self, rows: list[dict]) -> bool:
        """Return True bila susunan baris berubah (perlu memulihkan seleksi)."""
        if [r["id"] for r in rows] == [r["id"] for r in self.rows]:
            self.rows = rows
            if rows:
                self.dataChanged.emit(self.index(0, 0), self.index(len(rows) - 1, len(COLUMNS) - 1))
            return False
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()
        return True

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        r, c = self.rows[index.row()], index.column()
        if role == Qt.ItemDataRole.UserRole:
            return r
        if role == Qt.ItemDataRole.DisplayRole:
            if c == 0:
                return f"{SIDEBAR_ICONS.get(r['category'], '📦')}  {r['filename']}"
            if c == 1:
                return human_size(r["size"]) if r["size"] > 0 else "?"
            if c == 2:
                return None
            if c == 3:
                return f"{human_size(r['speed'])}/s" if r["status"] == Status.DOWNLOADING else ""
            if c == 4:
                return human_time(r["eta"]) if r["status"] == Status.DOWNLOADING else ""
            if c == 5:
                if r["status"] == Status.DOWNLOADING:
                    return f"{r['status']} · {r['active_conns']} koneksi"
                return r["status"]
            if c == 6:
                return time.strftime("%d %b %H:%M", time.localtime(r["added_at"]))
        if role == Qt.ItemDataRole.ForegroundRole and c == 5:
            return QBrush(status_color(r["status"]))
        if role == Qt.ItemDataRole.ToolTipRole:
            return r["error"] or r["url"]
        if role == Qt.ItemDataRole.TextAlignmentRole and c in (1, 3, 4):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None


# ============================================================ dialog
class AddDialog(QDialog):
    def __init__(self, parent, mgr: Manager, url: str = "", filename: str = "", headers: dict | None = None):
        super().__init__(parent)
        self.mgr, self.headers = mgr, headers or {}
        self.setWindowTitle("Tambah Unduhan")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("<b>Alamat (URL)</b> — satu per baris untuk unduhan massal:"))
        self.urls = QPlainTextEdit(url)
        self.urls.setPlaceholderText("https://contoh.com/file.zip")
        self.urls.setFixedHeight(90)
        lay.addWidget(self.urls)
        form = QFormLayout()
        self.name = QLineEdit(filename)
        self.name.setPlaceholderText("Otomatis dari server")
        form.addRow("Nama file:", self.name)
        row = QHBoxLayout()
        self.folder = QLineEdit(mgr.settings.download_dir)
        btn = QPushButton("Pilih…")
        btn.clicked.connect(self._pick)
        row.addWidget(self.folder)
        row.addWidget(btn)
        form.addRow("Simpan ke:", row)
        self.conns = QSpinBox()
        self.conns.setRange(1, 32)
        self.conns.setValue(mgr.settings.connections)
        form.addRow("Koneksi:", self.conns)
        self.start_now = QCheckBox("Mulai sekarang")
        self.start_now.setChecked(True)
        form.addRow("", self.start_now)
        lay.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Unduh")
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _pick(self):
        d = QFileDialog.getExistingDirectory(self, "Pilih folder", self.folder.text())
        if d:
            self.folder.setText(d)

    def _ok(self):
        urls = [u.strip() for u in self.urls.toPlainText().splitlines() if URL_RE.match(u.strip())]
        if not urls:
            QMessageBox.warning(self, APP_NAME, "Masukkan minimal satu URL http/https yang valid.")
            return
        folder = os.path.expanduser(self.folder.text().strip()) or self.mgr.settings.download_dir
        name = self.name.text().strip() if len(urls) == 1 else None
        for u in urls:
            self.mgr.add(u, folder, name, self.conns.value(), self.headers, self.start_now.isChecked())
        self.accept()


class SettingsDialog(QDialog):
    def __init__(self, parent, mgr: Manager):
        super().__init__(parent)
        self.mgr = mgr
        s = mgr.settings
        self.setWindowTitle("Pengaturan")
        self.setMinimumWidth(480)
        form = QFormLayout(self)
        row = QHBoxLayout()
        self.dir = QLineEdit(s.download_dir)
        b = QPushButton("Pilih…")
        b.clicked.connect(lambda: self.dir.setText(QFileDialog.getExistingDirectory(self, "Folder", self.dir.text()) or self.dir.text()))
        row.addWidget(self.dir)
        row.addWidget(b)
        form.addRow("Folder unduhan:", row)
        self.cat = QCheckBox("Pisahkan otomatis ke subfolder (Video, Musik, Dokumen, …)")
        self.cat.setChecked(s.categorize)
        form.addRow("", self.cat)
        self.conns = QSpinBox(); self.conns.setRange(1, 32); self.conns.setValue(s.connections)
        form.addRow("Koneksi per unduhan:", self.conns)
        self.conc = QSpinBox(); self.conc.setRange(1, 10); self.conc.setValue(s.max_concurrent)
        form.addRow("Unduhan bersamaan:", self.conc)
        self.limit = QSpinBox(); self.limit.setRange(0, 1_000_000); self.limit.setSuffix(" KB/s")
        self.limit.setSpecialValueText("Tanpa batas"); self.limit.setValue(s.speed_limit_kbps)
        form.addRow("Batas kecepatan:", self.limit)
        self.retry = QSpinBox(); self.retry.setRange(0, 50); self.retry.setValue(s.max_retries)
        form.addRow("Coba ulang per koneksi:", self.retry)
        self.clip = QCheckBox("Pantau clipboard untuk tautan unduhan"); self.clip.setChecked(s.clipboard_monitor)
        self.notif = QCheckBox("Notifikasi macOS saat selesai/gagal"); self.notif.setChecked(s.notify)
        self.sleep = QCheckBox("Cegah Mac tidur selama mengunduh"); self.sleep.setChecked(s.prevent_sleep)
        for w in (self.clip, self.notif, self.sleep):
            form.addRow("", w)
        form.addRow("Port ekstensi browser:", QLabel(f"127.0.0.1:{s.server_port}"))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _save(self):
        s = self.mgr.settings
        s.download_dir = os.path.expanduser(self.dir.text().strip()) or s.download_dir
        s.categorize = self.cat.isChecked()
        s.connections = self.conns.value()
        s.max_concurrent = self.conc.value()
        s.speed_limit_kbps = self.limit.value()
        s.max_retries = self.retry.value()
        s.clipboard_monitor = self.clip.isChecked()
        s.notify = self.notif.isChecked()
        s.prevent_sleep = self.sleep.isChecked()
        self.mgr.apply_settings()
        self.accept()


# ============================================================ jendela utama
class MainWindow(QMainWindow):
    def __init__(self, mgr: Manager, icon: QIcon):
        super().__init__()
        self.mgr = mgr
        self.inbox: "queue.Queue[dict]" = queue.Queue()
        self.filter = "Semua"
        self._last_clip = ""
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(icon)
        self.resize(1120, 640)
        self.setUnifiedTitleAndToolBarOnMac(True)
        self.setAcceptDrops(True)

        self._build_actions()
        self._build_toolbar()
        self._build_menu()
        self._build_body()
        self._build_tray(icon)

        self.statusBar().showMessage("Siap")
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(500)
        QApplication.clipboard().dataChanged.connect(self._clipboard_changed)
        self.refresh()

    # ---------------------------------------------------------- UI builders
    def _act(self, text, slot, shortcut=None, icon=None):
        a = QAction(text, self)
        if icon is not None:
            a.setIcon(self.style().standardIcon(icon))
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        a.triggered.connect(slot)
        return a

    def _build_actions(self):
        SP = QStyle.StandardPixmap
        self.a_add = self._act("Tambah URL", self.add_dialog, "Ctrl+N", SP.SP_FileDialogNewFolder)
        self.a_resume = self._act("Lanjutkan", self.resume_sel, "Ctrl+R", SP.SP_MediaPlay)
        self.a_pause = self._act("Jeda", self.pause_sel, "Ctrl+P", SP.SP_MediaPause)
        self.a_resume_all = self._act("Mulai Semua", self.mgr.resume_all, "Ctrl+Shift+R", SP.SP_MediaSeekForward)
        self.a_pause_all = self._act("Jeda Semua", self.mgr.pause_all, "Ctrl+Shift+P", SP.SP_MediaStop)
        self.a_remove = self._act("Hapus", lambda: self.remove_sel(False), "Backspace", SP.SP_TrashIcon)
        self.a_remove_file = self._act("Hapus beserta File", lambda: self.remove_sel(True), "Ctrl+Backspace")
        self.a_open = self._act("Buka File", self.open_sel, "Ctrl+O", SP.SP_FileIcon)
        self.a_reveal = self._act("Tampilkan di Finder", self.reveal_sel, "Ctrl+Shift+F", SP.SP_DirOpenIcon)
        self.a_copy = self._act("Salin URL", self.copy_url, "Ctrl+Shift+C")
        self.a_redownload = self._act("Unduh Ulang", self.redownload_sel)
        self.a_clear = self._act("Bersihkan yang Selesai", self.mgr.clear_completed)
        self.a_settings = self._act("Pengaturan…", self.settings_dialog, "Ctrl+,", SP.SP_FileDialogDetailedView)
        self.a_settings.setMenuRole(QAction.MenuRole.PreferencesRole)
        self.a_quit = self._act("Keluar Nuurani DM", self.quit_app, "Ctrl+Q")
        self.a_quit.setMenuRole(QAction.MenuRole.QuitRole)
        self.a_about = self._act("Tentang Nuurani DM", self.about)
        self.a_about.setMenuRole(QAction.MenuRole.AboutRole)

    def _build_toolbar(self):
        tb = QToolBar("Utama")
        tb.setMovable(False)
        tb.setIconSize(QSize(20, 20))
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        for a in (self.a_add, None, self.a_resume, self.a_pause, None, self.a_resume_all,
                  self.a_pause_all, None, self.a_remove, self.a_reveal, None, self.a_settings):
            tb.addSeparator() if a is None else tb.addAction(a)
        self.addToolBar(tb)

    def _build_menu(self):
        mb = self.menuBar()
        m = mb.addMenu("Berkas")
        for a in (self.a_add, None, self.a_settings, self.a_about, None, self.a_quit):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("Unduhan")
        for a in (self.a_resume, self.a_pause, None, self.a_resume_all, self.a_pause_all, None,
                  self.a_open, self.a_reveal, self.a_copy, self.a_redownload, None, self.a_remove,
                  self.a_remove_file, self.a_clear):
            m.addSeparator() if a is None else m.addAction(a)

    def _build_body(self):
        split = QSplitter(Qt.Orientation.Horizontal)
        self.side = QListWidget()
        self.side.setFixedWidth(190)
        self.side.setFrameShape(QFrame.Shape.NoFrame)
        self.side.setStyleSheet("QListWidget{background:transparent;font-size:13px}"
                                "QListWidget::item{padding:6px 8px;border-radius:6px}"
                                f"QListWidget::item:selected{{background:{ACCENT.name()};color:white}}")
        for name in SIDEBAR:
            if name is None:
                it = QListWidgetItem("KATEGORI")
                it.setFlags(Qt.ItemFlag.NoItemFlags)
                f = it.font(); f.setPointSize(10); f.setBold(True); it.setFont(f)
            else:
                it = QListWidgetItem(f"{SIDEBAR_ICONS[name]}  {name}")
                it.setData(Qt.ItemDataRole.UserRole, name)
            self.side.addItem(it)
        self.side.setCurrentRow(0)
        self.side.currentItemChanged.connect(self._side_changed)
        split.addWidget(self.side)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        self.model = DownloadModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setItemDelegateForColumn(2, ProgressDelegate(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c, w in ((1, 90), (2, 190), (3, 100), (4, 90), (5, 170), (6, 110)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(c, w)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.doubleClicked.connect(self._double_click)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._update_detail())
        rl.addWidget(self.table, 1)

        # panel detail
        self.detail = QWidget()
        dl = QVBoxLayout(self.detail)
        dl.setContentsMargins(14, 10, 14, 12)
        self.d_title = QLabel()
        f = self.d_title.font(); f.setPointSize(f.pointSize() + 2); f.setBold(True); self.d_title.setFont(f)
        self.d_title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.d_bar = SegmentBar()
        self.d_info = QLabel()
        self.d_info.setWordWrap(True)
        self.d_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        for w in (self.d_title, self.d_bar, self.d_info):
            dl.addWidget(w)
        self.detail.setFixedHeight(130)
        self.detail.hide()
        rl.addWidget(self.detail)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        self.setCentralWidget(split)

    def _build_tray(self, icon):
        self.tray = QSystemTrayIcon(icon, self)
        m = QMenu()
        m.addAction("Tampilkan Nuurani DM", self.show_front)
        m.addAction(self.a_add)
        m.addSeparator()
        m.addAction(self.a_resume_all)
        m.addAction(self.a_pause_all)
        m.addSeparator()
        m.addAction(self.a_quit)
        self.tray.setContextMenu(m)
        self.tray.setToolTip(APP_NAME)
        self.tray.show()

    # ---------------------------------------------------------- refresh loop
    def refresh(self):
        # tautan dari ekstensi browser
        while QApplication.activeModalWidget() is None:
            try:
                item = self.inbox.get_nowait()
            except queue.Empty:
                break
            self.show_front()
            AddDialog(self, self.mgr, item.get("url", ""), item.get("filename", "") or "",
                      item_headers(item)).exec()

        snaps = self.mgr.snapshots()
        counts = {k: 0 for k in SIDEBAR if k}
        for s in snaps:
            counts["Semua"] += 1
            counts[s["category"]] = counts.get(s["category"], 0) + 1
            if s["status"] in (Status.DOWNLOADING, Status.CONNECTING):
                counts["Sedang Berjalan"] += 1
            if s["status"] == Status.COMPLETED:
                counts["Selesai"] += 1
            else:
                counts["Belum Selesai"] += 1
        for i in range(self.side.count()):
            it = self.side.item(i)
            name = it.data(Qt.ItemDataRole.UserRole)
            if name:
                n = counts.get(name, 0)
                it.setText(f"{SIDEBAR_ICONS[name]}  {name}" + (f"   ({n})" if n else ""))

        rows = [s for s in snaps if self._match(s)]
        rows.sort(key=lambda s: s["added_at"], reverse=True)
        sel = self.selected_ids()
        if self.model.update_rows(rows):
            self._restore_selection(sel)
        self._update_detail()

        spd = self.mgr.total_speed()
        run = self.mgr.running_count()
        msg = f"⚡ {human_size(spd)}/s  ·  {run} berjalan  ·  {len(snaps)} total" if run else f"{len(snaps)} unduhan"
        if self.mgr.settings.speed_limit_kbps:
            msg += f"  ·  batas {self.mgr.settings.speed_limit_kbps} KB/s"
        self.statusBar().showMessage(msg)
        self.tray.setToolTip(f"{APP_NAME} — {human_size(spd)}/s" if run else APP_NAME)
        self.setWindowTitle(f"{APP_NAME} — {human_size(spd)}/s" if run else APP_NAME)

    def _match(self, s):
        f = self.filter
        if f == "Semua":
            return True
        if f == "Sedang Berjalan":
            return s["status"] in (Status.DOWNLOADING, Status.CONNECTING)
        if f == "Selesai":
            return s["status"] == Status.COMPLETED
        if f == "Belum Selesai":
            return s["status"] != Status.COMPLETED
        return s["category"] == f

    def _side_changed(self, cur, _prev):
        if cur and cur.data(Qt.ItemDataRole.UserRole):
            self.filter = cur.data(Qt.ItemDataRole.UserRole)
            self.refresh()

    def _update_detail(self):
        snaps = self.selected_snaps()
        if len(snaps) != 1:
            self.detail.hide()
            return
        s = snaps[0]
        self.detail.show()
        self.d_title.setText(s["filename"])
        self.d_bar.set_snap(s)
        active = sum(1 for a, p, e in s["segments"] if p <= e)
        parts = [f"<b>Status:</b> {s['status']}",
                 f"<b>Diunduh:</b> {human_size(s['downloaded'])} / {human_size(s['size'])}",
                 f"<b>Kecepatan:</b> {human_size(s['speed'])}/s" if s["status"] == Status.DOWNLOADING else "",
                 f"<b>Koneksi:</b> {s['active_conns']}/{s['connections']} · {len(s['segments'])} segmen ({active} tersisa)",
                 f"<b>Resume:</b> {'Ya' if s['resumable'] else 'Tidak'}"]
        info = "   ".join(p for p in parts if p)
        info += f"<br><span style='color:gray'>{s['path']}</span>"
        if s["error"]:
            info += f"<br><span style='color:{ERR.name()}'>⚠ {s['error']}</span>"
        self.d_info.setText(info)

    # ---------------------------------------------------------- selection helpers
    def selected_snaps(self):
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self.model.rows[r] for r in rows if r < len(self.model.rows)]

    def selected_ids(self):
        return [s["id"] for s in self.selected_snaps()]

    def _restore_selection(self, ids):
        sm = self.table.selectionModel()
        for r, row in enumerate(self.model.rows):
            if row["id"] in ids:
                sm.select(self.model.index(r, 0),
                          QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)

    # ---------------------------------------------------------- actions
    def add_dialog(self, url=""):
        if not isinstance(url, str) or not url:
            url = ""
            txt = QApplication.clipboard().text().strip()
            url = txt if URL_RE.match(txt) else ""
        self.show_front()
        AddDialog(self, self.mgr, url).exec()
        self.refresh()

    def settings_dialog(self):
        SettingsDialog(self, self.mgr).exec()

    def resume_sel(self):
        for i in self.selected_ids():
            self.mgr.resume(i)

    def pause_sel(self):
        for i in self.selected_ids():
            self.mgr.pause(i)

    def remove_sel(self, delete_file):
        ids = self.selected_ids()
        if not ids:
            return
        q = f"Hapus {len(ids)} unduhan dari daftar" + (" BESERTA filenya dari disk?" if delete_file else "?")
        if QMessageBox.question(self, APP_NAME, q) == QMessageBox.StandardButton.Yes:
            for i in ids:
                self.mgr.remove(i, delete_file)
            self.refresh()

    def open_sel(self):
        for s in self.selected_snaps():
            if s["status"] == Status.COMPLETED:
                open_file(s["path"])

    def reveal_sel(self):
        snaps = self.selected_snaps()
        if snaps:
            s = snaps[0]
            reveal_in_finder(s["path"] if s["status"] == Status.COMPLETED else s["path"] + ".part")
        else:
            reveal_in_finder(os.path.join(self.mgr.settings.download_dir, "."))

    def copy_url(self):
        QApplication.clipboard().setText("\n".join(s["url"] for s in self.selected_snaps()))
        self._last_clip = QApplication.clipboard().text()

    def redownload_sel(self):
        for s in self.selected_snaps():
            self.mgr.add(s["url"])

    def _double_click(self, idx):
        s = self.model.rows[idx.row()]
        if s["status"] == Status.COMPLETED:
            open_file(s["path"])
        elif s["status"] in (Status.PAUSED, Status.ERROR):
            self.mgr.resume(s["id"])
        else:
            self.mgr.pause(s["id"])

    def _context_menu(self, pos):
        if not self.selected_ids():
            return
        m = QMenu(self)
        for a in (self.a_open, self.a_reveal, None, self.a_resume, self.a_pause, self.a_redownload,
                  None, self.a_copy, None, self.a_remove, self.a_remove_file):
            m.addSeparator() if a is None else m.addAction(a)
        m.exec(self.table.viewport().mapToGlobal(pos))

    def about(self):
        QMessageBox.about(self, APP_NAME,
                          f"<h3>{APP_NAME} {__version__}</h3>"
                          "<p>Download manager multi-koneksi untuk macOS.</p>"
                          "<p>Dynamic segmentation · connection reuse · resume aman · antrean · "
                          "batas kecepatan · kategori otomatis · integrasi browser.</p>"
                          "<p>© Nuurani</p>")

    # ---------------------------------------------------------- clipboard & drag-drop
    def _clipboard_changed(self):
        if not self.mgr.settings.clipboard_monitor:
            return
        txt = QApplication.clipboard().text().strip()
        if txt == self._last_clip or not URL_RE.match(txt):
            return
        self._last_clip = txt
        path = txt.split("?", 1)[0].lower()
        ext = path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""
        if ext in CLIP_EXT:
            self.add_dialog(txt)

    def dragEnterEvent(self, e):
        md = e.mimeData()
        if md.hasUrls() or md.hasText():
            e.acceptProposedAction()

    def dropEvent(self, e):
        md = e.mimeData()
        urls = [u.toString() for u in md.urls() if u.scheme() in ("http", "https")] if md.hasUrls() else []
        if not urls and md.hasText():
            urls = [t.strip() for t in md.text().splitlines() if URL_RE.match(t.strip())]
        if urls:
            self.add_dialog("\n".join(urls))

    # ---------------------------------------------------------- window lifecycle
    def show_front(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e):
        # seperti IDM: tutup jendela = tetap jalan di menu bar; keluar via ⌘Q
        e.ignore()
        self.hide()
        if self.mgr.running_count():
            self.tray.showMessage(APP_NAME, "Unduhan tetap berjalan di latar belakang.",
                                  QSystemTrayIcon.MessageIcon.Information, 2500)

    def quit_app(self):
        self.timer.stop()
        QApplication.quit()  # mgr.shutdown dipanggil lewat aboutToQuit


def make_icon() -> QIcon:
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "..", "assets", "icon.png"),
              os.path.join(getattr(sys, "_MEIPASS", here), "assets", "icon.png")):
        if os.path.exists(p):
            return QIcon(p)
    pm = QPixmap(256, 256)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(ACCENT2)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(16, 16, 224, 224, 50, 50)
    p.setPen(QColor("white"))
    f = QFont()
    f.setPointSize(120)
    f.setBold(True)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "N")
    p.end()
    return QIcon(pm)


def main():
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName("Nuurani")
    app.setQuitOnLastWindowClosed(False)
    icon = make_icon()
    app.setWindowIcon(icon)

    mgr = Manager()
    win = MainWindow(mgr, icon)

    srv = start_server(mgr.settings.server_port, lambda item: win.inbox.put(item))
    if srv is None:
        win.statusBar().showMessage("Port ekstensi browser sedang dipakai aplikasi lain", 8000)

    # klik ikon Dock saat jendela tersembunyi -> tampilkan lagi
    app.applicationStateChanged.connect(
        lambda st: win.show_front() if st == Qt.ApplicationState.ApplicationActive and not win.isVisible() else None)
    app.aboutToQuit.connect(mgr.shutdown)

    # URL dari argumen baris perintah
    for a in sys.argv[1:]:
        if URL_RE.match(a):
            mgr.add(a)

    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
