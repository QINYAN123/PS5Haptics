"""Streaming speaker protection; tuning defaults, not a measured driver calibration.

Only speaker/headset PCM is processed. Grip channels must never pass through this.
5 ms lookahead catches transients before attenuation; no waveshaping or auto boost.
"""
import math
import numpy as np
from scipy.signal import butter, lfilter, lfilter_zi
from scipy.ndimage import maximum_filter1d

from haptic_dsp import FastSOS


class SpeakerSafety:
    def __init__(self, rate=48000, headset=False):
        self.rate = rate
        self.headset = headset
        self.delay = round(rate * .005)
        # Even delay makes the forward maximum window unambiguous.
        self.delay += self.delay % 2
        self.pending = np.zeros(self.delay)
        self.sos = np.vstack((butter(2, 180, 'highpass', fs=rate, output='sos'),
                              butter(2, 6500, 'lowpass', fs=rate, output='sos')))
        self.flt = FastSOS(self.sos, block_len=round(rate * .01))
        self.gain = 1.0
        self.attack_kernel = np.ones(self.delay + 1) / (self.delay + 1)
        self.attack_zi = lfilter_zi(self.attack_kernel, [1.])
        self._attack_zi_one = self.attack_zi.copy()
        self.release = math.exp(-1 / (rate * .080))
        self._rel_pows = self.release ** np.arange(1, round(rate * .05) + 1)
        self.ceiling = .95 if headset else .70
        self.limited_samples = 0
        self.invalid_samples = 0
        self._pending_active = False

    def process(self, samples):
        x = np.asarray(samples, dtype=np.float64)
        if x.ndim != 1:
            raise ValueError('speaker PCM must be mono')
        n = len(x)
        if not n:
            return x.astype(np.float32)
        hi = float(x.max()); lo = float(x.min())
        if not (math.isfinite(hi) and math.isfinite(lo)):
            self.invalid_samples += int(np.count_nonzero(~np.isfinite(x)))
            x = np.nan_to_num(x, nan=0., posinf=0., neginf=0.)
            hi = float(x.max()); lo = float(x.min())
        peak = hi if hi >= -lo else -lo
        attack_one = float(np.abs(self.attack_zi - self._attack_zi_one).max()) < 1e-9
        if peak < 1e-9 and (self.headset or not self.flt.active) and not self._pending_active and self.gain > 1.0 - 1e-9 and attack_one:
            return np.zeros(n, dtype=np.float32)
        # Reject corrupt numeric values without poisoning persistent filter state.
        if peak > 16.0:
            x = np.clip(x, -16., 16.)
            peak = 16.0
        if not self.headset:
            x = self.flt.step(x, peak)
        joined = np.concatenate((self.pending, x))
        jhi = float(joined.max()); jlo = float(joined.min())
        jpeak = jhi if jhi >= -jlo else -jlo
        if jpeak <= self.ceiling and self.gain > 1.0 - 1e-9 and attack_one:
            self.gain = 1.0
            out = joined[:n]
            self.pending = joined[n:].copy()
            self._pending_active = jpeak >= 1e-9
            return out.astype(np.float32)
        if jpeak <= self.ceiling:
            pows = self._rel_pows[:n] if n <= len(self._rel_pows) else (self.release ** np.arange(1, n + 1))
            gains = 1.0 - (1.0 - self.gain) * pows
            self.gain = float(gains[-1])
        else:
            peaks = maximum_filter1d(np.abs(joined), size=self.delay + 1,
                                     origin=-(self.delay // 2), mode='constant')[:n]
            if float(peaks.max()) <= self.ceiling:
                pows = self._rel_pows[:n] if n <= len(self._rel_pows) else (self.release ** np.arange(1, n + 1))
                gains = 1.0 - (1.0 - self.gain) * pows
                self.gain = float(gains[-1])
            else:
                targets = np.minimum(1., self.ceiling / np.maximum(peaks, 1e-12))
                gains = np.empty(n)
                g = self.gain
                rel = self.release
                for i, target in enumerate(targets):
                    g = min(float(target), 1. - (1. - g) * rel)
                    gains[i] = g
                self.gain = g
        # Spread attenuation over the lookahead, avoiding an audible gain step.
        gains, self.attack_zi = lfilter(self.attack_kernel, [1.], gains, zi=self.attack_zi)
        self.limited_samples += int(np.count_nonzero(gains < .999))
        out = joined[:n] * gains
        self.pending = joined[n:].copy()
        self._pending_active = bool(float(np.abs(self.pending).max()) >= 1e-9) if len(self.pending) else False
        return out.astype(np.float32)
