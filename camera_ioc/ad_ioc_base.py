"""
ad_ioc_base.py — shared contract module for all camera IOCs.

Defines the one PV surface that every camera IOC in this repo serves,
modeled on the genuine EPICS areaDetector record set (verified against
ADCore's ADDriver docs and NDArrayBase.template), so that any standard
areaDetector client works against our IOCs unmodified.

Architecture
------------
    ADCameraIOC (this module)         <- PV surface + acquisition loop
        |
    CameraDriver (abstract)           <- hardware access, one per backend
        |
        +-- picamera2 driver          (Pi cameras: IMX708, IMX296, IMX296 mono)
        +-- Harvester driver          (GigE Vision cameras via GenTL)
        +-- CA-relay driver           (gateway: "camera" is another IOC)

Concrete IOCs are built with build_ioc_class(MyDriver) and served with
caproto's normal run()/ioc_arg_parser machinery.

PV surface (prefix = e.g. "VPCAM:02", served as "<prefix>:cam1:..." )
---------------------------------------------------------------------
Standard areaDetector records (core contract — all clients may rely on these):

    cam1:Acquire / Acquire_RBV          enum Done/Acquire
    cam1:ImageMode / ImageMode_RBV      enum Single/Multiple/Continuous
    cam1:NumImages / NumImages_RBV      frames per Acquire in Multiple mode
    cam1:AcquireTime / _RBV             exposure time, seconds
    cam1:AcquirePeriod / _RBV           seconds/frame in Continuous mode
    cam1:Gain / Gain_RBV
    cam1:MinX / MinY / SizeX / SizeY (+ _RBV)   ROI, sensor coordinates
    cam1:MaxSizeX_RBV / MaxSizeY_RBV    full sensor size
    cam1:ArraySizeX_RBV / ArraySizeY_RBV  current frame size
    cam1:DataType / DataType_RBV        enum (UInt8, UInt16, ...)
    cam1:ColorMode / ColorMode_RBV      enum (Mono, ...)
    cam1:ArrayCounter / _RBV            frames acquired since reset
    cam1:ArrayRate_RBV                  Hz, smoothed
    cam1:Manufacturer_RBV / Model_RBV
    image1:ArrayData                    image waveform, uint16, fixed NELM =
                                        MaxSizeX*MaxSizeY, active w*h prefix
                                        (matches genuine NDStdArrays behavior)
    image1:ArrayCounter_RBV             frames published — THE new-frame
                                        monitor PV for all clients
    image1:UniqueId_RBV                 frame id (detects dropped frames)
    image1:TimeStamp_RBV                unix time of frame capture
    image1:ArraySize0_RBV / ArraySize1_RBV   width / height of active frame
    image1:NDimensions_RBV              always 2
    image1:DataType_RBV / ColorMode_RBV

House extension records (not in areaDetector — clients must treat as optional):

    cam1:BitsPerPixel_RBV               true sensor bit depth (e.g. 10 in a
                                        16-bit container; AD has no record
                                        for this)
    cam1:CalibX / CalibY                micron-per-pixel calibration, stored
                                        IOC-side (driver may persist)

Device-specific extension records (LED, lens position, CPU temp, ...) are
declared by each driver via CameraDriver.extension_pvs and served under
cam1: alongside the rest.
"""

from __future__ import annotations

import abc
import asyncio
import enum
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from caproto.server import PVGroup, pvproperty


CONTRACT_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Enums (values and order match areaDetector's ADBase / NDArrayBase)
# ---------------------------------------------------------------------------

class AcquireState(enum.IntEnum):
    Done = 0
    Acquire = 1


class ImageMode(enum.IntEnum):
    Single = 0
    Multiple = 1
    Continuous = 2


class ADDataType(enum.IntEnum):
    Int8 = 0
    UInt8 = 1
    Int16 = 2
    UInt16 = 3
    Int32 = 4
    UInt32 = 5
    Int64 = 6
    UInt64 = 7
    Float32 = 8
    Float64 = 9


class ADColorMode(enum.IntEnum):
    Mono = 0
    Bayer = 1
    RGB1 = 2
    RGB2 = 3
    RGB3 = 4
    YUV444 = 5
    YUV422 = 6
    YUV421 = 7


def to_enum(value: Any, enum_cls: type[enum.IntEnum]) -> enum.IntEnum:
    """Coerce a caproto channel value to an IntEnum member.

    caproto enum channels surface values variously as ints, member-name
    strings ('Acquire'), or stringified members ('AcquireState.Acquire')
    depending on code path — normalize all of them.
    """
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, (int, np.integer)):
        return enum_cls(int(value))
    s = str(value).strip()
    if "." in s:
        s = s.rsplit(".", 1)[-1]
    try:
        return enum_cls[s]
    except KeyError:
        return enum_cls(int(s))


# ---------------------------------------------------------------------------
# Driver interface
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExtensionPV:
    """One device-specific PV served under cam1: alongside the standard set.

    getter/setter receive the driver instance:
        getter(driver) -> value
        setter(driver, value) -> applied value (or None to accept as-is)
    A getter with poll_period > 0 is polled on that interval and its value
    published; otherwise the getter is called once at startup.
    """
    name: str                      # PV suffix after "cam1:", e.g. "LedEnable"
    dtype: type                    # int, float, str, or an IntEnum class
    initial: Any
    doc: str = ""
    read_only: bool = False
    getter: Callable | None = None
    setter: Callable | None = None
    poll_period: float = 0.0
    max_length: int = 1            # >1 only for string PVs needing length


class CameraDriver(abc.ABC):
    """Hardware-access interface consumed by ADCameraIOC.

    All methods are called from worker threads (via asyncio.to_thread), so
    blocking implementations are fine.  Mirrors beamview's CameraBase ABC
    where the concepts overlap.
    """

    manufacturer: str = ""
    model: str = ""

    #: device-specific PVs, declared at class level
    extension_pvs: list[ExtensionPV] = []

    # -- lifecycle ----------------------------------------------------------

    def open(self) -> None:
        """Connect to the hardware. Called once before the IOC starts."""

    def close(self) -> None:
        """Release the hardware."""

    # -- geometry -----------------------------------------------------------

    @property
    @abc.abstractmethod
    def sensor_width(self) -> int:
        """Full sensor width in pixels (MaxSizeX)."""

    @property
    @abc.abstractmethod
    def sensor_height(self) -> int:
        """Full sensor height in pixels (MaxSizeY)."""

    @property
    def max_frame_pixels(self) -> int:
        """Upper bound on frame pixel count, used as ArrayData NELM.

        Override if the camera has modes larger than the current
        sensor_width*sensor_height (e.g. IMX708 booted in binned mode).
        """
        return self.sensor_width * self.sensor_height

    @abc.abstractmethod
    def get_roi(self) -> tuple[int, int, int, int]:
        """Return the active ROI as (min_x, min_y, size_x, size_y)."""

    @abc.abstractmethod
    def set_roi(self, min_x: int, min_y: int, size_x: int, size_y: int
                ) -> tuple[int, int, int, int]:
        """Apply an ROI; return the actually-applied (clamped) values."""

    # -- exposure / gain ----------------------------------------------------

    @property
    @abc.abstractmethod
    def exposure_time(self) -> float:
        """Exposure time in seconds."""

    @exposure_time.setter
    @abc.abstractmethod
    def exposure_time(self, seconds: float) -> None: ...

    @property
    @abc.abstractmethod
    def gain(self) -> float: ...

    @gain.setter
    @abc.abstractmethod
    def gain(self, value: float) -> None: ...

    # -- pixel format -------------------------------------------------------

    @property
    @abc.abstractmethod
    def bits_per_pixel(self) -> int:
        """True sensor bit depth (10 for IMX296, 8 for IMX708, ...)."""

    @property
    def data_type(self) -> ADDataType:
        return ADDataType.UInt16

    @property
    def color_mode(self) -> ADColorMode:
        return ADColorMode.Mono

    # -- acquisition --------------------------------------------------------

    @abc.abstractmethod
    def capture(self) -> np.ndarray:
        """Blocking: acquire and return one frame, shape (h, w), uint16.

        May return None to indicate no new frame became available yet
        (event-driven drivers, e.g. the CA gateway); the acquisition loop
        skips publishing and calls capture() again."""

    def on_acquire_start(self) -> None:
        """Called when the acquisition loop starts (e.g. start camera stream)."""

    def on_acquire_stop(self) -> None:
        """Called when the acquisition loop stops."""

    # -- calibration persistence (optional) ----------------------------------

    def load_calibration(self) -> tuple[float, float] | None:
        """Return (cal_x, cal_y) in um/pixel, or None if not persisted."""
        return None

    def save_calibration(self, cal_x: float, cal_y: float) -> None:
        """Persist calibration; default does nothing (PV value only)."""


# ---------------------------------------------------------------------------
# IOC base
# ---------------------------------------------------------------------------

def _ext_kwargs(spec: ExtensionPV) -> dict:
    kwargs = dict(name=f"cam1:{spec.name}", value=spec.initial, doc=spec.doc)
    if spec.dtype is str:
        kwargs["max_length"] = max(spec.max_length, 256)
    elif not issubclass(spec.dtype, enum.IntEnum):
        kwargs["dtype"] = spec.dtype
    return kwargs


def _make_extension_property(spec: ExtensionPV):
    if spec.read_only:
        return pvproperty(read_only=True, **_ext_kwargs(spec))

    prop = pvproperty(**_ext_kwargs(spec))

    @prop.putter
    async def _putter(self, instance, value, _spec=spec):
        if _spec.setter is not None:
            applied = await asyncio.to_thread(_spec.setter, self.driver, value)
            # Extension writes can change geometry or exposure as a side
            # effect (sensor mode switch, AE handoff) — refresh readbacks.
            await self._refresh_readbacks()
            if applied is not None:
                return applied
        return value

    return prop


class ADCameraIOCBase(PVGroup):
    """Standard-areaDetector PV surface in front of a CameraDriver.

    Do not instantiate directly — use build_ioc_class(driver_cls) so the
    driver's extension PVs are included at class-creation time.
    """

    # -- cam1: acquisition control -------------------------------------------

    cam1_Acquire = pvproperty(name="cam1:Acquire", value=AcquireState.Done,
                              doc="Start/stop acquisition")
    cam1_Acquire_RBV = pvproperty(name="cam1:Acquire_RBV",
                                  value=AcquireState.Done, read_only=True)

    cam1_ImageMode = pvproperty(name="cam1:ImageMode",
                                value=ImageMode.Continuous,
                                doc="Single / Multiple / Continuous")
    cam1_ImageMode_RBV = pvproperty(name="cam1:ImageMode_RBV",
                                    value=ImageMode.Continuous, read_only=True)

    cam1_NumImages = pvproperty(name="cam1:NumImages", value=1, dtype=int,
                                doc="Frames per Acquire in Multiple mode")
    cam1_NumImages_RBV = pvproperty(name="cam1:NumImages_RBV", value=1,
                                    dtype=int, read_only=True)

    cam1_AcquireTime = pvproperty(name="cam1:AcquireTime", value=0.01,
                                  dtype=float, doc="Exposure time (s)")
    cam1_AcquireTime_RBV = pvproperty(name="cam1:AcquireTime_RBV", value=0.01,
                                      dtype=float, read_only=True)

    cam1_AcquirePeriod = pvproperty(name="cam1:AcquirePeriod", value=0.2,
                                    dtype=float,
                                    doc="Seconds/frame in Continuous mode")
    cam1_AcquirePeriod_RBV = pvproperty(name="cam1:AcquirePeriod_RBV",
                                        value=0.2, dtype=float, read_only=True)

    cam1_Gain = pvproperty(name="cam1:Gain", value=1.0, dtype=float)
    cam1_Gain_RBV = pvproperty(name="cam1:Gain_RBV", value=1.0, dtype=float,
                               read_only=True)

    # -- cam1: ROI ------------------------------------------------------------

    cam1_MinX = pvproperty(name="cam1:MinX", value=0, dtype=int)
    cam1_MinX_RBV = pvproperty(name="cam1:MinX_RBV", value=0, dtype=int,
                               read_only=True)
    cam1_MinY = pvproperty(name="cam1:MinY", value=0, dtype=int)
    cam1_MinY_RBV = pvproperty(name="cam1:MinY_RBV", value=0, dtype=int,
                               read_only=True)
    cam1_SizeX = pvproperty(name="cam1:SizeX", value=1, dtype=int)
    cam1_SizeX_RBV = pvproperty(name="cam1:SizeX_RBV", value=1, dtype=int,
                                read_only=True)
    cam1_SizeY = pvproperty(name="cam1:SizeY", value=1, dtype=int)
    cam1_SizeY_RBV = pvproperty(name="cam1:SizeY_RBV", value=1, dtype=int,
                                read_only=True)

    cam1_MaxSizeX_RBV = pvproperty(name="cam1:MaxSizeX_RBV", value=0,
                                   dtype=int, read_only=True)
    cam1_MaxSizeY_RBV = pvproperty(name="cam1:MaxSizeY_RBV", value=0,
                                   dtype=int, read_only=True)
    cam1_ArraySizeX_RBV = pvproperty(name="cam1:ArraySizeX_RBV", value=0,
                                     dtype=int, read_only=True)
    cam1_ArraySizeY_RBV = pvproperty(name="cam1:ArraySizeY_RBV", value=0,
                                     dtype=int, read_only=True)

    # -- cam1: format / info ---------------------------------------------------

    cam1_DataType = pvproperty(name="cam1:DataType", value=ADDataType.UInt16,
                               doc="Array data type (driver-determined)")
    cam1_DataType_RBV = pvproperty(name="cam1:DataType_RBV",
                                   value=ADDataType.UInt16, read_only=True)
    cam1_ColorMode = pvproperty(name="cam1:ColorMode", value=ADColorMode.Mono)
    cam1_ColorMode_RBV = pvproperty(name="cam1:ColorMode_RBV",
                                    value=ADColorMode.Mono, read_only=True)

    cam1_ArrayCounter = pvproperty(name="cam1:ArrayCounter", value=0,
                                   dtype=int,
                                   doc="Frame counter; write 0 to reset")
    cam1_ArrayCounter_RBV = pvproperty(name="cam1:ArrayCounter_RBV", value=0,
                                       dtype=int, read_only=True)
    cam1_ArrayRate_RBV = pvproperty(name="cam1:ArrayRate_RBV", value=0.0,
                                    dtype=float, read_only=True,
                                    doc="Acquisition rate (Hz), smoothed")

    cam1_Manufacturer_RBV = pvproperty(name="cam1:Manufacturer_RBV", value="",
                                       max_length=256, read_only=True)
    cam1_Model_RBV = pvproperty(name="cam1:Model_RBV", value="",
                                max_length=256, read_only=True)

    # -- cam1: house extensions (always present) -------------------------------

    cam1_BitsPerPixel_RBV = pvproperty(
        name="cam1:BitsPerPixel_RBV", value=16, dtype=int, read_only=True,
        doc="EXTENSION: true sensor bit depth within the 16-bit container")

    cam1_CalibX = pvproperty(name="cam1:CalibX", value=1.0, dtype=float,
                             doc="EXTENSION: X calibration, um/pixel")
    cam1_CalibY = pvproperty(name="cam1:CalibY", value=1.0, dtype=float,
                             doc="EXTENSION: Y calibration, um/pixel")

    # -- image1: NDStdArrays-equivalent ----------------------------------------

    # NOTE: value is replaced with a correctly-sized buffer in __init__;
    # the class-level 1-element default is a placeholder.
    image1_ArrayData = pvproperty(
        name="image1:ArrayData", value=[0], dtype=int, max_length=1,
        read_only=True,
        doc="Image waveform, uint16; active w*h prefix of a fixed-NELM buffer")

    image1_ArrayCounter_RBV = pvproperty(
        name="image1:ArrayCounter_RBV", value=0, dtype=int, read_only=True,
        doc="Frames published — monitor this PV for new-frame detection")
    image1_UniqueId_RBV = pvproperty(name="image1:UniqueId_RBV", value=0,
                                     dtype=int, read_only=True)
    image1_TimeStamp_RBV = pvproperty(name="image1:TimeStamp_RBV", value=0.0,
                                      dtype=float, read_only=True,
                                      doc="Unix time of frame capture")
    image1_ArraySize0_RBV = pvproperty(name="image1:ArraySize0_RBV", value=0,
                                       dtype=int, read_only=True,
                                       doc="Active frame width (px)")
    image1_ArraySize1_RBV = pvproperty(name="image1:ArraySize1_RBV", value=0,
                                       dtype=int, read_only=True,
                                       doc="Active frame height (px)")
    image1_NDimensions_RBV = pvproperty(name="image1:NDimensions_RBV", value=2,
                                        dtype=int, read_only=True)
    image1_DataType_RBV = pvproperty(name="image1:DataType_RBV",
                                     value=ADDataType.UInt16, read_only=True)
    image1_ColorMode_RBV = pvproperty(name="image1:ColorMode_RBV",
                                      value=ADColorMode.Mono, read_only=True)

    # ==========================================================================

    def __init__(self, driver: CameraDriver, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.driver = driver
        self._acq_task: asyncio.Task | None = None
        self._poll_tasks: list[asyncio.Task] = []
        self._rate_ema: float | None = None
        self._last_frame_time: float | None = None

        n = driver.max_frame_pixels
        self._frame_buffer = np.zeros(n, dtype=np.uint16)
        # Replace the placeholder ArrayData channel data with the real
        # fixed-length buffer before the server starts.
        self.image1_ArrayData._data["value"] = self._frame_buffer.copy()
        self.image1_ArrayData._max_length = n

    # -- startup ----------------------------------------------------------------

    async def startup(self):
        """Initialize PVs from hardware. Call from a caproto startup hook."""
        d = self.driver
        await asyncio.to_thread(d.open)

        await self.cam1_Manufacturer_RBV.write(d.manufacturer)
        await self.cam1_Model_RBV.write(d.model)
        await self.cam1_MaxSizeX_RBV.write(d.sensor_width)
        await self.cam1_MaxSizeY_RBV.write(d.sensor_height)
        await self.cam1_BitsPerPixel_RBV.write(d.bits_per_pixel)
        await self.cam1_DataType.write(d.data_type)
        await self.cam1_DataType_RBV.write(d.data_type)
        await self.image1_DataType_RBV.write(d.data_type)
        await self.cam1_ColorMode.write(d.color_mode)
        await self.cam1_ColorMode_RBV.write(d.color_mode)
        await self.image1_ColorMode_RBV.write(d.color_mode)

        x, y, w, h = await asyncio.to_thread(d.get_roi)
        await self._publish_roi(x, y, w, h)

        await self.cam1_AcquireTime.write(d.exposure_time)
        await self.cam1_AcquireTime_RBV.write(d.exposure_time)
        await self.cam1_Gain.write(d.gain)
        await self.cam1_Gain_RBV.write(d.gain)

        cal = await asyncio.to_thread(d.load_calibration)
        if cal is not None:
            await self.cam1_CalibX.write(cal[0])
            await self.cam1_CalibY.write(cal[1])

        # Initial values + polling for extension PVs
        for spec in type(d).extension_pvs:
            prop = getattr(self, f"ext_{spec.name}")
            if spec.getter is not None:
                try:
                    val = await asyncio.to_thread(spec.getter, d)
                    await prop.write(val)
                except Exception as exc:
                    print(f"[ad_ioc] extension {spec.name} initial read: {exc}")
                if spec.poll_period > 0:
                    self._poll_tasks.append(asyncio.create_task(
                        self._poll_extension(spec, prop)))

        print(f"[ad_ioc] contract v{CONTRACT_VERSION} ready: "
              f"{d.manufacturer} {d.model}, "
              f"{d.sensor_width}x{d.sensor_height}, "
              f"{d.bits_per_pixel}-bit")

    async def shutdown(self):
        if self._acq_task is not None:
            self._acq_task.cancel()
        for t in self._poll_tasks:
            t.cancel()
        await asyncio.to_thread(self.driver.close)

    async def _refresh_readbacks(self):
        """Re-publish geometry and exposure/gain readbacks from the driver.

        Called after extension-PV writes, whose side effects (sensor-mode
        switch, AE handoff) can change these without going through the
        standard putters.
        """
        d = self.driver
        try:
            await self.cam1_MaxSizeX_RBV.write(d.sensor_width)
            await self.cam1_MaxSizeY_RBV.write(d.sensor_height)
            x, y, w, h = await asyncio.to_thread(d.get_roi)
            await self._publish_roi(x, y, w, h)
            await self.cam1_AcquireTime_RBV.write(d.exposure_time)
            await self.cam1_Gain_RBV.write(d.gain)
        except Exception as exc:
            print(f"[ad_ioc] readback refresh failed: {exc}")

    async def _poll_extension(self, spec: ExtensionPV, prop):
        while True:
            await asyncio.sleep(spec.poll_period)
            try:
                val = await asyncio.to_thread(spec.getter, self.driver)
                await prop.write(val)
            except Exception as exc:
                print(f"[ad_ioc] extension {spec.name} poll: {exc}")

    # -- putters ------------------------------------------------------------------

    @cam1_Acquire.putter
    async def cam1_Acquire(self, instance, value):
        state = to_enum(value, AcquireState)
        if state == AcquireState.Acquire:
            if self._acq_task is None or self._acq_task.done():
                self._acq_task = asyncio.create_task(self._acquire_loop())
        else:
            if self._acq_task is not None:
                self._acq_task.cancel()
        await self.cam1_Acquire_RBV.write(state)
        return state

    @cam1_ImageMode.putter
    async def cam1_ImageMode(self, instance, value):
        await self.cam1_ImageMode_RBV.write(value)
        return value

    @cam1_NumImages.putter
    async def cam1_NumImages(self, instance, value):
        v = max(1, int(value))
        await self.cam1_NumImages_RBV.write(v)
        return v

    @cam1_AcquireTime.putter
    async def cam1_AcquireTime(self, instance, value):
        def _set():
            self.driver.exposure_time = float(value)
            return self.driver.exposure_time
        applied = await asyncio.to_thread(_set)
        await self.cam1_AcquireTime_RBV.write(applied)
        return applied

    @cam1_AcquirePeriod.putter
    async def cam1_AcquirePeriod(self, instance, value):
        v = max(0.0, float(value))
        await self.cam1_AcquirePeriod_RBV.write(v)
        return v

    @cam1_Gain.putter
    async def cam1_Gain(self, instance, value):
        def _set():
            self.driver.gain = float(value)
            return self.driver.gain
        applied = await asyncio.to_thread(_set)
        await self.cam1_Gain_RBV.write(applied)
        return applied

    @cam1_ArrayCounter.putter
    async def cam1_ArrayCounter(self, instance, value):
        v = int(value)
        await self.cam1_ArrayCounter_RBV.write(v)
        return v

    @cam1_DataType.putter
    async def cam1_DataType(self, instance, value):
        # Data type is driver-determined; accept writes but reflect reality.
        return self.driver.data_type

    @cam1_ColorMode.putter
    async def cam1_ColorMode(self, instance, value):
        return self.driver.color_mode

    @cam1_CalibX.putter
    async def cam1_CalibX(self, instance, value):
        v = float(value)
        await asyncio.to_thread(self.driver.save_calibration,
                                v, self.cam1_CalibY.value)
        return v

    @cam1_CalibY.putter
    async def cam1_CalibY(self, instance, value):
        v = float(value)
        await asyncio.to_thread(self.driver.save_calibration,
                                self.cam1_CalibX.value, v)
        return v

    # ROI: each putter stages its value, then applies the full rectangle.
    # Standard AD semantics — the driver clamps, readbacks show reality.

    async def _apply_roi(self, x, y, w, h):
        applied = await asyncio.to_thread(self.driver.set_roi,
                                          int(x), int(y), int(w), int(h))
        await self._publish_roi(*applied)
        return applied

    async def _publish_roi(self, x, y, w, h):
        await self.cam1_MinX_RBV.write(x)
        await self.cam1_MinY_RBV.write(y)
        await self.cam1_SizeX_RBV.write(w)
        await self.cam1_SizeY_RBV.write(h)
        await self.cam1_ArraySizeX_RBV.write(w)
        await self.cam1_ArraySizeY_RBV.write(h)

    @cam1_MinX.putter
    async def cam1_MinX(self, instance, value):
        applied = await self._apply_roi(value, self.cam1_MinY.value,
                                        self.cam1_SizeX.value,
                                        self.cam1_SizeY.value)
        return applied[0]

    @cam1_MinY.putter
    async def cam1_MinY(self, instance, value):
        applied = await self._apply_roi(self.cam1_MinX.value, value,
                                        self.cam1_SizeX.value,
                                        self.cam1_SizeY.value)
        return applied[1]

    @cam1_SizeX.putter
    async def cam1_SizeX(self, instance, value):
        applied = await self._apply_roi(self.cam1_MinX.value,
                                        self.cam1_MinY.value, value,
                                        self.cam1_SizeY.value)
        return applied[2]

    @cam1_SizeY.putter
    async def cam1_SizeY(self, instance, value):
        applied = await self._apply_roi(self.cam1_MinX.value,
                                        self.cam1_MinY.value,
                                        self.cam1_SizeX.value, value)
        return applied[3]

    # -- acquisition loop ----------------------------------------------------------

    async def _acquire_loop(self):
        mode = to_enum(self.cam1_ImageMode.value, ImageMode)
        n_target = (1 if mode == ImageMode.Single
                    else int(self.cam1_NumImages.value)
                    if mode == ImageMode.Multiple else None)
        n_done = 0
        try:
            await asyncio.to_thread(self.driver.on_acquire_start)
            while True:
                t0 = time.monotonic()
                img = await asyncio.to_thread(self.driver.capture)
                if img is None:
                    continue   # no new frame from the driver yet; retry
                await self._publish_frame(img)
                n_done += 1
                if n_target is not None and n_done >= n_target:
                    break
                period = float(self.cam1_AcquirePeriod.value)
                remaining = period - (time.monotonic() - t0)
                if remaining > 0:
                    await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[ad_ioc] acquisition stopped: {exc}")
        finally:
            try:
                await asyncio.to_thread(self.driver.on_acquire_stop)
            except Exception:
                pass
            await self.cam1_Acquire_RBV.write(AcquireState.Done)
            if to_enum(self.cam1_Acquire.value, AcquireState) != AcquireState.Done:
                # Loop ended on its own (Single/Multiple complete or error)
                await self.cam1_Acquire.write(AcquireState.Done)

    async def _publish_frame(self, img: np.ndarray):
        h, w = img.shape
        flat = np.ascontiguousarray(img, dtype=np.uint16).reshape(-1)

        n = flat.size
        self._frame_buffer.fill(0)
        ncopy = min(n, self._frame_buffer.size)
        self._frame_buffer[:ncopy] = flat[:ncopy]

        now = time.time()
        counter = int(self.cam1_ArrayCounter_RBV.value) + 1

        await self.image1_ArrayData.write(self._frame_buffer.copy())
        await self.image1_ArraySize0_RBV.write(w)
        await self.image1_ArraySize1_RBV.write(h)
        await self.image1_TimeStamp_RBV.write(now)
        await self.image1_UniqueId_RBV.write(counter)
        await self.cam1_ArrayCounter_RBV.write(counter)
        await self.cam1_ArraySizeX_RBV.write(w)
        await self.cam1_ArraySizeY_RBV.write(h)
        # New-frame monitor PV last, so all metadata is consistent when it fires
        await self.image1_ArrayCounter_RBV.write(counter)

        # Smoothed rate
        if self._last_frame_time is not None:
            dt = now - self._last_frame_time
            if dt > 0:
                inst = 1.0 / dt
                self._rate_ema = (inst if self._rate_ema is None
                                  else 0.8 * self._rate_ema + 0.2 * inst)
                await self.cam1_ArrayRate_RBV.write(self._rate_ema)
        self._last_frame_time = now


def build_ioc_class(driver_cls: type[CameraDriver]) -> type[ADCameraIOCBase]:
    """Build a concrete IOC class including driver_cls's extension PVs.

    Usage:
        IOCClass = build_ioc_class(MyDriver)
        ioc = IOCClass(driver=MyDriver(...), **ioc_options)
    """
    members = {
        f"ext_{spec.name}": _make_extension_property(spec)
        for spec in driver_cls.extension_pvs
    }
    return type(f"ADCameraIOC_{driver_cls.__name__}",
                (ADCameraIOCBase,), members)
