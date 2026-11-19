"""Stops pure dialogue from shaking the grips. Uses the Silero voice detector (models/silero_vad.onnx, MIT).

Only a clean voice closes the gate: the detector's confidence collapses as soon as other sounds cover the
voice, and any big sudden sound reopens it at once, so fights with talking in them stay as they are.
"""
import math
import pathlib
import sys

import numpy as np
from scipy.signal import butter
from haptic_dsp import FastSOS, HOP

ROOT = pathlib.Path(getattr(sys, '_MEIPASS', pathlib.Path(__file__).resolve().parent))   # the packaged app carries the model inside
RATE = 48000
WINDOW = 512              # 32 ms at 16 kHz, what the model takes
CONTEXT = 64


def suppression_enabled(config, scene=None):
    """Use the scene's saved choice, including preferences from older versions."""
    scene = scene or config.get('scene', '游戏')
    scenes = config.get('scenes') or {}
    settings = scenes.get(scene, {})
    if 'suppress_background' in settings:
        return bool(settings['suppress_background'])
    if 'voice_gate' in settings:
        return bool(settings['voice_gate'])
    # Before scene settings existed, the saved voice gate applied to games.
    if not scenes and scene == '游戏':
        return bool(config.get('voice_gate', True))
    return scene == '游戏'


class VoiceDetector:
    """48 kHz mono blocks in, speech probability out."""

    def __init__(self, model=None):
        import onnxruntime as ort
        so = ort.SessionOptions(); so.intra_op_num_threads = 1; so.inter_op_num_threads = 1
        self.sess = ort.InferenceSession(str(model or ROOT / 'models' / 'silero_vad.onnx'), so, providers=['CPUExecutionProvider'])
        self.flt = FastSOS(butter(6, 7000, 'lowpass', fs=RATE, output='sos'))
        self.phase = 0; self.buf = np.zeros(0, np.float32); self.ctx = np.zeros(CONTEXT, np.float32)
        self.state = np.zeros((2, 1, 128), np.float32); self.sr = np.array(16000, np.int64)
        self.prob = 0.0
        self._silent_hops = 0

    def feed(self, mono, block_peak=None):
        n = len(mono)
        if block_peak is None:
            block_peak = float(np.max(np.abs(mono))) if n else 0.0
        if block_peak < 1e-8 and not self.flt.active:
            self._silent_hops += 1
            if self._silent_hops >= 6:
                # Sustained digital silence: drain complete, keep phase aligned and reset VAD once cleanly.
                if self._silent_hops == 6:
                    self.ctx.fill(0.0); self.buf = np.zeros(0, np.float32)
                    self.state.fill(0.0); self.prob = 0.0
                self.phase = (self.phase - n) % 3
                return self.prob
        else:
            self._silent_hops = 0
        y = self.flt.step(mono, block_peak)
        y = y[self.phase::3].astype(np.float32)             # 48 -> 16 kHz
        self.phase = (self.phase - n) % 3
        self.buf = np.concatenate([self.buf, y])
        while len(self.buf) >= WINDOW:
            w = self.buf[:WINDOW]; self.buf = self.buf[WINDOW:]
            x = np.concatenate([self.ctx, w])[None]
            out, self.state = self.sess.run(None, {'input': x, 'state': self.state, 'sr': self.sr})
            self.ctx = w[-CONTEXT:]; self.prob = float(out[0][0])
        return self.prob


class VoiceGate:
    """gain(block_l, block_r, impact) -> what to multiply this block's sound haptics by.

    A sentence keeps a steady loudness, and a voice has next to nothing below 120 Hz and little sustained
    energy above 5 kHz. So once the gate has learned the voice, a block well above its recent peak, or a jump
    in the low or the high end, means something else joined in: open at once and stay open for a moment."""
    ON = 0.85              # a clean voice sits close to 1
    OFF = 0.5
    CONFIRM = 3            # hops above ON before closing (30 ms: a single stray window is not speech)
    HOLD = 12              # hops the gate stays closed across a gap between syllables
    LEARN = 15             # hops spent learning the voice's level after closing
    OVER = 1.5             # block loudness over the voice's recent peak that counts as another sound
    LOW_OVER = 2.5         # the same for the part below 120 Hz
    HIGH_OVER = 2.0        # and for the part above 5 kHz (metal on metal)
    REOPEN = 40            # hops the gate may not close again after that
    FLOOR = 0.1            # what remains while closed
    CLOSE, OPEN = 0.25, 0.8   # smoothing per hop

    def __init__(self, detector=None):
        self.det = detector or VoiceDetector()
        self.count = 0; self.hold = 0; self.closed = False; self.g = 1.0; self.block = 0
        self.age = 0; self.peak = 0.0; self.low_peak = 0.0; self.high_peak = 0.0
        self.flt_low = FastSOS(butter(2, 120, 'lowpass', fs=RATE, output='sos'))
        self.flt_high = FastSOS(butter(2, 5000, 'highpass', fs=RATE, output='sos'))
        self._zero_mono = np.zeros(HOP, dtype=np.float64)
        self._zero_mono.setflags(write=False)
        self._has_peak_det = hasattr(self.det, '_silent_hops')

    def gain(self, left, right, impact=0.0):
        n = len(left)
        pk = max(float(np.max(np.abs(left))), float(np.max(np.abs(right)))) if n else 0.0
        if pk < 1e-8 and not self.flt_low.active and not self.flt_high.active:
            mono = self._zero_mono if n == HOP else np.zeros(n, dtype=np.float64)
            p = self.det.feed(mono, pk) if self._has_peak_det else self.det.feed(mono)
            level = low = high = 0.0
        else:
            mono = (np.asarray(left, np.float64) + np.asarray(right, np.float64)) * 0.5
            p = self.det.feed(mono, pk) if self._has_peak_det else self.det.feed(mono)
            lo = self.flt_low.step(mono, pk)
            hi = self.flt_high.step(mono, pk)
            level = math.sqrt(float(np.dot(mono, mono)) / n) if n else 0.0
            low = math.sqrt(float(np.dot(lo, lo)) / n) if n else 0.0
            high = math.sqrt(float(np.dot(hi, hi)) / n) if n else 0.0
        if self.closed and self.age >= self.LEARN and (level > self.peak * self.OVER or low > max(self.low_peak, 0.003) * self.LOW_OVER
                                                       or high > max(self.high_peak, 0.003) * self.HIGH_OVER):
            self.closed = False; self.hold = 0; self.count = 0; self.block = self.REOPEN
        if self.block:
            self.block -= 1
        else:
            self.count = self.count + 1 if p >= self.ON else 0
            if self.count >= self.CONFIRM:
                if not self.closed:
                    self.age = 0; self.peak = level; self.low_peak = low; self.high_peak = high
                self.closed = True; self.hold = self.HOLD
            elif p < self.OFF:
                self.hold = max(0, self.hold - 1)
                if not self.hold:
                    self.closed = False
        if self.closed:
            self.age += 1
            self.peak = max(level, self.peak * 0.998); self.low_peak = max(low, self.low_peak * 0.998); self.high_peak = max(high, self.high_peak * 0.998)
        want = self.FLOOR if self.closed else 1.0
        self.g += (self.OPEN if want > self.g else self.CLOSE) * (want - self.g)
        return self.g


class GameSoundGate:
    """Pass short, broadband effects against the recent game mix (48 kHz, 10 ms hops)."""

    def __init__(self):
        self.window = np.hanning(480) / 240
        self.background = np.zeros(120)
        self.previous = np.zeros(120)
        self.age = 0
        self.hold = 0
        self.level = 0.0

    def gain(self, left, right, attack=False):
        pk = max(float(np.max(np.abs(left))), float(np.max(np.abs(right)))) if len(left) else 0.0
        if pk < 1e-8:
            # Below -160 dBFS, every FFT bin is far below 0.0008, so novel hits are guaranteed 0.
            if not (attack and self.hold) and self.level > 0.0 or self.age < 6 or float(np.max(self.background)) >= 1e-7:
                if not (attack and self.hold):
                    self.background *= 0.996
                self.previous.fill(0.0)
            else:
                self.background.fill(0.0)
                self.previous.fill(0.0)
            self.age += 1
            if self.hold:
                self.hold -= 1
                self.level = 1.0
            else:
                self.level *= 0.72
                if self.level < 0.01:
                    self.level = 0.0
            return self.level
        # Keep stereo energy: wide effects can cancel if mixed to mono first.
        spectrum = np.abs(np.fft.rfft(np.asarray((left, right)) * self.window, axis=1))
        bins = spectrum[:, 1:121].max(axis=0)
        # A slow peak envelope remembers repeated percussion. A separate onset
        # check prevents a sustained effect from repeatedly opening the gate.
        reference = self.background * (1.35 if attack else 1.9)
        novel = bins > np.maximum(np.maximum(reference, self.previous * 1.35), 0.0008)
        hits = np.count_nonzero(novel)
        if (self.age >= 6 and hits >= (9 if attack else 14)
                and np.count_nonzero(novel[:22]) >= 2
                and np.count_nonzero(novel[22:]) >= 4):
            self.hold = 12
        # Do not learn a button-correlated hit as music: an identical second
        # cast should get through even if it follows the first one immediately.
        if not (attack and self.hold):
            self.background = np.maximum(self.background * 0.996, bins)
        self.previous = bins
        self.age += 1
        if self.hold:
            self.hold -= 1
            self.level = 1.0
        else:
            self.level *= 0.72
            if self.level < 0.01:
                self.level = 0.0
        return self.level
