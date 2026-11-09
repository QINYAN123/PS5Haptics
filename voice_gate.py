"""Stops pure dialogue from shaking the grips. Uses the Silero voice detector (models/silero_vad.onnx, MIT).

Only a clean voice closes the gate: the detector's confidence collapses as soon as other sounds cover the
voice, and any big sudden sound reopens it at once, so fights with talking in them stay as they are.
"""
import pathlib
import sys

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi

ROOT = pathlib.Path(getattr(sys, '_MEIPASS', pathlib.Path(__file__).resolve().parent))   # the packaged app carries the model inside
RATE = 48000
WINDOW = 512              # 32 ms at 16 kHz, what the model takes
CONTEXT = 64


class VoiceDetector:
    """48 kHz mono blocks in, speech probability out."""

    def __init__(self, model=None):
        import onnxruntime as ort
        so = ort.SessionOptions(); so.intra_op_num_threads = 1; so.inter_op_num_threads = 1
        self.sess = ort.InferenceSession(str(model or ROOT / 'models' / 'silero_vad.onnx'), so, providers=['CPUExecutionProvider'])
        self.sos = butter(6, 7000, 'lowpass', fs=RATE, output='sos'); self.z = sosfilt_zi(self.sos) * 0.0
        self.phase = 0; self.buf = np.zeros(0, np.float32); self.ctx = np.zeros(CONTEXT, np.float32)
        self.state = np.zeros((2, 1, 128), np.float32); self.sr = np.array(16000, np.int64)
        self.prob = 0.0

    def feed(self, mono):
        y, self.z = sosfilt(self.sos, mono, zi=self.z)
        y = y[self.phase::3].astype(np.float32)             # 48 -> 16 kHz
        self.phase = (self.phase - len(mono)) % 3
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
        self.sos_low = butter(2, 120, 'lowpass', fs=RATE, output='sos'); self.z_low = sosfilt_zi(self.sos_low) * 0.0
        self.sos_high = butter(2, 5000, 'highpass', fs=RATE, output='sos'); self.z_high = sosfilt_zi(self.sos_high) * 0.0

    def gain(self, left, right, impact=0.0):
        mono = (np.asarray(left, np.float64) + np.asarray(right, np.float64)) * 0.5
        p = self.det.feed(mono)
        lo, self.z_low = sosfilt(self.sos_low, mono, zi=self.z_low)
        hi, self.z_high = sosfilt(self.sos_high, mono, zi=self.z_high)
        level = float(np.sqrt(np.mean(mono * mono))); low = float(np.sqrt(np.mean(lo * lo))); high = float(np.sqrt(np.mean(hi * hi)))
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
