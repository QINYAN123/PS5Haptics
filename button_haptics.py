"""Control haptics: a short tap on the grip coil when a button, trigger, stick or the touchpad is used.

ControlTracker turns DualSense USB input reports into events (it runs on whichever thread reads the
pad). ButtonClicks mixes short decaying sine bursts into the 48 kHz coil output.
Triggers use the analog value with hysteresis: their digital bit fires at the slightest pressure and chatters.
"""
from collections import deque
import math

import numpy as np

from haptic_dsp import RATE

# name: (report byte, bit mask, side, click kind). L/R = that grip, both = both grips.
BUTTONS = {
    'square': (8, 0x10, 'R', 'face'), 'cross': (8, 0x20, 'R', 'face'), 'circle': (8, 0x40, 'R', 'face'), 'triangle': (8, 0x80, 'R', 'face'),
    'L1': (9, 0x01, 'L', 'shoulder'), 'R1': (9, 0x02, 'R', 'shoulder'), 'create': (9, 0x10, 'L', 'small'), 'options': (9, 0x20, 'R', 'small'),
    'L3': (9, 0x40, 'L', 'shoulder'), 'R3': (9, 0x80, 'R', 'shoulder'),
    'PS': (10, 0x01, 'both', 'small'), 'touchpad': (10, 0x02, 'both', 'face'), 'mute': (10, 0x04, 'both', 'small'),
}
KINDS = {'face': (150.0, 0.012, 0.75), 'shoulder': (115.0, 0.018, 0.85), 'dpad': (185.0, 0.009, 0.65), 'small': (185.0, 0.007, 0.5),
         'trigger': (130.0, 0.014, 0.6), 'stick': (205.0, 0.006, 0.4)}   # Hz, decay s, level
RELEASE = 0.4          # releases are lighter than presses
OTHER_GRIP = 0.18      # a little of the click reaches the other hand through the shell
MAX_VOICES = 8         # mashing buttons must not build up into a buzz
TRIGGER_ON, TRIGGER_OFF = 150, 100     # of 255
STICK_ON, STICK_OFF = 0.85, 0.6        # radius


def _click(kind):
    hz, decay, level = KINDS[kind]
    n = int(RATE * decay * 5)
    t = np.arange(n) / RATE
    return (np.sin(2 * np.pi * hz * t) * np.exp(-t / decay) * level * np.minimum(1.0, t / 0.001)).astype(np.float32)


WAVES = {k: _click(k) for k in KINDS}


class ControlTracker:
    """update(report) on the reader thread, drain() on the audio thread; a bounded deque is safe for that.
    Events: (group, side, kind, pressed), group = button / trigger / stick / touch."""

    def __init__(self):
        self.prev = None; self.events = deque(maxlen=64)
        self.trig = {'L': False, 'R': False}; self.stick = {'L': False, 'R': False}

    def update(self, r):
        if len(r) < 11:
            return
        state = {name: bool(r[b] & mask) for name, (b, mask, _s, _k) in BUTTONS.items()}
        hat = r[8] & 0x0F
        state['dpad'] = hat if hat < 8 else None
        fingers = (not r[33] & 0x80, not r[37] & 0x80) if len(r) >= 38 else (False, False)
        state['fingers'] = fingers
        prev, self.prev = self.prev, state
        if prev is None:                # whatever is held when the app starts is not a press
            self._levels(r, False)
            return
        for name, (_b, _m, side, kind) in BUTTONS.items():
            if state[name] != prev[name]:
                self.events.append(('button', side, kind, state[name]))
        if state['dpad'] != prev['dpad']:
            self.events.append(('button', 'L', 'dpad', state['dpad'] is not None))
        for now, before in zip(fingers, prev['fingers']):
            if now and not before:
                self.events.append(('touch', 'both', 'small', True))
        self._levels(r, True)

    def _levels(self, r, emit):
        for side, v in (('L', r[5]), ('R', r[6])):
            on = v >= TRIGGER_ON if not self.trig[side] else v > TRIGGER_OFF
            if on != self.trig[side]:
                self.trig[side] = on
                if emit:
                    self.events.append(('trigger', side, 'trigger', on))
        for side, (x, y) in (('L', (r[1], r[2])), ('R', (r[3], r[4]))):
            m = math.hypot((x - 128) / 127, (y - 128) / 127)
            on = m >= STICK_ON if not self.stick[side] else m > STICK_OFF
            if on != self.stick[side]:
                self.stick[side] = on
                if emit and on:          # only the push to the edge, not the return to centre
                    self.events.append(('stick', side, 'stick', True))

    def drain(self):
        out = []
        while True:
            try:
                out.append(self.events.popleft())
            except IndexError:
                return out


class ButtonClicks:
    def __init__(self):
        self.voices = []                 # [wave, position, left gain, right gain]

    def add(self, side, kind, pressed):
        g = 1.0 if pressed else RELEASE
        left = g if side in ('L', 'both') else g * OTHER_GRIP
        right = g if side in ('R', 'both') else g * OTHER_GRIP
        self.voices.append([WAVES[kind], 0, left, right])
        del self.voices[:-MAX_VOICES]

    def render(self, n, gain=1.0):
        left = np.zeros(n, np.float32); right = np.zeros(n, np.float32)
        alive = []
        for v in self.voices:
            wave, pos, gl, gr = v
            part = wave[pos:pos + n]
            left[:len(part)] += part * gl * gain; right[:len(part)] += part * gr * gain
            v[1] = pos + len(part)
            if v[1] < len(wave):
                alive.append(v)
        self.voices = alive
        return left, right
