import time
from datetime import datetime
import numpy as np
import pyqtgraph as pg

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QPushButton, QLabel, QDoubleSpinBox, QSpinBox,
    QComboBox, QCheckBox, QGroupBox, QSizePolicy,
    QScrollArea, QLineEdit, QFrame, QRubberBand, QGridLayout,
)
from PyQt5.QtCore import QTimer, Qt, QThread, QObject, pyqtSignal, QRect, QSize, QEvent
from PyQt5.QtGui import QFont

from .cameras.base import CameraBase

try:
    import epics as _epics
except ImportError:
    _epics = None

try:
    from utilities.get_colormap import get_colormap as _get_colormap
    COLORMAPS = _get_colormap()
except Exception:
    COLORMAPS = ["Gray", "Viridis", "Plasma", "Inferno", "Magma", "Hot", "Jet"]

EPICS_PREFIXES = [
    "", "B24", "B29", "PPGUN", "PER", "CMM", "CMM2", "Sample",
    "MEDUSA0", "MEDUSA1", "MEDUSA2", "MEDUSA3", "MEDUSA4",
    "MEDUSA5", "MEDUSA6", "MEDUSA7", "MEDUSA8", "MEDUSA9",
]


class FrameWorker(QObject):
    """Captures frames on a background thread so the Qt event loop stays free."""
    frame_ready = pyqtSignal(object)   # emits np.ndarray

    def __init__(self, camera: "CameraBase"):
        super().__init__()
        self._camera = camera
        self._busy = False

    def request_frame(self):
        if self._busy:
            return
        # Skip the caget entirely if the camera signals no new frame is ready
        # (VPCAMCamera monitors frame_timestamp via CA; other cameras always return True)
        if not self._camera.has_new_frame():
            return
        self._busy = True
        try:
            img = self._camera.snapshot()
            self.frame_ready.emit(img)
        except Exception as e:
            print(f"[worker] {e}")
        finally:
            self._busy = False


class MainWindow(QMainWindow):
    def __init__(self, camera: CameraBase, lab_name: str = "Beamview",
                 entries=None, epics_prefix: str = ""):
        """
        Parameters
        ----------
        camera  : initial (first) camera object
        lab_name: shown in window title
        entries : list[CameraEntry] from config_loader, or None for single-camera
        """
        super().__init__()
        self._entries = entries or []   # CameraEntry list; empty = single-camera mode
        self._lab_name = lab_name
        self._epics_prefix = epics_prefix
        self.camera = camera
        self._set_window_title()
        self.resize(1150, 900)

        # Worker thread for blocking camera captures
        self._worker_thread = QThread(self)
        self._worker = FrameWorker(camera)
        self._worker.moveToThread(self._worker_thread)
        self._worker.frame_ready.connect(self._on_frame_ready)
        self._worker_thread.start()

        self._t_frame_request: float = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._on_timer_tick)

        self._display_min = 0.0
        self._display_max = float(camera.max_value)
        self._pending_set_range = False
        self._last_roi_display = None
        self._zoom_mode = False
        self._current_lut = None
        # Last processed frame + coordinates, kept so pan/zoom can re-run analysis
        self._last_analysis_img: np.ndarray | None = None
        self._last_analysis_xx:  np.ndarray | None = None
        self._last_analysis_yy:  np.ndarray | None = None
        self._zoom_start = None

        self._build_ui()
        self._refresh_camera_settings()
        self._apply_colormap()
        self._update_colorbar_range()
        # Load initial calibration scale from EPICS if configured
        if self._entries and self._entries[0].has_epics_cal:
            try:
                from .config_loader import _load_scale
                sx, sy = _load_scale(self._entries[0].cal_prefix)
                self._scale_x_spin.blockSignals(True)
                self._scale_y_spin.blockSignals(True)
                self._scale_x_spin.setValue(sx)
                self._scale_y_spin.setValue(sy)
                self._scale_x_spin.blockSignals(False)
                self._scale_y_spin.blockSignals(False)
            except Exception as e:
                print(f"[scale init] {e}")
        # Rubber band for zoom selection (parented to viewport so it overlays the image)
        self._rubber_band = QRubberBand(QRubberBand.Rectangle, self._gfx.viewport())
        self._gfx.viewport().installEventFilter(self)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        root_widget = QWidget()
        self.setCentralWidget(root_widget)
        root = QVBoxLayout(root_widget)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        # ── Top section: image + right panel ──────────────────────────
        top = QHBoxLayout()
        top.setSpacing(4)
        root.addLayout(top, stretch=1)

        # Image — PlotItem + ImageItem gives us free axes with tick marks
        self._gfx = pg.GraphicsLayoutWidget()
        self._gfx.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # Main image plot (col 0)
        self._plot = self._gfx.addPlot(row=0, col=0)
        self._plot.setAspectLocked(True)
        self._plot.showAxis('top', False)
        self._plot.showAxis('right', False)
        self._plot.vb.sigRangeChanged.connect(self._on_view_range_changed)
        self._image_item = pg.ImageItem()
        self._plot.addItem(self._image_item)

        # Floating tooltip label — parented to the top-level window so it can
        # overlay anything, hidden by default
        self._hover_label = QLabel("", self)
        self._hover_label.setStyleSheet(
            "background-color: rgba(30,30,30,210); color: white;"
            "padding: 2px 5px; border-radius: 3px; font-size: 11px;"
        )
        self._hover_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._hover_label.hide()
        self._gfx.viewport().setMouseTracking(True)
        self._gfx.viewport().installEventFilter(self)

        # Colorbar plot (col 1) — narrow fixed-width panel, never redrawn per frame
        self._cbar_plot = self._gfx.addPlot(row=0, col=1)
        self._gfx.ci.layout.setColumnFixedWidth(1, 70)
        self._cbar_plot.showAxis('top', False)
        self._cbar_plot.showAxis('bottom', False)
        self._cbar_plot.showAxis('right', False)
        self._cbar_plot.setMouseEnabled(x=False, y=False)
        self._cbar_plot.setMenuEnabled(False)
        _grad = np.linspace(0, 1, 256, dtype=np.float32).reshape(1, 256)
        self._cbar_item = pg.ImageItem(_grad)
        self._cbar_item.setLevels((0, 1))
        self._cbar_plot.addItem(self._cbar_item)
        self._cbar_plot.setXRange(0, 1, padding=0)

        top.addWidget(self._gfx, stretch=1)

        # Right panel
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFixedWidth(295)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        right_widget = QWidget()
        self._right_layout = QVBoxLayout(right_widget)
        self._right_layout.setSpacing(4)
        self._right_layout.setContentsMargins(2, 2, 2, 2)
        right_scroll.setWidget(right_widget)
        top.addWidget(right_scroll)

        self._build_camera_enable_group()
        self._build_snapshot_group()
        self._build_background_group()
        self._build_range_group()
        self._build_analysis_group()
        self._build_longterm_group()
        self._right_layout.addStretch()

        # ── Bottom strip ───────────────────────────────────────────────
        bottom = QHBoxLayout()
        bottom.setSpacing(4)
        root.addLayout(bottom)

        self._build_camera_info_group(bottom)
        self._build_data_processing_group(bottom)
        self._build_roi_group(bottom)
        self._build_colormap_group(bottom)
        bottom.addStretch()

    # ------------------------------------------------------------------
    # Right panel groups
    # ------------------------------------------------------------------

    def _right_group(self, title):
        box = QGroupBox(title)
        lay = QVBoxLayout(box)
        lay.setSpacing(3)
        lay.setContentsMargins(6, 8, 6, 4)
        self._right_layout.addWidget(box)
        return lay

    def _build_camera_enable_group(self):
        lay = self._right_group("Camera Enable")

        row1 = QHBoxLayout()
        self._on_off_btn = QPushButton("Camera Off")
        self._on_off_btn.setCheckable(True)
        self._on_off_btn.setFixedWidth(88)
        self._on_off_btn.toggled.connect(self._on_toggle)
        row1.addWidget(self._on_off_btn)
        self._redraw_btn = QPushButton("Redraw")
        self._redraw_btn.setFixedWidth(62)
        self._redraw_btn.clicked.connect(self._force_redraw)
        row1.addWidget(self._redraw_btn)
        self._fps_lbl = QLabel("0.000 s/frame")
        self._fps_lbl.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        row1.addWidget(self._fps_lbl)
        row1.addStretch()
        lay.addLayout(row1)

    def _build_snapshot_group(self):
        lay = self._right_group("Save Data")
        row = QHBoxLayout()
        self._new_fig_btn = QPushButton("Make New Figure")
        self._new_fig_btn.clicked.connect(self._on_make_new_figure)
        row.addWidget(self._new_fig_btn)
        row.addStretch()
        lay.addLayout(row)
        self._snapshot_windows: list = []   # keep references so windows stay open

    def _build_background_group(self):
        lay = self._right_group("Background")

        row = QHBoxLayout()
        self._save_bg_btn = QPushButton("Save Background")
        self._save_bg_btn.clicked.connect(self._on_save_bg)
        self._save_bg_btn.clicked.connect(self._trigger_redraw)
        row.addWidget(self._save_bg_btn)
        self._subtract_bg_chk = QCheckBox("Subtract Background")
        self._subtract_bg_chk.setEnabled(False)
        self._subtract_bg_chk.toggled.connect(self._trigger_redraw)
        row.addWidget(self._subtract_bg_chk)
        lay.addLayout(row)

        self._bg_image = None

    def _build_range_group(self):
        lay = self._right_group("Intensity Range")

        # Row 0: [Set Range] [Reset]   (buttons left-aligned)
        row1 = QHBoxLayout()
        self._set_range_btn = QPushButton("Set Range")
        self._set_range_btn.clicked.connect(self._on_set_range)
        self._set_range_btn.clicked.connect(self._trigger_redraw)
        self._reset_range_btn = QPushButton("Reset")
        self._reset_range_btn.clicked.connect(self._on_reset_range)
        self._reset_range_btn.clicked.connect(self._trigger_redraw)
        row1.addWidget(self._set_range_btn)
        row1.addWidget(self._reset_range_btn)
        row1.addStretch()
        lay.addLayout(row1)

        # Rows 1-2: Min/Max boxes + checkboxes aligned in a grid
        #   Col 0: "Min:" / "Max:"   Col 1: edit box   Col 2: checkbox
        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(2)

        grid.addWidget(QLabel("Min:"), 0, 0, Qt.AlignRight)
        self._range_min_edit = QLineEdit("0")
        self._range_min_edit.setFixedWidth(66)
        self._range_min_edit.editingFinished.connect(self._on_set_manual_range)
        self._range_min_edit.editingFinished.connect(self._trigger_redraw)
        grid.addWidget(self._range_min_edit, 0, 1)
        self._allow_neg_chk = QCheckBox("Allow Negative")
        self._allow_neg_chk.toggled.connect(self._trigger_redraw)
        grid.addWidget(self._allow_neg_chk, 0, 2)

        grid.addWidget(QLabel("Max:"), 1, 0, Qt.AlignRight)
        self._range_max_edit = QLineEdit(str(int(self.camera.max_value)))
        self._range_max_edit.setFixedWidth(66)
        self._range_max_edit.editingFinished.connect(self._on_set_manual_range)
        self._range_max_edit.editingFinished.connect(self._trigger_redraw)
        grid.addWidget(self._range_max_edit, 1, 1)
        self._log_plot_chk = QCheckBox("Log Plot")
        self._log_plot_chk.toggled.connect(self._trigger_redraw)
        grid.addWidget(self._log_plot_chk, 1, 2)

        lay.addLayout(grid)

    def _build_analysis_group(self):
        lay = self._right_group("Single Frame Analysis")


        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(1)
        grid.setColumnStretch(0, 1)   # label column stretches
        grid.setColumnMinimumWidth(1, 88)  # value column fixed

        def stat_row(r, label):
            lbl = QLabel(f"{label}:")
            lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            val = QLabel("0.00")
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(lbl, r, 0)
            grid.addWidget(val, r, 1)
            return val

        self._lbl_peak   = stat_row(0, "Peak Intensity")
        self._lbl_cx     = stat_row(1, "Centroid X")
        self._lbl_cy     = stat_row(2, "Centroid Y")
        self._lbl_sx     = stat_row(3, "Width (rms) X")
        self._lbl_sy     = stat_row(4, "Width (rms) Y")
        self._lbl_cxy    = stat_row(5, "Correlation XY")
        self._lbl_tilt   = stat_row(6, "Tilt Angle (deg)")
        self._lbl_maxpct = stat_row(7, "Max Data (%)")
        self._lbl_sum    = stat_row(8, "Integrated Int.")

        lay.addLayout(grid)

        nn_row = QHBoxLayout()
        self._nn_chk = QCheckBox("NxN:")
        self._nn_chk.toggled.connect(self._trigger_redraw)
        self._nn_x_spin = QSpinBox()
        self._nn_x_spin.setRange(1, 500)
        self._nn_x_spin.setValue(5)
        self._nn_x_spin.setFixedWidth(45)
        self._nn_x_spin.editingFinished.connect(self._trigger_redraw)
        self._nn_x_spin.valueChanged.connect(self._trigger_redraw)
        self._nn_y_spin = QSpinBox()
        self._nn_y_spin.setRange(1, 500)
        self._nn_y_spin.setValue(5)
        self._nn_y_spin.setFixedWidth(45)
        self._nn_y_spin.editingFinished.connect(self._trigger_redraw)
        self._nn_y_spin.valueChanged.connect(self._trigger_redraw)
        nn_row.addWidget(self._nn_chk)
        nn_row.addWidget(self._nn_x_spin)
        nn_row.addWidget(QLabel("×"))
        nn_row.addWidget(self._nn_y_spin)
        nn_row.addStretch()
        lay.addLayout(nn_row)

        epics_row = QHBoxLayout()
        self._to_epics_chk = QCheckBox("To EPICS")
        self._to_epics_chk.setChecked(True)
        epics_row.addWidget(self._to_epics_chk)
        epics_row.addStretch()
        lay.addLayout(epics_row)

    def _build_longterm_group(self):
        lay = self._right_group("Long Term Analysis")

        row1 = QHBoxLayout()
        self._longterm_chk = QCheckBox("Enable")
        row1.addWidget(self._longterm_chk)
        row1.addWidget(QLabel("Buffer Size:"))
        self._buffer_spin = QSpinBox()
        self._buffer_spin.setRange(2, 1000)
        self._buffer_spin.setValue(20)
        self._buffer_spin.setFixedWidth(55)
        row1.addWidget(self._buffer_spin)
        self._reset_buffer_btn = QPushButton("Reset Buffer")
        self._reset_buffer_btn.clicked.connect(self._reset_buffer)
        row1.addWidget(self._reset_buffer_btn)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Filled:"))
        self._lbl_buf_pct = QLabel("0.00")
        self._lbl_buf_pct.setFixedWidth(36)
        row2.addWidget(self._lbl_buf_pct)
        row2.addStretch()
        row2.addWidget(QLabel("Threshold (%):"))
        self._longterm_thresh_spin = QDoubleSpinBox()
        self._longterm_thresh_spin.setRange(0, 100)
        self._longterm_thresh_spin.setValue(50)
        self._longterm_thresh_spin.setFixedWidth(55)
        row2.addWidget(self._longterm_thresh_spin)
        lay.addLayout(row2)


        lt_grid = QGridLayout()
        lt_grid.setHorizontalSpacing(4)
        lt_grid.setVerticalSpacing(1)
        lt_grid.setColumnMinimumWidth(0, 38)  # label
        lt_grid.setColumnMinimumWidth(1, 60)  # mean value
        lt_grid.setColumnMinimumWidth(2, 14)  # ±
        lt_grid.setColumnMinimumWidth(3, 60)  # std value

        def lt_row(r, label):
            lbl = QLabel(label)
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            mean = QLabel("0.00")
            mean.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            pm = QLabel("±")
            pm.setAlignment(Qt.AlignCenter)
            std = QLabel("0.00")
            std.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            lt_grid.addWidget(lbl,  r, 0)
            lt_grid.addWidget(mean, r, 1)
            lt_grid.addWidget(pm,   r, 2)
            lt_grid.addWidget(std,  r, 3)
            return mean, std

        self._lbl_lt_cx,  self._lbl_lt_cx_std  = lt_row(0, "<X>:")
        self._lbl_lt_cy,  self._lbl_lt_cy_std  = lt_row(1, "<Y>:")
        self._lbl_lt_sx,  self._lbl_lt_sx_std  = lt_row(2, "sig x:")
        self._lbl_lt_sy,  self._lbl_lt_sy_std  = lt_row(3, "sig y:")
        lay.addLayout(lt_grid)

        self._jitter_x_buf = []
        self._jitter_y_buf = []
        self._sx_buf = []
        self._sy_buf = []

    # ------------------------------------------------------------------
    # Bottom strip groups
    # ------------------------------------------------------------------

    def _bottom_group(self, title, parent_layout):
        box = QGroupBox(title)
        lay = QVBoxLayout(box)
        lay.setSpacing(3)
        lay.setContentsMargins(6, 8, 6, 4)
        parent_layout.addWidget(box)
        return lay

    def _build_camera_info_group(self, parent):
        lay = self._bottom_group("Camera Info", parent)

        # Camera selector — first item in the box, matching MATLAB layout
        # The combo is always created; only shown when multiple cameras exist
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name:"))
        self._camera_combo = QComboBox()
        for e in self._entries:
            self._camera_combo.addItem(e.display_name)
        self._camera_combo.currentIndexChanged.connect(self._on_camera_changed)
        name_row.addWidget(self._camera_combo, stretch=1)
        lay.addLayout(name_row)
        # Hide the row entirely in single-camera mode
        if not self._entries:
            self._camera_combo.hide()
            name_row.itemAt(0).widget().hide()  # hide "Name:" label too

        grid = QGridLayout()
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(3)

        # Row 0: Size
        grid.addWidget(QLabel("Size:"), 0, 0, Qt.AlignRight)
        self._cam_size_lbl = QLabel("0 x 0")
        self._cam_size_lbl.setFixedWidth(80)
        grid.addWidget(self._cam_size_lbl, 0, 1, 1, 4)

        # Row 1: Scale x  y  [Units=pixels]
        grid.addWidget(QLabel("Scale:"), 1, 0, Qt.AlignRight)
        self._scale_x_spin = QDoubleSpinBox()
        self._scale_x_spin.setRange(0.0001, 100000)
        self._scale_x_spin.setValue(1.0)
        self._scale_x_spin.setDecimals(4)
        self._scale_x_spin.setFixedWidth(70)
        self._scale_x_spin.editingFinished.connect(self._on_scale_changed)
        grid.addWidget(self._scale_x_spin, 1, 1)
        grid.addWidget(QLabel("x"), 1, 2, Qt.AlignCenter)
        self._scale_y_spin = QDoubleSpinBox()
        self._scale_y_spin.setRange(0.0001, 100000)
        self._scale_y_spin.setValue(1.0)
        self._scale_y_spin.setDecimals(4)
        self._scale_y_spin.setFixedWidth(70)
        self._scale_y_spin.editingFinished.connect(self._on_scale_changed)
        grid.addWidget(self._scale_y_spin, 1, 3)
        self._units_pixels_chk = QCheckBox("Units = pixels")
        self._units_pixels_chk.setChecked(False)
        self._units_pixels_chk.toggled.connect(self._on_scale_changed)
        grid.addWidget(self._units_pixels_chk, 1, 4)

        # Row 2: Exposure (ms)  [value]   Gain  [value]
        grid.addWidget(QLabel("Exposure (ms):"), 2, 0, Qt.AlignRight)
        self._exposure_spin = QDoubleSpinBox()
        self._exposure_spin.setRange(0.001, 10000.0)
        self._exposure_spin.setDecimals(3)
        self._exposure_spin.setSingleStep(1.0)
        self._exposure_spin.setFixedWidth(70)
        self._exposure_spin.editingFinished.connect(self._on_exposure_changed)
        grid.addWidget(self._exposure_spin, 2, 1)
        grid.addWidget(QLabel("Gain:"), 2, 2, Qt.AlignRight)
        self._gain_spin = QDoubleSpinBox()
        self._gain_spin.setRange(0.0, 100.0)
        self._gain_spin.setDecimals(2)
        self._gain_spin.setSingleStep(0.1)
        self._gain_spin.setFixedWidth(55)
        self._gain_spin.editingFinished.connect(self._on_gain_changed)
        grid.addWidget(self._gain_spin, 2, 3)

        # Row 3: EPICS prefix
        grid.addWidget(QLabel("EPICS prefix:"), 3, 0, Qt.AlignRight)
        self._epics_prefix_combo = QComboBox()
        self._epics_prefix_combo.addItems(EPICS_PREFIXES)
        if self._epics_prefix in EPICS_PREFIXES:
            self._epics_prefix_combo.setCurrentText(self._epics_prefix)
        grid.addWidget(self._epics_prefix_combo, 3, 1, 1, 4)

        lay.addLayout(grid)

    def _build_data_processing_group(self, parent):
        lay = self._bottom_group("Data Processing", parent)

        row1 = QHBoxLayout()
        self._threshold_chk = QCheckBox("Threshold")
        self._threshold_chk.toggled.connect(self._trigger_redraw)
        self._threshold_spin = QDoubleSpinBox()
        self._threshold_spin.setRange(0, 100)
        self._threshold_spin.setFixedWidth(50)
        self._threshold_spin.editingFinished.connect(self._trigger_redraw)
        self._threshold_spin.valueChanged.connect(self._trigger_redraw)
        self._threshold_type_combo = QComboBox()
        self._threshold_type_combo.addItems(["Percent", "Absolute"])
        self._threshold_type_combo.setFixedWidth(75)
        self._threshold_type_combo.currentTextChanged.connect(self._on_threshold_type_changed)
        self._threshold_type_combo.currentTextChanged.connect(self._trigger_redraw)
        row1.addWidget(self._threshold_chk)
        row1.addWidget(self._threshold_spin)
        row1.addWidget(self._threshold_type_combo)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        self._median_chk = QCheckBox("Median filter:")
        self._median_chk.toggled.connect(self._trigger_redraw)
        self._median_spin = QSpinBox()
        self._median_spin.setRange(1, 21)
        self._median_spin.setValue(3)
        self._median_spin.setSingleStep(2)
        self._median_spin.setFixedWidth(45)
        self._median_spin.editingFinished.connect(self._trigger_redraw)
        self._median_spin.valueChanged.connect(self._trigger_redraw)
        row2.addWidget(self._median_chk)
        row2.addWidget(self._median_spin)
        lay.addLayout(row2)

        row_sg = QHBoxLayout()
        self._sgauss_chk = QCheckBox("S-Gauss:")
        self._sgauss_chk.toggled.connect(self._trigger_redraw)
        self._sgauss_width_spin = QSpinBox()
        self._sgauss_width_spin.setRange(2, 200)
        self._sgauss_width_spin.setValue(5)
        self._sgauss_width_spin.setFixedWidth(45)
        self._sgauss_width_spin.editingFinished.connect(self._trigger_redraw)
        self._sgauss_width_spin.valueChanged.connect(self._trigger_redraw)
        self._sgauss_power_spin = QDoubleSpinBox()
        self._sgauss_power_spin.setRange(0.1, 10.0)
        self._sgauss_power_spin.setValue(2.0)
        self._sgauss_power_spin.setSingleStep(0.1)
        self._sgauss_power_spin.setDecimals(2)
        self._sgauss_power_spin.setFixedWidth(90)
        self._sgauss_power_spin.editingFinished.connect(self._trigger_redraw)
        self._sgauss_power_spin.valueChanged.connect(self._trigger_redraw)
        row_sg.addWidget(self._sgauss_chk)
        row_sg.addWidget(self._sgauss_width_spin)
        row_sg.addWidget(QLabel("p:"))
        row_sg.addWidget(self._sgauss_power_spin)
        lay.addLayout(row_sg)

        row_rot = QHBoxLayout()
        self._rotate_chk = QCheckBox("Rotate:")
        self._rotate_chk.toggled.connect(self._trigger_redraw)
        self._rotate_angle_spin = QDoubleSpinBox()
        self._rotate_angle_spin.setRange(-180.0, 180.0)
        self._rotate_angle_spin.setValue(0.0)
        self._rotate_angle_spin.setSingleStep(0.5)
        self._rotate_angle_spin.setDecimals(1)
        self._rotate_angle_spin.setFixedWidth(65)
        self._rotate_angle_spin.editingFinished.connect(self._trigger_redraw)
        self._rotate_angle_spin.valueChanged.connect(self._trigger_redraw)
        row_rot.addWidget(self._rotate_chk)
        row_rot.addWidget(self._rotate_angle_spin)
        row_rot.addWidget(QLabel("deg"))
        lay.addLayout(row_rot)

        row3 = QHBoxLayout()
        self._frame_avg_chk = QCheckBox("Frame avg:")
        self._frame_avg_chk.toggled.connect(self._trigger_redraw)
        self._frame_avg_spin = QSpinBox()
        self._frame_avg_spin.setRange(2, 200)
        self._frame_avg_spin.setValue(10)
        self._frame_avg_spin.setFixedWidth(45)
        self._frame_avg_reset_btn = QPushButton("Reset")
        self._frame_avg_reset_btn.setFixedWidth(45)
        self._frame_avg_reset_btn.clicked.connect(self._reset_frame_avg)
        self._frame_avg_count_lbl = QLabel("0 / 10")
        self._frame_avg_count_lbl.setFixedWidth(48)
        self._frame_avg_count_lbl.setAlignment(Qt.AlignCenter)
        row3.addWidget(self._frame_avg_chk)
        row3.addWidget(self._frame_avg_spin)
        row3.addWidget(self._frame_avg_count_lbl)
        row3.addWidget(self._frame_avg_reset_btn)
        lay.addLayout(row3)

        self._frame_avg_buffer = []

    def _build_roi_group(self, parent):
        lay = self._bottom_group("Hardware Region of Interest (ROI)", parent)


        grid = QGridLayout()
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(3)

        _LBL_W = 72   # "Horizontal:" / "Vertical:" column
        _SP_W  = 58   # spinbox column width

        lbl_h = QLabel("Horizontal:")
        lbl_h.setFixedWidth(_LBL_W)
        lbl_h.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        grid.addWidget(lbl_h, 0, 0)

        self._roi_x_min = QSpinBox()
        self._roi_x_min.setRange(0, 99999)
        self._roi_x_min.setFixedWidth(_SP_W)
        self._roi_x_min.editingFinished.connect(self._on_roi_apply)
        grid.addWidget(self._roi_x_min, 0, 1)

        grid.addWidget(QLabel("–"), 0, 2, Qt.AlignCenter)

        self._roi_x_max = QSpinBox()
        self._roi_x_max.setRange(0, 99999)
        self._roi_x_max.setFixedWidth(_SP_W)
        self._roi_x_max.editingFinished.connect(self._on_roi_apply)
        grid.addWidget(self._roi_x_max, 0, 3)

        self._roi_reset_btn = QPushButton("Reset")
        self._roi_reset_btn.setFixedWidth(50)
        self._roi_reset_btn.clicked.connect(self._on_roi_reset)
        grid.addWidget(self._roi_reset_btn, 0, 4)

        lbl_v = QLabel("Vertical:")
        lbl_v.setFixedWidth(_LBL_W)
        lbl_v.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        grid.addWidget(lbl_v, 1, 0)

        self._roi_y_min = QSpinBox()
        self._roi_y_min.setRange(0, 99999)
        self._roi_y_min.setFixedWidth(_SP_W)
        self._roi_y_min.editingFinished.connect(self._on_roi_apply)
        grid.addWidget(self._roi_y_min, 1, 1)

        grid.addWidget(QLabel("–"), 1, 2, Qt.AlignCenter)

        self._roi_y_max = QSpinBox()
        self._roi_y_max.setRange(0, 99999)
        self._roi_y_max.setFixedWidth(_SP_W)
        self._roi_y_max.editingFinished.connect(self._on_roi_apply)
        grid.addWidget(self._roi_y_max, 1, 3)

        self._zoom_btn = QPushButton("Zoom")
        self._zoom_btn.setFixedWidth(50)
        self._zoom_btn.clicked.connect(self._on_zoom_btn)
        grid.addWidget(self._zoom_btn, 1, 4)

        lay.addLayout(grid)

    def _build_colormap_group(self, parent):
        lay = self._bottom_group("Colormap", parent)

        row = QHBoxLayout()
        self._colormap_combo = QComboBox()
        self._colormap_combo.addItems(COLORMAPS)
        self._colormap_combo.setCurrentText("Freeze")
        self._colormap_combo.currentTextChanged.connect(self._apply_colormap)
        self._colormap_combo.currentTextChanged.connect(self._trigger_redraw)
        self._colormap_flip = QCheckBox("Reverse")
        self._colormap_flip.toggled.connect(self._apply_colormap)
        self._colormap_flip.toggled.connect(self._trigger_redraw)
        row.addWidget(self._colormap_combo)
        row.addWidget(self._colormap_flip)
        lay.addLayout(row)

        lay.addStretch()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _refresh_camera_settings(self):
        try:
            self._exposure_spin.setValue(self.camera.exposure_time * 1e3)
        except Exception:
            pass
        try:
            self._gain_spin.setValue(self.camera.gain)
        except Exception:
            pass
        try:
            w, h = self.camera.width_max, self.camera.height_max
            self._cam_size_lbl.setText(f"{w} x {h}")
        except Exception:
            pass
        self._refresh_roi_boxes()

    def _apply_colormap(self):
        name = self._colormap_combo.currentText()
        if self._colormap_flip.isChecked():
            name += "_r"
        try:
            from utilities.get_colormap import get_colormap
            rgb = get_colormap(name, m=256)        # (256, 3) float 0-1
            lut = np.empty((256, 4), dtype=np.uint8)
            lut[:, :3] = (rgb * 255).astype(np.uint8)
            lut[:, 3] = 255
            self._current_lut = lut
            self._image_item.setLookupTable(lut)
            self._cbar_item.setLookupTable(lut)
        except Exception:
            self._current_lut = None

    def _update_colorbar_range(self):
        lo, hi = self._display_min, self._display_max
        self._cbar_item.setRect(0, lo, 1, hi - lo)   # stretch gradient to cover display range
        self._cbar_plot.setYRange(lo, hi, padding=0)

    def _reset_buffer(self):
        self._jitter_x_buf.clear()
        self._jitter_y_buf.clear()
        self._sx_buf.clear()
        self._sy_buf.clear()

    def _reset_frame_avg(self):
        self._frame_avg_buffer.clear()

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_threshold_type_changed(self, text):
        if text == "Percent":
            self._threshold_spin.setRange(0, 100)
        else:
            self._threshold_spin.setRange(-1e9, 1e9)

    def _on_zoom_btn(self):
        self._zoom_mode = True
        self._zoom_btn.setText("Cancel Zoom")
        self._zoom_btn.clicked.disconnect(self._on_zoom_btn)
        self._zoom_btn.clicked.connect(self._cancel_zoom)
        self._gfx.viewport().setCursor(Qt.CrossCursor)

    def _cancel_zoom(self):
        self._zoom_mode = False
        self._zoom_start = None
        self._rubber_band.hide()
        self._gfx.viewport().setCursor(Qt.ArrowCursor)
        self._zoom_btn.setText("Zoom")
        self._zoom_btn.clicked.disconnect(self._cancel_zoom)
        self._zoom_btn.clicked.connect(self._on_zoom_btn)

    def eventFilter(self, obj, event):
        if obj is self._gfx.viewport():
            t = event.type()

            # ── Hover tooltip ──────────────────────────────────────────
            if t == QEvent.MouseMove:
                self._update_hover_tooltip(event.pos())
                if self._zoom_mode and self._zoom_start is not None:
                    self._rubber_band.setGeometry(
                        QRect(self._zoom_start, event.pos()).normalized())
                    return True

            elif t == QEvent.Leave:
                self._hover_label.hide()

            # ── Zoom rubber-band ───────────────────────────────────────
            if self._zoom_mode:
                if t == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                    self._zoom_start = event.pos()
                    self._rubber_band.setGeometry(QRect(self._zoom_start, QSize()))
                    self._rubber_band.show()
                    return True
                elif t == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                    if self._zoom_start is not None:
                        self._rubber_band.hide()
                        self._apply_zoom_selection(self._zoom_start, event.pos())
                    self._cancel_zoom()
                    return True

        return super().eventFilter(obj, event)

    def _update_hover_tooltip(self, viewport_pos):
        """Show a floating label with x, y (data coords) and pixel value."""
        if self._last_analysis_img is None:
            return
        vb   = self._plot.vb
        scene_pt = self._gfx.mapToScene(viewport_pos)
        data_pt  = vb.mapSceneToView(scene_pt)
        dx, dy   = data_pt.x(), data_pt.y()

        xx = self._last_analysis_xx
        yy = self._last_analysis_yy
        img = self._last_analysis_img

        # Check whether cursor is actually inside the image extent
        if xx is None or yy is None or img is None:
            self._hover_label.hide()
            return
        if dx < xx[0] or dx > xx[-1] or dy < yy[-1] or dy > yy[0]:
            self._hover_label.hide()
            return

        # Nearest pixel indices
        col = int(np.clip(np.searchsorted(xx, dx), 0, img.shape[1] - 1))
        # yy is descending (top of image = largest y), so flip search
        row = int(np.clip(np.searchsorted(-yy, -dy), 0, img.shape[0] - 1))
        val = img[row, col]

        self._hover_label.setText(f"x={dx:.1f}  y={dy:.1f}  val={val}")
        self._hover_label.adjustSize()

        # Position the label near the cursor, nudged so it doesn't hide under it
        gpos = self._gfx.mapToGlobal(viewport_pos)
        lpos = self.mapFromGlobal(gpos)
        offset_x, offset_y = 14, -24
        lx = lpos.x() + offset_x
        ly = lpos.y() + offset_y
        # Keep inside window
        lx = min(lx, self.width()  - self._hover_label.width()  - 4)
        ly = max(ly, 4)
        self._hover_label.move(lx, ly)
        self._hover_label.show()
        self._hover_label.raise_()

    def _apply_zoom_selection(self, p1, p2):
        """Convert two widget points to plot coordinates and apply as hardware ROI."""
        vb = self._plot.vb
        s1 = self._gfx.mapToScene(p1)
        s2 = self._gfx.mapToScene(p2)
        d1 = vb.mapSceneToView(s1)
        d2 = vb.mapSceneToView(s2)

        wmax = self.camera.width_max
        hmax = self.camera.height_max

        x0 = max(0, int(min(d1.x(), d2.x())))
        x1 = min(wmax - 1, int(max(d1.x(), d2.x())))
        y0 = max(0, int(min(d1.y(), d2.y())))
        y1 = min(hmax - 1, int(max(d1.y(), d2.y())))

        if x1 <= x0 or y1 <= y0:
            return  # too small / off-image

        for sb, val in [(self._roi_x_min, x0), (self._roi_x_max, x1),
                        (self._roi_y_min, y0), (self._roi_y_max, y1)]:
            sb.blockSignals(True)
            sb.setValue(val)
            sb.blockSignals(False)

        self._on_roi_apply()

    def _trigger_redraw(self, *_):
        """If the camera is off, immediately redraw with current settings.
        If it's running, the next timer tick will pick up the change naturally."""
        if not self._timer.isActive():
            self._update_frame()

    def _set_window_title(self):
        try:
            w, h = self.camera.width_max, self.camera.height_max
            size_str = f" ({w}×{h})"
        except Exception:
            size_str = ""
        name = self._entries[self._camera_combo.currentIndex()].display_name \
               if self._entries and hasattr(self, "_camera_combo") \
               else self._lab_name
        self.setWindowTitle(f"Beamview — {self._lab_name} — {name}{size_str}")

    def _on_camera_changed(self, index: int):
        """Switch to a different camera from the dropdown."""
        if not self._entries:
            return
        entry = self._entries[index]
        was_running = self._timer.isActive()

        # Stop current camera
        self._timer.stop()
        self.camera.stop_streaming()

        # Swap camera object + worker
        self.camera = entry.camera
        self._worker._camera = entry.camera
        self._display_max = float(entry.camera.max_value)

        # Load calibration scale from EPICS
        if entry.has_epics_cal:
            try:
                from .config_loader import _load_scale
                sx, sy = _load_scale(entry.cal_prefix)
                self._scale_x_spin.blockSignals(True)
                self._scale_y_spin.blockSignals(True)
                self._scale_x_spin.setValue(sx)
                self._scale_y_spin.setValue(sy)
                self._scale_x_spin.blockSignals(False)
                self._scale_y_spin.blockSignals(False)
            except Exception as e:
                print(f"[scale] {e}")

        # Reset display state
        self._last_roi_display = None
        self._last_analysis_img = None
        self._refresh_camera_settings()
        self._refresh_roi_boxes()
        self._set_window_title()
        self._on_reset_range()

        if was_running:
            self.camera.start_streaming(rate_hz=5.0)
            self._timer.start()

    def _on_toggle(self, checked):
        if checked:
            self._on_off_btn.setText("Camera On")
            self.camera.start_streaming(rate_hz=5.0)
            self._timer.start()
        else:
            self._timer.stop()
            self.camera.stop_streaming()
            self._on_off_btn.setText("Camera Off")

    def _force_redraw(self):
        if not self._timer.isActive():
            self._update_frame()

    def _on_exposure_changed(self):
        ms = self._exposure_spin.value()
        try:
            self.camera.exposure_time = ms * 1e-3
        except Exception as e:
            print(f"[exposure] {e}")

    def _on_gain_changed(self):
        try:
            self.camera.gain = self._gain_spin.value()
        except Exception as e:
            print(f"[gain] {e}")

    def _on_make_new_figure(self):
        """Open a frozen SnapshotWindow with the current frame."""
        if self._last_analysis_img is None:
            return
        from .snapshot_window import SnapshotWindow

        # Build display image the same way _process_and_display does
        img = self._last_analysis_img
        if self._log_plot_chk.isChecked():
            display_img = np.log10(1.0 + np.abs(img.astype(np.float32)))
        else:
            display_img = img.astype(np.float32)

        ts = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
        title_str = f"{self.windowTitle()}    {ts}"

        win = SnapshotWindow(
            raw_img      = img,
            xx           = self._last_analysis_xx,
            yy           = self._last_analysis_yy,
            display_img  = display_img,
            display_min  = self._display_min,
            display_max  = self._display_max,
            colormap     = self._colormap_combo.currentText(),
            cmap_reversed= self._colormap_flip.isChecked(),
            lut          = self._current_lut,
            title_str    = title_str,
            camera_name  = self.windowTitle(),
            exposure_ms  = self._exposure_spin.value(),
            gain         = self._gain_spin.value(),
        )
        win.show()
        self._snapshot_windows.append(win)

    def _on_save_bg(self):
        self._bg_image = self._last_raw.copy() if hasattr(self, "_last_raw") else None
        if self._bg_image is not None:
            self._subtract_bg_chk.setEnabled(True)

    def _on_set_range(self):
        """Snap range to [0, max of current (possibly log-transformed) frame data]."""
        self._pending_set_range = True

    def _on_reset_range(self):
        """Reset range to full scale: [0, camera max value]."""
        lo = 0.0
        hi = float(self.camera.max_value)
        if self._log_plot_chk.isChecked():
            import math
            lo = 0.0  # log10(1+0) = 0
            hi = math.log10(1.0 + hi)
        self._display_min = lo
        self._display_max = hi
        self._range_min_edit.setText(f"{lo:.4g}")
        self._range_max_edit.setText(f"{hi:.4g}")
        self._update_colorbar_range()

    def _on_set_manual_range(self):
        try:
            lo = float(self._range_min_edit.text())
            hi = float(self._range_max_edit.text())
            if hi <= lo:
                return
            self._display_min = lo
            self._display_max = hi
            self._update_colorbar_range()
        except ValueError:
            pass

    def _on_roi_reset(self):
        try:
            w = self.camera.width_max
            h = self.camera.height_max
            self._apply_roi(0, 0, w, h)
        except Exception as e:
            print(f"[roi reset] {e}")

    def _on_roi_apply(self):
        """Read the four ROI spinboxes, clamp, convert to sensor coords, send to camera."""
        try:
            wmax = self.camera.width_max
            hmax = self.camera.height_max

            # X: display coords == sensor coords (no horizontal flip)
            x0 = max(0, min(self._roi_x_min.value(), wmax - 1))
            x1 = max(x0 + 1, min(self._roi_x_max.value(), wmax - 1))

            # Y: display is vertically flipped — plot y=0 is sensor row hmax-1
            dy0 = max(0, min(self._roi_y_min.value(), hmax - 1))
            dy1 = max(dy0 + 1, min(self._roi_y_max.value(), hmax - 1))

            # Skip if nothing changed vs what the boxes already show (handles double-fire)
            cur = (self._roi_x_min.value(), self._roi_x_max.value(),
                   self._roi_y_min.value(), self._roi_y_max.value())
            if (x0, x1, dy0, dy1) == cur and hasattr(self, '_last_roi_display') \
                    and self._last_roi_display == (x0, x1, dy0, dy1):
                return

            sensor_y = hmax - 1 - dy1
            sensor_h = dy1 - dy0 + 1
            self._apply_roi(x0, sensor_y, x1 - x0 + 1, sensor_h)
        except Exception as e:
            print(f"[roi apply] {e}")

    def _apply_roi(self, x: int, y: int, w: int, h: int):
        """Send ROI to camera, read back actual values, update boxes, redraw."""
        was_running = self._timer.isActive()
        self._timer.stop()
        try:
            self.camera.set_roi(x, y, w, h)
            self._refresh_roi_boxes()
        finally:
            if was_running:
                self._timer.start()
        self._trigger_redraw()

    def _refresh_roi_boxes(self):
        """Read current ROI from camera, convert to display coords, update spinboxes,
        and reposition the image axes to match. Skips focused boxes."""
        try:
            rx, ry, rw, rh = self.camera.get_roi()
            hmax = self.camera.height_max

            # Y: sensor → display (flip)
            dy1 = hmax - 1 - ry
            dy0 = hmax - ry - rh

            focused = self.focusWidget()
            for sb, val in [(self._roi_x_min, rx),
                            (self._roi_x_max, rx + rw - 1),
                            (self._roi_y_min, dy0),
                            (self._roi_y_max, dy1)]:
                if sb is focused:
                    continue
                sb.blockSignals(True)
                sb.setValue(val)
                sb.blockSignals(False)

            # Cache the last applied display ROI for the double-fire guard
            self._last_roi_display = (rx, rx + rw - 1, dy0, dy1)
            self._update_image_rect(rx, dy0, rw, rh)
        except Exception as e:
            print(f"[roi readback] {e}")

    def _get_display_xy(self, h: int, w: int):
        """
        Compute display coordinates for an image of shape (h, w) pixels.

        img row 0 = top of sensor crop = HIGHEST display-y value.
        img col 0 = left edge of sensor crop.

        Returns:
            xx  : float64 (w,)  x coord of each column
            yy  : float64 (h,)  y coord of each row (row 0 → highest y)
            rect: (x_left, y_bottom, rect_w, rect_h) for ImageItem.setRect
        """
        # Use the last confirmed (applied) ROI, not the live spinbox values.
        # This prevents half-typed numbers from shifting the image mid-edit.
        if self._last_roi_display is not None:
            roi_x, roi_x1, roi_y0, roi_y1 = self._last_roi_display
        else:
            roi_x  = self._roi_x_min.value()
            roi_x1 = self._roi_x_max.value()
            roi_y0 = self._roi_y_min.value()
            roi_y1 = self._roi_y_max.value()

        if self._units_pixels_chk.isChecked():
            xx = roi_x  + np.arange(w, dtype=np.float64)
            yy = roi_y1 - np.arange(h, dtype=np.float64)   # row 0 = top = roi_y1
            rect = (float(roi_x), float(roi_y0), float(w), float(h))
        else:
            sx   = self._scale_x_spin.value()
            sy   = self._scale_y_spin.value()
            wmax = self.camera.width_max
            hmax = self.camera.height_max
            xx   = (roi_x  + np.arange(w, dtype=np.float64) - 0.5 * wmax) * sx
            yy   = (roi_y1 - np.arange(h, dtype=np.float64) - 0.5 * hmax) * sy
            # rect bottom-left corner, then full extent
            rect = (xx[0] - 0.5 * sx,
                    yy[-1] - 0.5 * sy,   # yy[-1] = smallest y (bottom row)
                    w * sx,
                    h * sy)

        return xx, yy, rect

    def _update_image_rect(self, x0: int, y0: int, w: int, h: int):
        """Refit the plot view to show the full image after a ROI change."""
        _, _, rect = self._get_display_xy(h, w)
        self._plot.setRange(
            xRange=(rect[0], rect[0] + rect[2]),
            yRange=(rect[1], rect[1] + rect[3]),
            padding=0.02)

    def _on_scale_changed(self):
        """Refit the plot view, write calibration back to EPICS, and redraw."""
        # Write new scale values back to EPICS calibration PVs if configured
        if self._entries and not self._units_pixels_chk.isChecked():
            idx = self._camera_combo.currentIndex()
            if 0 <= idx < len(self._entries):
                entry = self._entries[idx]
                if entry.has_epics_cal:
                    try:
                        from .config_loader import _write_scale
                        _write_scale(entry.cal_prefix,
                                     self._scale_x_spin.value(),
                                     self._scale_y_spin.value())
                    except Exception as e:
                        print(f"[scale write] {e}")

        if self._last_roi_display is not None:
            roi_x, roi_x1, roi_y0, roi_y1 = self._last_roi_display
        else:
            roi_x  = self._roi_x_min.value()
            roi_x1 = self._roi_x_max.value()
            roi_y0 = self._roi_y_min.value()
            roi_y1 = self._roi_y_max.value()
        rw = max(1, roi_x1 - roi_x + 1)
        rh = max(1, roi_y1 - roi_y0 + 1)
        self._update_image_rect(roi_x, roi_y0, rw, rh)
        self._trigger_redraw()

    # ------------------------------------------------------------------
    # Frame update
    # ------------------------------------------------------------------

    def _on_timer_tick(self):
        """Stamp the request time, then ask the worker for a frame."""
        self._t_frame_request = time.perf_counter()
        self._worker.request_frame()

    def _update_frame(self):
        """Direct (synchronous) capture + display — used only when camera is off."""
        self._t_frame_request = time.perf_counter()
        try:
            img = self.camera.snapshot()
        except Exception as e:
            print(f"[snapshot] {e}")
            return
        self._process_and_display(img)

    def _on_frame_ready(self, img: np.ndarray):
        """Slot called from the background worker thread via signal."""
        self._process_and_display(img)

    def _process_and_display(self, img: np.ndarray):
        t0 = self._t_frame_request if self._t_frame_request else time.perf_counter()

        self._last_raw = img

        # Update exposure display from camera readback, but not while the user is editing it
        if not self._exposure_spin.hasFocus():
            try:
                self._exposure_spin.blockSignals(True)
                self._exposure_spin.setValue(self.camera.exposure_time * 1e3)
                self._exposure_spin.blockSignals(False)
            except Exception:
                pass

        # Background subtraction
        if self._subtract_bg_chk.isChecked() and self._bg_image is not None:
            if self._bg_image.shape == img.shape:
                diff = img.astype(np.int32) - self._bg_image.astype(np.int32)
                if not self._allow_neg_chk.isChecked():
                    diff = np.clip(diff, 0, None)
                img = diff.astype(np.float32)

        # Median filter
        if self._median_chk.isChecked():
            from scipy.ndimage import median_filter
            img = median_filter(img, size=self._median_spin.value()).astype(np.uint16)

        # Super-gaussian smoothing
        if self._sgauss_chk.isChecked():
            img = self._apply_sgauss(img, self._sgauss_width_spin.value(),
                                     self._sgauss_power_spin.value())

        # Frame averaging
        if self._frame_avg_chk.isChecked():
            n = self._frame_avg_spin.value()
            self._frame_avg_buffer.append(img.astype(np.float32))
            if len(self._frame_avg_buffer) > n:
                self._frame_avg_buffer.pop(0)
            img = np.mean(self._frame_avg_buffer, axis=0).astype(np.uint16)
            self._frame_avg_count_lbl.setText(f"{len(self._frame_avg_buffer)} / {n}")
        else:
            self._frame_avg_count_lbl.setText("—")

        # Rotation
        if self._rotate_chk.isChecked():
            angle = self._rotate_angle_spin.value()
            if angle != 0.0:
                from scipy.ndimage import rotate as _rotate
                img = np.clip(
                    _rotate(img.astype(np.float32), angle, reshape=False, order=1),
                    0, np.iinfo(np.uint16).max
                ).astype(np.uint16)

        # Threshold
        if self._threshold_chk.isChecked():
            thresh_val = self._threshold_spin.value()
            if self._threshold_type_combo.currentText() == "Percent":
                cutoff = (thresh_val / 100.0) * float(img.max())
            else:
                cutoff = thresh_val
            img = np.where(img >= cutoff, img, 0).astype(np.uint16)

        # Log transform (applied before range logic, matching MATLAB: log10(1 + |data|))
        if self._log_plot_chk.isChecked():
            display_img = np.log10(1.0 + np.abs(img.astype(np.float32)))
        else:
            display_img = img.astype(np.float32)

        # "Set Range": snap to [0, max] of the current (possibly log) frame
        if self._pending_set_range:
            self._pending_set_range = False
            lo = 0.0 if not self._allow_neg_chk.isChecked() else float(display_img.min())
            hi = max(float(display_img.max()), lo + 1.0)
            self._display_min = lo
            self._display_max = hi
            self._range_min_edit.setText(f"{lo:.4g}")
            self._range_max_edit.setText(f"{hi:.4g}")
            self._update_colorbar_range()

        xx, yy, rect = self._get_display_xy(img.shape[0], img.shape[1])
        self._image_item.setImage(display_img[::-1].T, autoLevels=False)
        self._image_item.setLevels((self._display_min, self._display_max))
        self._image_item.setRect(*rect)

        # Cache for re-use when the user pans/zooms without a new frame
        self._last_analysis_img = img
        self._last_analysis_xx  = xx
        self._last_analysis_yy  = yy

        ci, cx_arr, cy_arr = self._visible_crop(img, xx, yy)
        self._update_analysis(ci, cx_arr, cy_arr)

        self._fps_lbl.setText(f"{time.perf_counter() - t0:.3f} s/frame")

    def _visible_crop(self, img: np.ndarray, xx: np.ndarray, yy: np.ndarray):
        """Return (img_crop, xx_crop, yy_crop) restricted to the currently visible
        view range.  If the full image is visible (or no range is set yet) the
        arrays are returned unchanged."""
        (vx0, vx1), (vy0, vy1) = self._plot.vb.viewRange()
        # Which columns of img fall inside the x view range?
        col_mask = (xx >= vx0) & (xx <= vx1)
        # Which rows of img fall inside the y view range?
        row_mask = (yy >= vy0) & (yy <= vy1)
        if not col_mask.any() or not row_mask.any():
            # Nothing visible — return originals so analysis doesn't go blank
            return img, xx, yy
        return img[np.ix_(row_mask, col_mask)], xx[col_mask], yy[row_mask]

    def _on_view_range_changed(self):
        """Re-run analysis on the visible crop whenever the user pans or zooms."""
        if (self._last_analysis_img is None or
                self._last_analysis_xx is None or
                self._last_analysis_yy is None):
            return
        ci, cx_arr, cy_arr = self._visible_crop(
            self._last_analysis_img, self._last_analysis_xx, self._last_analysis_yy
        )
        self._update_analysis(ci, cx_arr, cy_arr)

    def _apply_sgauss(self, img: np.ndarray, mean_param: int, p: float) -> np.ndarray:
        """Super-gaussian smoothing kernel, matching MATLAB source/make_plot.m."""
        import math
        from scipy.signal import fftconvolve
        sig = mean_param / 4.0
        sig_super = sig * np.sqrt(2 * math.gamma(1 + 1/p) / math.gamma(1 + 2/p))
        kw = int(np.ceil(mean_param * 1.3))
        kw = 2 * int(np.ceil((kw - 1) / 2)) + 1   # next higher odd number
        x = np.arange(-(kw - 1) / 2, (kw - 1) / 2 + 1)
        X, Y = np.meshgrid(x, x)
        kernel = np.exp(-((X**2 + Y**2) / (2 * sig_super**2))**p).astype(np.float32)
        kernel /= kernel.sum()
        out = fftconvolve(img.astype(np.float32), kernel, mode='same')
        return np.clip(out, 0, np.iinfo(np.uint16).max).astype(np.uint16)

    def _epics_pv(self, name: str) -> str:
        prefix = self._epics_prefix_combo.currentText()
        return f"{prefix}:{name}" if prefix else name

    def _caput_nonan(self, name: str, value: float) -> None:
        if _epics is None or np.isnan(value):
            return
        _epics.caput(self._epics_pv(name), value, wait=False)

    def _update_analysis(self, img: np.ndarray, xx: np.ndarray, yy: np.ndarray):
        d = img.astype(np.float64)
        total_full = d.sum()
        peak = float(d.max())
        pct = 100.0 * peak / self.camera.max_value

        self._lbl_peak.setText(f"{peak:.2f}")
        self._lbl_maxpct.setText(f"{pct:.1f} %")
        self._lbl_maxpct.setStyleSheet(
            "background-color: red;" if pct > 95 else ""
        )

        # NxN integrated intensity: max sum over all NxN sliding windows
        if self._nn_chk.isChecked():
            from scipy.signal import fftconvolve
            nx, ny = self._nn_x_spin.value(), self._nn_y_spin.value()
            total = float(fftconvolve(d, np.ones((nx, ny)), mode='same').max())
        else:
            total = total_full

        self._lbl_sum.setText(f"{total:.0f}")

        if total_full == 0:
            return

        rho = d / total_full  # centroid/sigma always use full-image normalization

        # xx has one entry per column; yy has one entry per row (row 0 = highest y)
        cx = (xx * rho.sum(axis=0)).sum()
        cy = (yy * rho.sum(axis=1)).sum()
        X, Y = np.meshgrid(xx, yy)
        sx = np.sqrt(((X - cx) ** 2 * rho).sum())
        sy = np.sqrt(((Y - cy) ** 2 * rho).sum())
        cxy = ((X - cx) * (Y - cy) * rho).sum()
        dd = np.sqrt((sx**2 - sy**2)**2 + 4 * cxy**2)
        if dd > 0:
            tilt = np.degrees(0.5 * np.arctan2(2 * cxy / dd, (sx**2 - sy**2) / dd))
        else:
            tilt = 0.0

        self._lbl_cx.setText(f"{cx:.2f}")
        self._lbl_cy.setText(f"{cy:.2f}")
        self._lbl_sx.setText(f"{sx:.2f}")
        self._lbl_sy.setText(f"{sy:.2f}")
        self._lbl_cxy.setText(f"{cxy:.4f}")
        self._lbl_tilt.setText(f"{tilt:.2f}")

        if self._to_epics_chk.isChecked():
            self._caput_nonan("centroid_x", cx)
            self._caput_nonan("centroid_y", cy)
            self._caput_nonan("rms_x", sx)
            self._caput_nonan("rms_y", sy)
            self._caput_nonan("total_intensity", total)

        # Long term buffer
        if self._longterm_chk.isChecked():
            n = self._buffer_spin.value()
            thresh_pct = self._longterm_thresh_spin.value() / 100.0
            # Use a threshold centroid for the jitter calc (matching MATLAB)
            d50 = np.where(d >= thresh_pct * peak, d, 0)
            t50 = d50.sum()
            if t50 > 0:
                rho50 = d50 / t50
                cx50 = (xx * rho50.sum(axis=0)).sum()
                cy50 = (yy * rho50.sum(axis=1)).sum()
            else:
                cx50, cy50 = cx, cy

            self._jitter_x_buf.append(cx50)
            self._jitter_y_buf.append(cy50)
            self._sx_buf.append(sx)
            self._sy_buf.append(sy)
            if len(self._jitter_x_buf) > n:
                self._jitter_x_buf.pop(0)
                self._jitter_y_buf.pop(0)
                self._sx_buf.pop(0)
                self._sy_buf.pop(0)

            pct_filled = 100.0 * len(self._jitter_x_buf) / n
            self._lbl_buf_pct.setText(f"{pct_filled:.1f}")

            def _fmt(arr):
                return f"{np.mean(arr):.2f}", f"{np.std(arr):.2f}"

            mx, sx_ = _fmt(self._jitter_x_buf)
            my, sy_ = _fmt(self._jitter_y_buf)
            msx, ssx = _fmt(self._sx_buf)
            msy, ssy = _fmt(self._sy_buf)

            self._lbl_lt_cx.setText(mx);  self._lbl_lt_cx_std.setText(sx_)
            self._lbl_lt_cy.setText(my);  self._lbl_lt_cy_std.setText(sy_)
            self._lbl_lt_sx.setText(msx); self._lbl_lt_sx_std.setText(ssx)
            self._lbl_lt_sy.setText(msy); self._lbl_lt_sy_std.setText(ssy)

    # ------------------------------------------------------------------

    def closeEvent(self, event):
        self._timer.stop()
        self._worker_thread.quit()
        self._worker_thread.wait(2000)
        self.camera.close()
        super().closeEvent(event)
