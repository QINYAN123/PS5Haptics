"""Wuthering Waves motorbike HUD and boost resistance."""
from functools import lru_cache
import math
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.signal import butter, sosfilt

from ds_hid import effect_zones, effect_off
from haptic_dsp import RATE

HUD_TTL = 0.30
BOOST_BAND = (45, 140)  # initial tuning for the low engine/boost sound, in Hz
# Regions at 3840x2160, calibrated on the 2026-11-14 gamepad HUD.
# Templates are normalized edge vectors from the 23:04:23, 23:05:04 and 23:05:20 captures.
HUD_BOXES = {
    'bike': (3016, 1604, 3169, 1732),
    'boost': (3473, 1885, 3621, 2026),
    'r1': (3644, 1927, 3711, 1979),
    'pitch': (3490, 1685, 3608, 1806),
    'l2': (3634, 1505, 3701, 1561),
    'r2': (3642, 1714, 3709, 1769),
}


def hud_feature(image):
    pixels = np.asarray(image.resize((30, 24), Image.Resampling.BILINEAR), dtype=np.float32)
    edges = np.concatenate((np.diff(pixels, axis=0).ravel(), np.diff(pixels, axis=1).ravel()))
    return edges / max(1.0, np.linalg.norm(edges))


@lru_cache(maxsize=1)
def hud_templates():
    with np.load(Path(__file__).resolve().parent / 'models' / 'ww-bike-hud.npz') as data:
        return {name: data[name] for name in HUD_BOXES}


class BikeHud:
    def __init__(self):
        self.templates = hud_templates()
        self.previous = 0.0
        self.state = ('other', 0.0)

    def update(self, frame, pixel, now):
        def score(key):
            x0, y0, x1, y1 = HUD_BOXES[key]
            x0, y0 = pixel(x0, y0)
            x1, y1 = pixel(x1, y1)
            crop = frame[max(0, y0):y1, max(0, x0):x1]
            if crop.size == 0:
                return 0.0
            image = Image.fromarray(crop[..., :3][..., ::-1]).convert('L')
            return float(np.max(self.templates[key] @ hud_feature(image)))

        bike = score('bike') >= 0.50 and score('boost') >= 0.65
        if bike:
            # Suppress combat on the first confirmed bike frame. Remap only after
            # two frames and a verified R1 boost label, including rail glide.
            mode = 'bike'
            if now < self.previous and score('r1') >= 0.72:
                mode = 'air' if score('pitch') >= 0.60 and score('l2') >= 0.72 and score('r2') >= 0.72 else 'boost'
            self.state = (mode, now + HUD_TTL)
        else:
            self.state = ('other', now + HUD_TTL)
        self.previous = now + HUD_TTL if bike else 0.0

    def current(self, now):
        mode, deadline = self.state
        return (mode, deadline) if now < deadline else ('unknown', 0.0)


# USB input report byte 9: physical shoulder/trigger digital bits.
R1_BIT, R2_BIT = 0x02, 0x08
RIGHT_BUTTONS = R1_BIT | R2_BIT


class BikeInput:
    """Run on the HID reader thread. Require neutral before changing the map."""
    def __init__(self):
        self.swapped = False
        self.wait_release = False
        self.boost_pressed = False

    def apply(self, report, swap):
        held = bool((report[9] & RIGHT_BUTTONS) or report[6] > 8)
        if swap != self.swapped:
            self.swapped = swap
            self.wait_release = held
        if not held:
            self.wait_release = False
        self.boost_pressed = swap and not self.wait_release and report[6] >= (24 if self.boost_pressed else 38)
        if not swap and not self.wait_release:
            return report
        mapped = bytearray(report)
        mapped[9] &= ~RIGHT_BUTTONS
        mapped[6] = 0
        if not self.wait_release:
            if report[9] & R1_BIT:
                mapped[9] |= R2_BIT
                mapped[6] = 255
            if self.boost_pressed:
                mapped[9] |= R1_BIT
        return bytes(mapped)


class BoostResistance:
    """Smooth band RMS into bounded R2 spring strength during a physical press."""
    def __init__(self):
        self.filter = butter(2, BOOST_BAND, 'bandpass', fs=RATE, output='sos')
        self.reset()

    def reset(self):
        self.z = np.zeros((len(self.filter), 2, 2))
        self.energy = 0.0
        self.last_audio = None

    def feed(self, left, right, now):
        if not len(left):
            return
        audio = np.column_stack((left, right))
        band, self.z = sosfilt(self.filter, audio, axis=0, zi=self.z)
        rms = float(np.sqrt(np.mean(band * band)))
        seconds = len(left) / RATE
        decay = math.exp(-seconds / (0.05 if rms > self.energy else 0.15))
        self.energy = self.energy * decay + rms * (1.0 - decay)
        self.last_audio = now

    def effect(self, pressed, now, gain):
        if not pressed or gain <= 0:
            return effect_off()
        fresh = self.last_audio is not None and now - self.last_audio < 0.20
        energy = self.energy if fresh and math.isfinite(self.energy) else 0.0
        strength = min(3, round(max(0.0, min(1.0, (energy - 0.008) / 0.067)) * 3))
        zones = (0, 0, 0, 1, 1, 1, 2, 2 + strength // 2, 2 + strength, 2 + strength)
        return effect_zones(round(force * min(1.0, gain)) for force in zones)


DIVE_EFFECT = (0, 0, 0, 1, 1, 1, 2, 2, 2, 3)
