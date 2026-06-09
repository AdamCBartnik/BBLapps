from abc import ABC, abstractmethod
import numpy as np


class CameraBase(ABC):
    """Abstract base class for all camera backends."""

    @property
    @abstractmethod
    def width(self) -> int:
        """Current frame width in pixels."""

    @property
    @abstractmethod
    def height(self) -> int:
        """Current frame height in pixels."""

    @property
    @abstractmethod
    def width_max(self) -> int:
        """Maximum sensor width in pixels."""

    @property
    @abstractmethod
    def height_max(self) -> int:
        """Maximum sensor height in pixels."""

    @property
    @abstractmethod
    def offset_x(self) -> int:
        """Horizontal ROI offset in pixels."""

    @property
    @abstractmethod
    def offset_y(self) -> int:
        """Vertical ROI offset in pixels."""

    @property
    @abstractmethod
    def exposure_time(self) -> float:
        """Exposure time in seconds."""

    @exposure_time.setter
    @abstractmethod
    def exposure_time(self, value: float):
        pass

    @property
    @abstractmethod
    def gain(self) -> float:
        """Analogue gain."""

    @gain.setter
    @abstractmethod
    def gain(self, value: float):
        pass

    @property
    @abstractmethod
    def bits(self) -> int:
        """Bits per pixel."""

    @property
    def max_value(self) -> int:
        return 2 ** self.bits - 1

    @abstractmethod
    def snapshot(self) -> np.ndarray:
        """Capture and return a 2-D array of shape (height, width)."""

    def has_new_frame(self) -> bool:
        """Return True if a new frame is available since the last call.
        Default always returns True — suitable for cameras without a separate
        readiness signal. Override for EPICS cameras that monitor a timestamp PV."""
        return True

    def set_roi(self, x: int, y: int, w: int, h: int):
        """Set hardware ROI. Override for cameras that support it."""

    def get_roi(self) -> tuple:
        """Return current ROI as (x, y, w, h). Default reads offset/width/height properties."""
        return self.offset_x, self.offset_y, self.width, self.height

    def start_streaming(self, rate_hz: float = 5.0):
        """Begin continuous frame capture. Override for cameras that need it."""

    def stop_streaming(self):
        """Stop continuous frame capture. Override for cameras that need it."""

    def close(self):
        """Release resources. Override if needed."""
