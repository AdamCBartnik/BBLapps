import time
import numpy as np
from .base import CameraBase


class MockCamera(CameraBase):
    """
    Synthetic camera that generates a drifting Gaussian beam image.
    Useful for UI development without a real EPICS connection.
    """

    def __init__(self, width=640, height=480, bits=12):
        self._width_max = width
        self._height_max = height
        self._width = width
        self._height = height
        self._offset_x = 0
        self._offset_y = 0
        self._exposure_time = 0.01
        self._gain = 1.0
        self._bits = bits
        self._frame = 0

    @property
    def width(self): return self._width

    @property
    def height(self): return self._height

    @property
    def width_max(self): return self._width_max

    @property
    def height_max(self): return self._height_max

    @property
    def offset_x(self): return self._offset_x

    @property
    def offset_y(self): return self._offset_y

    @property
    def exposure_time(self): return self._exposure_time

    @exposure_time.setter
    def exposure_time(self, value): self._exposure_time = max(1e-6, float(value))

    @property
    def gain(self): return self._gain

    @gain.setter
    def gain(self, value): self._gain = max(0.0, float(value))

    @property
    def bits(self): return self._bits

    def snapshot(self) -> np.ndarray:
        time.sleep(self._exposure_time)
        self._frame += 1
        t = self._frame * 0.05

        cx = self._width / 2 + 30 * np.sin(t * 0.3)
        cy = self._height / 2 + 20 * np.sin(t * 0.2 + 1.0)
        sx = self._width * 0.08
        sy = self._height * 0.06

        y, x = np.ogrid[:self._height, :self._width]
        beam = np.exp(-0.5 * ((x - cx) / sx) ** 2 - 0.5 * ((y - cy) / sy) ** 2)

        peak = self.max_value * 0.8 * self._gain * (self._exposure_time / 0.01)
        noise = np.random.normal(0, self.max_value * 0.002, beam.shape)
        image = np.clip(beam * peak + noise, 0, self.max_value)
        return image.astype(np.uint16)
