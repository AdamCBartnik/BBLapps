"""
SnapshotWindow — a frozen copy of the current beamview frame.

Created by "Make New Figure".  Each window holds:
  - a full image + colorbar + axes (pan/zoom enabled, hover tooltip)
  - a Save button that writes  ssss_NNN.png  and  ssss_NNN.h5  to today's
    data directory (\\samba\bbl_online\beamdata\YYYY\MM\YYYY-MM-DD\).

The .h5 file stores the raw pixel array and all display metadata so the image
can be fully reconstructed or reprocessed later.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

import h5py
import numpy as np
import pyqtgraph as pg
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QSizePolicy,
)

# utilities lives one level above beamview/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utilities.today import get_todays_directory


# ---------------------------------------------------------------------------
# Helper: find next available ssss_NNN stem
# ---------------------------------------------------------------------------

def _next_ssss_stem(directory: Path, prefix: str = "ssss") -> Path:
    """Return a Path stem (no extension) that doesn't clash with existing files.

    Mirrors ssss.m: a custom prefix tries the bare name first, then appends _2, _3, ...
    The default 'ssss' prefix is always numbered (ssss_001, ssss_002, ...).
    """
    _exts = (".png", ".h5")

    def _free(stem: Path) -> bool:
        return all(not stem.with_suffix(e).exists() for e in _exts)

    if prefix != "ssss":
        bare = directory / prefix
        if _free(bare):
            return bare
        n = 2
        while True:
            stem = directory / f"{prefix}_{n}"
            if _free(stem):
                return stem
            n += 1
    else:
        n = 1
        while True:
            stem = directory / f"ssss_{n:03d}"
            if _free(stem):
                return stem
            n += 1


# ---------------------------------------------------------------------------
# SnapshotWindow
# ---------------------------------------------------------------------------

class SnapshotWindow(QMainWindow):
    """
    Frozen image window created by "Make New Figure".

    Parameters
    ----------
    raw_img      : uint16 ndarray (h, w) — sensor pixel values, no transforms
    xx           : float64 (w,)          — x coordinate for each column
    yy           : float64 (h,)          — y coordinate for each row (descending)
    display_img  : float32 ndarray (h,w) — after log/threshold, used for display
    display_min/max : float              — colormap clipping levels
    colormap     : str                   — colormap name (e.g. "grey")
    cmap_reversed: bool
    lut          : ndarray (256,3) uint8 — pre-built LUT from main window
    title_str    : str                   — window title / plot title
    camera_name  : str
    exposure_ms  : float
    gain         : float
    """

    def __init__(
        self,
        raw_img: np.ndarray,
        xx: np.ndarray,
        yy: np.ndarray,
        display_img: np.ndarray,
        display_min: float,
        display_max: float,
        colormap: str,
        cmap_reversed: bool,
        lut: np.ndarray,
        title_str: str,
        camera_name: str = "",
        exposure_ms: float = 0.0,
        gain: float = 1.0,
    ):
        super().__init__()
        self.setWindowTitle(title_str)
        self.resize(900, 650)

        # Keep copies of everything we'll need for saving
        self._raw_img      = raw_img.copy()
        self._xx           = xx.copy()
        self._yy           = yy.copy()
        self._display_min  = display_min
        self._display_max  = display_max
        self._colormap     = colormap
        self._cmap_reversed= cmap_reversed
        self._title_str    = title_str
        self._camera_name  = camera_name
        self._exposure_ms  = exposure_ms
        self._gain         = gain

        self._build_ui(display_img, lut)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self, display_img: np.ndarray, lut: np.ndarray):
        root = QWidget()
        self.setCentralWidget(root)
        vlay = QVBoxLayout(root)
        vlay.setContentsMargins(4, 4, 4, 4)
        vlay.setSpacing(4)

        # ── Image area ────────────────────────────────────────────────
        self._gfx = pg.GraphicsLayoutWidget()
        self._gfx.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self._plot = self._gfx.addPlot(row=0, col=0)
        self._plot.setAspectLocked(True)
        self._plot.showAxis("top", False)
        self._plot.showAxis("right", False)
        self._plot.setTitle(self._title_str, size="10pt")

        self._image_item = pg.ImageItem()
        self._plot.addItem(self._image_item)

        # Colorbar (narrow right panel)
        self._cbar_plot = self._gfx.addPlot(row=0, col=1)
        self._gfx.ci.layout.setColumnFixedWidth(1, 70)
        self._cbar_plot.showAxis("top", False)
        self._cbar_plot.showAxis("bottom", False)
        self._cbar_plot.showAxis("right", False)
        self._cbar_plot.setMouseEnabled(x=False, y=False)
        self._cbar_plot.setMenuEnabled(False)

        _grad = np.linspace(0, 1, 256, dtype=np.float32).reshape(1, 256)
        self._cbar_item = pg.ImageItem(_grad)
        self._cbar_item.setLevels((0, 1))
        self._cbar_plot.addItem(self._cbar_item)
        self._cbar_plot.setXRange(0, 1, padding=0)

        vlay.addWidget(self._gfx)

        # ── Bottom bar: status | Prefix: [____] [Save] ───────────────
        bot = QHBoxLayout()
        self._status_lbl = QLabel("")
        bot.addWidget(self._status_lbl, stretch=1)
        bot.addWidget(QLabel("Prefix:"))
        self._prefix_edit = QLineEdit("ssss")
        self._prefix_edit.setFixedWidth(80)
        self._prefix_edit.textChanged.connect(self._on_prefix_changed)
        bot.addWidget(self._prefix_edit)
        self._save_btn = QPushButton("Save")
        self._save_btn.setFixedWidth(55)
        self._save_btn.clicked.connect(self._on_save)
        bot.addWidget(self._save_btn)
        vlay.addLayout(bot)

        # ── Hover tooltip (same pattern as main window) ───────────────
        self._hover_label = QLabel("", self)
        self._hover_label.setStyleSheet(
            "background-color: rgba(30,30,30,210); color: white;"
            "padding: 2px 5px; border-radius: 3px; font-size: 11px;"
        )
        self._hover_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._hover_label.hide()
        self._gfx.viewport().setMouseTracking(True)
        self._gfx.viewport().installEventFilter(self)

        # ── Populate image ────────────────────────────────────────────
        self._image_item.setLookupTable(lut)
        self._image_item.setImage(display_img[::-1].T, autoLevels=False)
        self._image_item.setLevels((self._display_min, self._display_max))

        h, w = display_img.shape
        rect = self._compute_rect(h, w)
        self._image_item.setRect(*rect)

        lo, hi = self._display_min, self._display_max
        self._cbar_item.setRect(0, lo, 1, hi - lo)
        self._cbar_item.setLookupTable(lut)
        self._cbar_plot.setYRange(lo, hi, padding=0)

        # Fit image in view
        self._plot.setRange(
            xRange=(self._xx[0], self._xx[-1]),
            yRange=(self._yy[-1], self._yy[0]),
            padding=0.02,
        )

    def _compute_rect(self, h: int, w: int):
        xx, yy = self._xx, self._yy
        if len(xx) == w and len(yy) == h:
            dx = xx[1] - xx[0] if w > 1 else 1.0
            dy = abs(yy[0] - yy[1]) if h > 1 else 1.0
            return (xx[0] - 0.5 * dx, yy[-1] - 0.5 * dy, w * dx, h * dy)
        # Fallback
        return (float(xx[0]), float(yy[-1]),
                float(xx[-1] - xx[0]), float(yy[0] - yy[-1]))

    # ------------------------------------------------------------------
    # Hover tooltip (mirrors main_window logic)
    # ------------------------------------------------------------------

    def eventFilter(self, obj, event):
        from PyQt5.QtCore import QEvent
        if obj is self._gfx.viewport():
            if event.type() == QEvent.MouseMove:
                self._update_hover_tooltip(event.pos())
            elif event.type() == QEvent.Leave:
                self._hover_label.hide()
        return super().eventFilter(obj, event)

    def _update_hover_tooltip(self, viewport_pos):
        vb = self._plot.vb
        scene_pt = self._gfx.mapToScene(viewport_pos)
        data_pt  = vb.mapSceneToView(scene_pt)
        dx, dy   = data_pt.x(), data_pt.y()

        xx, yy, img = self._xx, self._yy, self._raw_img
        if dx < xx[0] or dx > xx[-1] or dy < yy[-1] or dy > yy[0]:
            self._hover_label.hide()
            return

        col = int(np.clip(np.searchsorted(xx, dx),  0, img.shape[1] - 1))
        row = int(np.clip(np.searchsorted(-yy, -dy), 0, img.shape[0] - 1))
        val = img[row, col]

        self._hover_label.setText(f"x={dx:.1f}  y={dy:.1f}  val={val}")
        self._hover_label.adjustSize()

        gpos = self._gfx.mapToGlobal(viewport_pos)
        lpos = self.mapFromGlobal(gpos)
        lx = min(lpos.x() + 14, self.width()  - self._hover_label.width()  - 4)
        ly = max(lpos.y() - 24, 4)
        self._hover_label.move(lx, ly)
        self._hover_label.show()
        self._hover_label.raise_()

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _on_prefix_changed(self):
        """Re-enable Save whenever the prefix is edited after a successful save."""
        self._save_btn.setEnabled(True)
        self._status_lbl.setText("")

    def _on_save(self):
        prefix = self._prefix_edit.text().strip() or "ssss"

        try:
            today_dir = get_todays_directory()
        except Exception as e:
            self._status_lbl.setText(f"Save failed: {e}")
            return

        if not today_dir.exists():
            self._status_lbl.setText(
                f"Directory not found: {today_dir}  (cron job creates it on-site)"
            )
            return

        stem = _next_ssss_stem(today_dir, prefix=prefix)

        # ── PNG: grab the window as rendered ─────────────────────────
        try:
            from PyQt5.QtGui import QPixmap
            pixmap = self.grab()
            png_path = stem.with_suffix(".png")
            pixmap.save(str(png_path))
        except Exception as e:
            self._status_lbl.setText(f"PNG save failed: {e}")
            return

        # ── HDF5: raw data + metadata ─────────────────────────────────
        try:
            h5_path = stem.with_suffix(".h5")
            with h5py.File(h5_path, "w") as f:
                f.create_dataset("image", data=self._raw_img,
                                 compression="gzip", compression_opts=4)
                f.create_dataset("xx", data=self._xx)
                f.create_dataset("yy", data=self._yy)

                f.attrs["title"]         = self._title_str
                f.attrs["camera_name"]   = self._camera_name
                f.attrs["exposure_ms"]   = self._exposure_ms
                f.attrs["gain"]          = self._gain
                f.attrs["colormap"]      = self._colormap
                f.attrs["cmap_reversed"] = int(self._cmap_reversed)
                f.attrs["display_min"]   = self._display_min
                f.attrs["display_max"]   = self._display_max
        except Exception as e:
            self._status_lbl.setText(f"HDF5 save failed: {e}")
            return

        name = stem.name
        self._status_lbl.setText(f"Saved: {name}.png + {name}.h5")
        self._save_btn.setEnabled(False)   # prevent accidental double-save
