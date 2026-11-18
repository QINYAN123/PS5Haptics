"""Cleans up what the game sees from the real controller: stick deadzones and gyro drift.

Only the report handed to a virtual pad is changed; our own haptics read the raw report.
Offsets (USB report 0x01, id first): 1 LX 2 LY 3 RX 4 RY, 16..21 gyro xyz (int16 LE).
"""
import math
import struct
import time

from button_haptics import BUTTONS

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


# Only controls represented on all three virtual pads appear in the editor.
BUTTON_LABELS = {
    'square': '□', 'cross': '×', 'circle': '○', 'triangle': '△',
    'L1': 'L1', 'R1': 'R1', 'L2': 'L2', 'R2': 'R2', 'L3': 'L3', 'R3': 'R3',
    'up': '↑', 'right': '→', 'down': '↓', 'left': '←', 'create': 'Create', 'options': 'Options',
}
_DPAD = {'up': 1, 'right': 2, 'down': 4, 'left': 8}
_HATS = (1, 3, 2, 6, 4, 12, 8, 9, 0)
_TRIGGERS = {'L2': (5, 4), 'R2': (6, 8)}


class ButtonMap:
    """A permutation from physical controls to virtual controls, fixed for one connection."""

    def __init__(self, saved=None):
        self.bindings = {}
        if isinstance(saved, dict):
            choices = {k: saved.get(k, k) for k in BUTTON_LABELS}
            if all(isinstance(v, str) for v in choices.values()) and set(choices.values()) == set(BUTTON_LABELS):
                self.bindings = {k: v for k, v in choices.items() if k != v}
        self.trigger_down = dict.fromkeys(_TRIGGERS, False)
        self.uses_right_shoulder = 'R1' in self.bindings or 'R2' in self.bindings

    def swap(self, a, b):
        targets = dict(self.bindings)
        targets[a], targets[b] = targets.get(b, b), targets.get(a, a)
        return ButtonMap(targets).bindings

    def trigger_effects(self, left, right, off=None):
        if 'L2' not in self.bindings and 'R2' not in self.bindings:
            return left, right
        effects = {'L2': left, 'R2': right}
        return tuple(effects.get(self.bindings.get(key, key), off) for key in _TRIGGERS)

    def apply(self, report):
        if not self.bindings:
            return report
        out = bytearray(report)
        hat = report[8] & 15
        original_dirs = _HATS[hat] if hat < 8 else 0
        dirs = original_dirs
        for source, target in self.bindings.items():
            if source in _TRIGGERS:
                axis, bit = _TRIGGERS[source]
                level = report[axis]
                pressed = bool(report[9] & bit)
                if target not in _TRIGGERS:
                    pressed = level >= (96 if self.trigger_down[source] else 128)
                    self.trigger_down[source] = pressed
            else:
                if source in _DPAD:
                    pressed = bool(original_dirs & _DPAD[source])
                else:
                    byte, bit = BUTTONS[source][:2]
                    pressed = bool(report[byte] & bit)
                level = 255 if pressed else 0
            if target in _TRIGGERS:
                axis, bit = _TRIGGERS[target]
                out[axis] = level
                byte = 9
            elif target in _DPAD:
                bit = _DPAD[target]
                dirs = dirs | bit if pressed else dirs & ~bit
                continue
            else:
                byte, bit = BUTTONS[target][:2]
            out[byte] = out[byte] | bit if pressed else out[byte] & ~bit
        if dirs != original_dirs:
            if dirs & 5 == 5:  # Opposite directions cancel on each axis.
                dirs &= ~5
            if dirs & 10 == 10:
                dirs &= ~10
            out[8] = (out[8] & 0xF0) | _HATS.index(dirs)
        return bytes(out)


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
