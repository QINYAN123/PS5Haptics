"""Streaming speaker protection; tuning defaults, not a measured driver calibration.

Only speaker/headset PCM is processed. Grip channels must never pass through this.
5 ms lookahead catches transients before attenuation; no waveshaping or auto boost.
"""
import math
import numpy as np
from scipy.signal import butter, sosfilt, lfilter, lfilter_zi
from scipy.ndimage import maximum_filter1d


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
        self.zi = np.zeros((len(self.sos), 2))
        self.gain = 1.0
        self.attack_kernel = np.ones(self.delay + 1) / (self.delay + 1)
        self.attack_zi = lfilter_zi(self.attack_kernel, [1.])
        self.release = math.exp(-1 / (rate * .080))
        self.ceiling = .95 if headset else .70
        self.limited_samples = 0
        self.invalid_samples = 0

    def process(self, samples):
        x = np.asarray(samples, dtype=np.float64)
        if x.ndim != 1:
            raise ValueError('speaker PCM must be mono')
        if not len(x):
            return x.astype(np.float32)
        self.invalid_samples += int(np.count_nonzero(~np.isfinite(x)))
        x = np.nan_to_num(x, nan=0., posinf=0., neginf=0.)
        # Reject corrupt numeric values without poisoning persistent filter state.
        x = np.clip(x, -16., 16.)
        if not self.headset:
            x, self.zi = sosfilt(self.sos, x, zi=self.zi)
        joined = np.concatenate((self.pending, x))
        peaks = maximum_filter1d(np.abs(joined), size=self.delay + 1,
                                 origin=-(self.delay // 2), mode='constant')[:len(x)]
        targets = np.minimum(1., self.ceiling / np.maximum(peaks, 1e-12))
        gains = np.empty(len(x))
        g = self.gain
        for i, target in enumerate(targets):
            g = min(target, 1. - (1. - g) * self.release)
            gains[i] = g
        self.gain = g
        # Spread attenuation over the lookahead, avoiding an audible gain step.
        gains, self.attack_zi = lfilter(self.attack_kernel, [1.], gains, zi=self.attack_zi)
        self.limited_samples += int(np.count_nonzero(gains < .999))
        out = joined[:len(x)] * gains
        self.pending = joined[len(x):].copy()
        return out.astype(np.float32)
