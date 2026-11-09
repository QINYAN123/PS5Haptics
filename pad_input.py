"""Cleans up what the game sees from the real controller: stick deadzones and gyro drift.

Only the report handed to a virtual pad is changed; our own haptics read the raw report.
Offsets (USB report 0x01, id first): 1 LX 2 LY 3 RX 4 RY, 16..21 gyro xyz (int16 LE).
"""
import math
import struct
import time

CAL_MAX_RANGE = 300      # counts (1024 per deg/s) a resting pad may wobble during calibration
CAL_MIN_SAMPLES = 100


def deadzone(x, y, dz):
    """Radial deadzone with rescaling, so the stick still reaches full range and starts from zero."""
    fx, fy = (x - 128) / 127, (y - 128) / 127
    m = math.hypot(fx, fy)
    if m <= dz:
        return 128, 128
    k = (min(m, 1.0) - dz) / (1 - dz) / m
    return (max(0, min(255, round(128 + fx * k * 127))), max(0, min(255, round(128 + fy * k * 127))))


class InputFilter:
    def __init__(self, dz_left=0.0, dz_right=0.0, gyro_bias=(0, 0, 0)):
        self.configure(dz_left, dz_right, gyro_bias)
        self.cal_until = 0.0; self.cal_samples = []; self.cal_result = None

    def configure(self, dz_left, dz_right, gyro_bias):
        self.dz_left = float(dz_left); self.dz_right = float(dz_right)
        self.gyro_bias = tuple(int(v) for v in gyro_bias)

    def calibrate(self, seconds=2.0):
        """Start collecting; cal_result becomes ('ok', bias), ('moved', None) or ('no_data', None)."""
        self.cal_samples = []; self.cal_result = None; self.cal_until = time.monotonic() + seconds

    def _sample(self, r):
        if time.monotonic() < self.cal_until:
            self.cal_samples.append(struct.unpack_from('<3h', bytes(r), 16))
            return
        self.cal_until = 0.0
        s = self.cal_samples
        if len(s) < CAL_MIN_SAMPLES:
            self.cal_result = ('no_data', None)
        elif any(max(v[a] for v in s) - min(v[a] for v in s) > CAL_MAX_RANGE for a in range(3)):
            self.cal_result = ('moved', None)
        else:
            self.cal_result = ('ok', tuple(sorted(v[a] for v in s)[len(s) // 2] for a in range(3)))

    def apply(self, r):
        if self.cal_until:
            self._sample(r)
        if not (self.dz_left or self.dz_right or any(self.gyro_bias)):
            return r
        b = bytearray(r)
        if self.dz_left:
            b[1], b[2] = deadzone(b[1], b[2], self.dz_left)
        if self.dz_right:
            b[3], b[4] = deadzone(b[3], b[4], self.dz_right)
        if any(self.gyro_bias):
            g = struct.unpack_from('<3h', b, 16)
            struct.pack_into('<3h', b, 16, *(max(-32768, min(32767, v - o)) for v, o in zip(g, self.gyro_bias)))
        return bytes(b)
