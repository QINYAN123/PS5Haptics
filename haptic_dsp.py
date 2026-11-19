"""Audio -> haptics DSP (own design, tuned with sim_feel's touch model and test mixes).

Per side (left/right game audio):
  STFT (1024, hop 5 ms) -> impact / sustain separation: a per-bin "sustain floor" follows slowly
  rising, quickly falling energy (music, ambience, voices); a frequency-median gives the
  broadband part. Only energy that is broadband AND above the floor counts as an impact.
  Onset detector (spectral flux 1.5-11 kHz) adds a short accent to sharp attacks.
  Three analysis bands -> envelopes -> three carriers sized for the hand and the voice coils:
    body 65 Hz   (weight; low enough to feel heavy, high enough not to buzz)
    mid 115 Hz   (punch; the DualSense coils are most efficient around here)
    edge 165 Hz  (crisp but not shrill: clashes, clicks, footsteps)
  The right coil plays with an inverted carrier so both hands do not feel one centred buzz.
  Auto level: 8 s history, 97.5th percentile, steered to -14 dBFS.
Trigger channel: same analysis, separate band set, -> 0..1 vibration strength per trigger.
"""
import collections
import math
import numpy as np
from scipy.ndimage import median_filter
from scipy.signal import lfilter, butter, sosfilt, sosfilt_zi

try:
    from scipy.signal._sosfilt import _sosfilt as _c_sosfilt
except ImportError:
    _c_sosfilt = None

RATE = 48000
HOP = 480            # 10 ms (was 5 ms: half the per-block work, latency +5 ms)
NFFT = 1024
BIN_HZ = RATE / NFFT


class FastSOS:
    """Stateful second-order-section IIR filter bypassing Python wrapper overhead and denormal stalls."""

    def __init__(self, sos, block_len=HOP):
        self.sos = np.ascontiguousarray(sos, dtype=np.float64)
        self.z3 = np.zeros((1, self.sos.shape[0], 2), dtype=np.float64)
        self._buf = np.empty((1, block_len), dtype=np.float64)
        self._zeros = np.zeros(block_len, dtype=np.float64)
        self._zeros.setflags(write=False)
        self.active = False

    def reset(self):
        self.z3.fill(0.0)
        self.active = False

    def step(self, x, block_peak=None):
        n = len(x)
        if n == 0:
            return np.zeros(0, dtype=np.float64)
        if block_peak is None:
            block_peak = float(np.max(np.abs(x)))
        if block_peak < 1e-8 and not self.active:
            return self._zeros if n == len(self._zeros) else np.zeros(n, dtype=np.float64)
        if _c_sosfilt is None:
            y, zi = sosfilt(self.sos, np.asarray(x, dtype=np.float64), zi=self.z3[0])
            self.z3[0] = zi
        else:
            buf = self._buf if n == self._buf.shape[1] else np.empty((1, n), dtype=np.float64)
            np.copyto(buf[0], x, casting='unsafe')
            _c_sosfilt(self.sos, buf, self.z3)
            y = buf[0].copy()
        if block_peak >= 1e-8:
            self.active = True
        elif float(np.abs(self.z3).max()) < 1e-12:
            self.z3.fill(0.0)
            self.active = False
        return y

# bands: (low Hz, high Hz, threshold dB, gain dB, release ms, carrier Hz)
# floor_pct / floor_ms: how fast the sustain floor tracks; span_hz: broadband median width;
# mask_floor: minimum share kept of an above-floor bin; onset: flux ratio for an accent; accent_db
GRIP_PROFILES = {
    '均衡': dict(floor_pct=30, floor_ms=280, span_hz=260, mask_floor=.35, onset=2.2, accent_db=4,
               bands=[(500, 2000, -35, 8, 38, 65), (2000, 4200, -36, 7, 18, 115), (4200, 11000, -39, 8, 9, 165)]),
    '细节丰富': dict(floor_pct=25, floor_ms=240, span_hz=260, mask_floor=.45, onset=1.9, accent_db=5,
                 bands=[(450, 2000, -39, 9, 42, 65), (2000, 4200, -40, 8, 20, 115), (4200, 11000, -43, 9, 11, 165)]),
    '干净': dict(floor_pct=35, floor_ms=320, span_hz=260, mask_floor=.25, onset=2.6, accent_db=3,
               bands=[(600, 2200, -31, 7, 32, 65), (2200, 4500, -32, 6, 14, 115), (4500, 11000, -35, 7, 7, 165)]),
    # more bands and carriers, the top bands lifted so detail is not buried under the body; 'texture' adds the
    # sound's own low end (only while it is an impact), so different hits differ in weight and not only in strength
    '细腻': dict(floor_pct=30, floor_ms=280, span_hz=260, mask_floor=.35, onset=2.2, accent_db=5, level=.75, texture=.4,
               bands=[(500, 1000, -36, 6, 45, 60), (1000, 2000, -37, 8, 34, 80), (2000, 3200, -38, 10, 22, 105),
                      (3200, 5000, -39, 12, 14, 135), (5000, 8000, -41, 13, 10, 165), (8000, 12000, -42, 14, 7, 190)]),
}
TRIGGER_PROFILES = {
    '均衡冲击': dict(floor_pct=20, floor_ms=180, span_hz=260, mask_floor=.45, onset=2.0, accent_db=5,
                 bands=[(700, 2200, -32, 5, 25, 55), (2200, 5000, -34, 7, 12, 80), (5000, 11000, -35, 8, 6, 120)]),
    '载具摩擦': dict(floor_pct=20, floor_ms=110, span_hz=260, mask_floor=.45, onset=2.0, accent_db=5,
                 bands=[(200, 1500, -30, 4, 60, 50), (1500, 4500, -32, 6, 30, 80), (4500, 11000, -34, 9, 15, 140)]),
}


def _db(x):
    return 20 * np.log10(np.maximum(x, 1e-9))


def trigger_curve(x, drive_db=6.0, knee=0.55, ceiling=0.85):
    """Trigger strength: +6 dB, linear up to 0.55, then a soft shoulder to 0.85 (full-scale
    trigger vibration everywhere just feels like noise; headroom keeps big hits distinguishable)."""
    y = x * 10 ** (drive_db / 20)
    if y <= knee:
        return float(y)
    return float(knee + (ceiling - knee) * math.tanh((y - knee) / (ceiling - knee)))


class AutoGain:
    """Slow level steering: 8 s history, 97.5th percentile -> target; cuts 15x faster than it boosts."""

    def __init__(self, target_db=-14.0, max_boost=18.0, max_cut=12.0, rise_db_s=0.6):
        maxlen = int(8 * RATE / HOP)
        self.hist = collections.deque(maxlen=maxlen)
        self.gain_db = 0.0
        self.target = target_db; self.max_boost = max_boost; self.max_cut = max_cut; self.rise = rise_db_s
        self._buf = np.empty(maxlen, dtype=np.float64)
        self._maxlen = maxlen
        self._idx = 0
        self._count = 0
        self._dirty = 0
        self._peak = -90.0
        self._gain = 1.0

    def update(self, level_db):
        if level_db > -90:
            self.hist.append(level_db)
            self._buf[self._idx] = level_db
            self._idx = (self._idx + 1) % self._maxlen
            if self._count < self._maxlen:
                self._count += 1
            self._dirty += 1
        if self._count < 40:
            return self._gain
        if self._dirty >= 8 or self._peak <= -90.0:
            view = self._buf[:self._count] if self._count < self._maxlen else self._buf
            self._peak = float(np.percentile(view, 97.5))
            self._dirty = 0
        want = max(-self.max_cut, min(self.max_boost, self.target - self._peak))
        if abs(want - self.gain_db) < 1e-9:
            return self._gain
        step = self.rise * HOP / RATE
        if want < self.gain_db:
            self.gain_db = max(want, self.gain_db - 15 * step)
        else:
            self.gain_db = min(want, self.gain_db + step)
        self._gain = 10 ** (self.gain_db / 20)
        return self._gain


class STFT:
    """Shared per-channel spectrum (computed once, used by grip and trigger analyzers)."""

    def __init__(self):
        self.win = np.hanning(NFFT).astype(np.float32)
        self.buf = np.zeros(NFFT, np.float32)
        self._wbuf = np.empty(NFFT, np.float32)
        self._scale = np.float32(1.0 / (NFFT / 4))
        self._zero_mag = np.zeros(NFFT // 2 + 1, np.float32)
        self._zero_mag.setflags(write=False)
        self._tail_hops = 0

    def step(self, block, block_peak=None):
        n = len(block)
        if block_peak is None:
            block_peak = float(np.max(np.abs(block))) if n else 0.0
        if block_peak >= 1e-8:
            self._tail_hops = (NFFT + HOP - 1) // HOP + 1
        elif self._tail_hops > 0:
            self._tail_hops -= 1
            if self._tail_hops == 0:
                self.buf.fill(0.0)
                return self._zero_mag
        else:
            return self._zero_mag
        if n < NFFT:
            self.buf[:-n] = self.buf[n:]
            self.buf[-n:] = block
        else:
            self.buf[:] = block[-NFFT:]
        np.multiply(self.buf, self.win, out=self._wbuf)
        return np.abs(np.fft.rfft(self._wbuf)) * self._scale


class SideAnalyzer:
    """Spectrum -> impact spectrum per hop (sustain floor + broadband check + onset accent).

    Sustain floor: per-bin asymmetric follower, quick to fall and slow to rise, so a held note or
    ambience settles into the floor while a hit pokes out of it."""

    def __init__(self, prof):
        self.p = prof
        self.H = None
        self.rise = 1 - math.exp(-HOP / RATE / (prof['floor_ms'] / 1000))
        self.fall = 1 - math.exp(-HOP / RATE / (prof['floor_ms'] / 1000 * prof['floor_pct'] / 100 / 4))
        self.prev_log = None
        self.flux_hist = collections.deque(maxlen=200)
        self.enhance = 0.0
        self.lock = 0
        self.span = max(1, int(round(prof['span_hz'] / BIN_HZ))) | 1
        self.flux_lo = int(1500 / BIN_HZ); self.flux_hi = int(11000 / BIN_HZ)
        self._zero_spec = np.zeros(NFFT // 2 + 1, dtype=np.float64)
        self._zero_spec.setflags(write=False)
        self._enhance_decay = math.exp(-HOP / RATE / 0.025)

    def settled(self):
        return (self.H is None or float(np.max(self.H)) < 1e-9) and self.enhance < 1e-7 and self.lock == 0

    def step(self, mag, P=None, is_zero=False):
        if is_zero and self.settled():
            if self.H is None:
                self.H = np.zeros_like(mag, dtype=np.float64)
            else:
                self.H.fill(0.0)
            self.enhance = 0.0
            if self.prev_log is not None:
                self.prev_log.fill(0.0)
            self.flux_hist.append(0.0)
            return self._zero_spec
        if self.H is None:
            self.H = mag.copy()
        d = mag - self.H
        self.H += np.where(d > 0, self.rise, self.fall) * d
        H = self.H
        if is_zero:
            perc = self._zero_spec
            flux = 0.0
            if self.prev_log is not None:
                self.prev_log.fill(0.0)
        else:
            if P is None:
                P = median_filter(mag, size=self.span, mode='nearest')
            mask = P ** 2 / (H ** 2 + P ** 2 + 1e-12)
            mask = np.maximum(mask, self.p['mask_floor']) * (mag > H)  # keep only energy above the sustained floor
            perc = mag * mask
            # onset (spectral flux, 1.5-11 kHz) -> short accent
            logm = np.log1p(mag[self.flux_lo:self.flux_hi] * 100)
            flux = float(np.sum(np.maximum(0, logm - self.prev_log))) if self.prev_log is not None else 0.0
            self.prev_log = logm
        self.flux_hist.append(flux)
        self.enhance *= self._enhance_decay
        if self.lock > 0:
            self.lock -= 1
        elif flux > 0.5:
            med = float(np.median(self.flux_hist)) + 1e-6
            if flux / med > self.p['onset']:
                self.enhance = 1.0; self.lock = int(0.035 * RATE / HOP)
        if perc is self._zero_spec:
            return perc
        boost = 10 ** (self.p['accent_db'] * self.enhance / 20)
        return perc * boost


class BandMapper:
    """Impact spectrum -> per-band envelopes (attack ~1 ms = immediate per hop, release per band)."""

    def __init__(self, bands):
        self.bands = bands
        self.idx = [(int(lo / BIN_HZ), max(int(lo / BIN_HZ) + 1, int(hi / BIN_HZ))) for lo, hi, *_ in bands]
        self.env = np.zeros(len(bands))
        self.rel = np.array([math.exp(-HOP / RATE / (b[4] / 1000)) for b in bands])
        self._zeros = np.zeros(len(bands), dtype=np.float64)
        self._zeros.setflags(write=False)

    def settled(self):
        return float(np.max(self.env)) < 1e-7

    def step(self, spec, gain, is_zero=False):
        if is_zero:
            if float(np.max(self.env)) < 1e-7:
                self.env.fill(0.0)
                return self._zeros
            self.env *= self.rel
            return self.env.copy()
        out = np.empty(len(self.bands), dtype=np.float64)
        for i, ((a, b), (lo, hi, thr, gdb, rel_ms, car)) in enumerate(zip(self.idx, self.bands)):
            sl = spec[a:b]
            rms = math.sqrt(float(np.dot(sl, sl)) / (b - a)) * gain
            level = 20.0 * math.log10(rms if rms > 1e-9 else 1e-9)
            over = level - thr
            target = 0.0 if over <= 0 else min(1.0, 10 ** ((over + gdb - 30) / 20))
            self.env[i] = target if target > self.env[i] else max(target, self.env[i] * self.rel[i])
            out[i] = self.env[i]
        return out


HAPTIC_STYLES = ('游戏', '影院', '律动')                      # shown names
STYLE_ALIAS = {'游戏': '标准', '影院': '电影', '律动': '音乐'}   # internal names


class Groove:
    """Extra layer for 电影 / 音乐 styles: rhythm and low end that the impact analysis filters out.

    音乐: kick-drum / bass onsets (35-150 Hz) -> short 55 Hz pulses on both grips and pulses on both
          triggers; hi-hat / snare onsets (2.5-8 kHz) -> short 170 Hz ticks on the right grip; the
          bass line keeps a light hum on the triggers -> the pad moves with the beat.
    电影: sustained low end (25-150 Hz) -> 45 Hz rumble on both grips, explosions -> long heavy
          pulses (250 ms) on grips and triggers.
    Levels are normalised per band against a slow running peak, so quiet and loud mixes both work.
    """

    def __init__(self, style='标准', gains=None):
        style = STYLE_ALIAS.get(style, style); self.style = style
        g = gains or {}                                    # user gains from the mode card's settings (1.0 = default)
        self.g_kick = g.get('kick', 1.0); self.g_tick = g.get('tick', 1.0); self.g_rumble = g.get('rumble', 1.0); self.g_trig = g.get('trigger', 1.0)
        self.flt_low = FastSOS(butter(2, (35, 150) if style == '音乐' else (25, 150), 'bandpass', fs=RATE, output='sos'))
        self.flt_hi = FastSOS(butter(2, (2500, 8000), 'bandpass', fs=RATE, output='sos'))
        self.fast = [0.0, 0.0]; self.slow = [1e-4, 1e-4]; self.peak = [1e-3, 1e-3]
        self.kick = 0.0; self.tick = 0.0; self.rumble = 0.0; self.hold = [0, 0]
        dec = 0.07 if style == '音乐' else 0.25
        self.kick_dec = math.exp(-HOP / RATE / dec); self.tick_dec = math.exp(-HOP / RATE / 0.03)
        self.f_rel = math.exp(-HOP / RATE / 0.025); self.s_k = math.exp(-HOP / RATE / 0.4)
        self.p_rel = math.exp(-HOP / RATE / 6.0); self.r_k = math.exp(-HOP / RATE / 0.08)
        self.ph = np.zeros(3); self.car = np.array([55.0 if style == '音乐' else 45.0, 170.0, 45.0])
        self.prev = np.zeros(3)
        self._k = np.arange(HOP, dtype=np.float64)
        self._frac = (self._k / HOP)[None, :]
        self._phases_base = (2 * np.pi / RATE) * self.car[:, None] * self._k[None, :]
        self._dph = 2 * np.pi * self.car / RATE
        self._z_hop = np.zeros(HOP, dtype=np.float32); self._z_hop.setflags(write=False)

    def settled(self):
        return (self.style not in ('电影', '音乐')
                or (not self.flt_low.active and not self.flt_hi.active
                    and self.kick < 1e-6 and self.tick < 1e-6 and self.rumble < 1e-6
                    and float(np.max(np.abs(self.prev))) < 1e-6))

    def _band(self, i, y):
        e = math.sqrt(float(np.dot(y, y)) / len(y)) if len(y) else 0.0
        self.fast[i] = e if e > self.fast[i] else self.fast[i] * self.f_rel
        self.slow[i] = self.slow[i] * self.s_k + e * (1 - self.s_k)
        self.peak[i] = e if e > self.peak[i] else max(1e-4, self.peak[i] * self.p_rel)
        norm = e / self.peak[i]
        onset = self.fast[i] / (self.slow[i] + 1e-6)
        return norm, onset

    def step(self, left, right, block_peak=None):
        """-> (grip_l, grip_r, trig_l, trig_r); zeros in 标准."""
        n = len(left)
        if self.style not in ('电影', '音乐') or n == 0:
            return None
        if block_peak is None:
            block_peak = max(float(np.max(np.abs(left))), float(np.max(np.abs(right))))
        if block_peak < 1e-8 and self.settled():
            self.kick = self.tick = self.rumble = 0.0
            self.prev.fill(0.0)
            z = self._z_hop if n == HOP else np.zeros(n, dtype=np.float32)
            return z, z, 0.0, 0.0
        x = (left + right) * 0.5
        yl = self.flt_low.step(x, block_peak); yh = self.flt_hi.step(x, block_peak)
        nl, ol = self._band(0, yl); nh, oh = self._band(1, yh)
        self.hold = [max(0, h - 1) for h in self.hold]
        if ol > 1.7 and nl > 0.35 and not self.hold[0]:           # kick / explosion onset (refractory 60 ms)
            self.kick = max(self.kick, min(1.0, 0.45 + 0.35 * (ol - 1.7))); self.hold[0] = int(0.06 * RATE / HOP)
        if self.style == '音乐' and oh > 1.8 and nh > 0.3 and not self.hold[1]:
            self.tick = max(self.tick, min(1.0, 0.4 + 0.3 * (oh - 1.8))); self.hold[1] = int(0.04 * RATE / HOP)
        self.rumble = self.rumble * self.r_k + nl * (1 - self.r_k)
        if self.style == '音乐':
            k, tk_ = self.kick * self.g_kick, self.tick * self.g_tick
            amps = np.array([0.95 * k, 0.55 * tk_, 0.0])
            hum = 0.22 * self.rumble
            tl = max(0.85 * k, hum) * self.g_trig; tr = max(1.0 * k, hum, 0.4 * tk_) * self.g_trig
        else:
            k, rb = self.kick * self.g_kick, self.rumble ** 1.5 * self.g_rumble
            amps = np.array([0.9 * k, 0.0, 0.55 * rb])
            tl = tr = max(0.9 * k, 0.45 * rb) * self.g_trig
        if n == HOP:
            frac = self._frac
            pbase = self._phases_base
        else:
            k_arr = np.arange(n, dtype=np.float64)
            frac = (k_arr / n)[None, :]
            pbase = (2 * np.pi / RATE) * self.car[:, None] * k_arr[None, :]
        ramp = self.prev[:, None] + (amps - self.prev)[:, None] * frac
        ph = self.ph[:, None] + pbase
        w = ramp * np.sin(ph)
        self.ph = (self.ph + self._dph * n) % (2 * np.pi); self.prev = amps
        self.kick *= self.kick_dec; self.tick *= self.tick_dec
        gl = w[0] + w[2]; gr = w[0] + w[1] + w[2]
        return gl.astype(np.float32), gr.astype(np.float32), tl, tr


class TextureLayer:
    """The sound's own 70-320 Hz waveform, let through only while the impact analysis sees a hit there."""
    LO, HI = 70, 320

    def __init__(self, amount):
        self.amount = amount
        self.flt = FastSOS(butter(2, (self.LO, self.HI), 'bandpass', fs=RATE, output='sos'))
        self.a, self.b = int(self.LO / BIN_HZ), max(int(self.LO / BIN_HZ) + 1, int(self.HI / BIN_HZ))
        self.gate = 0.0; self.rel = math.exp(-HOP / RATE / 0.05)
        self._z32 = np.zeros(HOP, dtype=np.float32); self._z32.setflags(write=False)

    def settled(self):
        return not self.flt.active and self.gate < 1e-6

    def step(self, block, mag, perc, agc, impact, block_peak=None):
        if block_peak is None:
            block_peak = float(np.max(np.abs(block)))
        if block_peak < 1e-8 and self.settled():
            self.gate = 0.0
            return self._z32 if len(block) == HOP else np.zeros(len(block), dtype=np.float32)
        y = self.flt.step(block, block_peak)
        m_sl = mag[self.a:self.b]; p_sl = perc[self.a:self.b]
        total = float(np.dot(m_sl, m_sl)) + 1e-12; hit = float(np.dot(p_sl, p_sl))
        want = min(1.0, max(0.0, (hit / total - 0.25) / 0.5))           # how much of this band is above the sustain floor
        want *= min(1.0, impact / 0.2)                                  # and the bands above must agree it is a hit: a bass note is not
        start = self.gate
        self.gate = want if want > self.gate else max(want, self.gate * self.rel)
        if start == 0.0 and self.gate == 0.0:
            return self._z32 if len(y) == HOP else np.zeros(len(y), dtype=np.float32)
        ramp = np.linspace(start, self.gate, len(y))
        return (y * ramp * agc * self.amount).astype(np.float32)


class HapticEngine:
    """Stereo game audio -> (left wave, right wave, trigger_left 0..1, trigger_right 0..1)."""

    def __init__(self, grip='均衡', trigger='均衡冲击', grip_gain=1.0, trigger_gain=1.0, style='标准', groove=None):
        gp, tp = GRIP_PROFILES[grip], TRIGGER_PROFILES[trigger]
        self.groove = Groove(style, groove)
        self.sl_fft, self.sr_fft = STFT(), STFT()
        self.gl, self.gr = SideAnalyzer(gp), SideAnalyzer(gp)
        self.tl, self.tr = SideAnalyzer(tp), SideAnalyzer(tp)
        self.ml, self.mr = BandMapper(gp['bands']), BandMapper(gp['bands'])
        self.tml, self.tmr = BandMapper(tp['bands']), BandMapper(tp['bands'])
        self.carriers = np.array([b[5] for b in gp['bands']], float)
        self.level_scale = gp.get('level', 1.0)
        self.tex_l = TextureLayer(gp['texture']) if gp.get('texture') else None
        self.tex_r = TextureLayer(gp['texture']) if gp.get('texture') else None
        self.phase = np.zeros(len(self.carriers))
        self.prev_amp_l = np.zeros(len(self.carriers)); self.prev_amp_r = np.zeros(len(self.carriers))
        self.agc = AutoGain()
        self.grip_gain = grip_gain; self.trigger_gain = trigger_gain
        self.duck = 1.0
        self.level = 0.0
        self.impact = 0.0
        n = np.arange(HOP, dtype=np.float64)
        self._frac = (n / HOP)[None, :]
        self._phases_base = (2 * np.pi / RATE) * self.carriers[:, None] * n[None, :]
        self._dphase = 2 * np.pi * self.carriers * HOP / RATE
        self._z_hop = np.zeros(HOP, dtype=np.float32); self._z_hop.setflags(write=False)
        self._zero_zeros = np.zeros(HOP, dtype=np.float64); self._zero_zeros.setflags(write=False)
        self._idle = False

    def _synth(self, amps, prev):
        ramp = prev[:, None] + (amps - prev)[:, None] * self._frac          # per-sample amplitude ramp
        ph = self.phase[:, None] + self._phases_base
        return ramp, ph

    def _is_idle(self):
        return (self.sl_fft._tail_hops == 0 and self.sr_fft._tail_hops == 0
                and self.gl.settled() and self.gr.settled()
                and self.tl.settled() and self.tr.settled()
                and self.ml.settled() and self.mr.settled()
                and self.tml.settled() and self.tmr.settled()
                and float(np.max(np.abs(self.prev_amp_l))) < 1e-6
                and float(np.max(np.abs(self.prev_amp_r))) < 1e-6
                and (self.tex_l is None or self.tex_l.settled())
                and (self.tex_r is None or self.tex_r.settled())
                and self.groove.settled())

    def process(self, left, right, trigger_pressed=(0.0, 0.0)):
        press = max(trigger_pressed)
        target_duck = 1.0 - 0.7 * min(1.0, max(0.0, (press - 0.12) / 0.18))
        self.duck += (target_duck - self.duck) * 0.3
        n = len(left)
        if n:
            hi_l, lo_l = float(left.max()), float(left.min())
            hi_r, lo_r = float(right.max()), float(right.min())
            pk_l = hi_l if hi_l >= -lo_l else -lo_l
            pk_r = hi_r if hi_r >= -lo_r else -lo_r
        else:
            pk_l = pk_r = 0.0
        peak = pk_l if pk_l >= pk_r else pk_r
        if peak < 1e-8:
            if self._idle or self._is_idle():
                if not self._idle:
                    for a in (self.gl, self.gr, self.tl, self.tr):
                        if a.H is None:
                            a.H = np.zeros(NFFT // 2 + 1, dtype=np.float64)
                    self._idle = True
                self.agc.update(-180.0)
                self.impact = 0.0
                self.level = 0.0
                return self._z_hop, self._z_hop, (0.0, 0.0)
        else:
            self._idle = False
        if peak < 1e-8:
            g = self.agc.update(-180.0)
        else:
            rms = math.sqrt((float(np.dot(left, left)) + float(np.dot(right, right))) / (2 * len(left))) + 1e-9
            g = self.agc.update(20.0 * math.log10(rms if rms > 1e-9 else 1e-9))
        ml, mr = self.sl_fft.step(left, pk_l), self.sr_fft.step(right, pk_r)
        lz, rz = (ml is self.sl_fft._zero_mag), (mr is self.sr_fft._zero_mag)
        span = self.gl.span                                     # one broadband median per side, shared by grip + trigger
        pl_ = ml if lz else median_filter(ml, size=span, mode='nearest')
        pr_ = mr if rz else median_filter(mr, size=span, mode='nearest')
        sl, sr = self.gl.step(ml, pl_, is_zero=lz), self.gr.step(mr, pr_, is_zero=rz)
        slz, srz = (sl is self.gl._zero_spec), (sr is self.gr._zero_spec)
        al, ar = self.ml.step(sl, g, is_zero=slz), self.mr.step(sr, g, is_zero=srz)
        self.impact = float(max(np.max(al), np.max(ar)))      # before user gain/duck: drives the speaker gate
        # grips step back while a trigger is held, so the trigger feedback reads clearly (keeps 30 %)
        al = al * self.grip_gain * self.duck; ar = ar * self.grip_gain * self.duck
        l_silent = float(np.max(al)) == 0.0 and float(np.max(self.prev_amp_l)) == 0.0
        r_silent = float(np.max(ar)) == 0.0 and float(np.max(self.prev_amp_r)) == 0.0
        if l_silent:
            wl = self._zero_zeros
            ph = self.phase[:, None] + self._phases_base if not r_silent else None
        else:
            rl, ph = self._synth(al, self.prev_amp_l)
            wl = np.sum(rl * np.sin(ph), axis=0)
        if r_silent:
            wr = self._zero_zeros
        else:
            if l_silent:
                rr, ph = self._synth(ar, self.prev_amp_r)
            else:
                rr = self.prev_amp_r[:, None] + (ar - self.prev_amp_r)[:, None] * self._frac
            wr = np.sum(rr * np.sin(ph + np.pi), axis=0)          # right coil inverted (two hands, not one centred buzz)
        if self.level_scale != 1.0:
            if not l_silent:
                wl = wl * self.level_scale
            if not r_silent:
                wr = wr * self.level_scale
        if self.tex_l is not None:                             # the sound's own weight, same user gain and duck as the rest
            tl_wav = self.tex_l.step(left, ml, sl, g, self.impact, pk_l)
            tr_wav = self.tex_r.step(right, mr, sr, g, self.impact, pk_r)
            if tl_wav is not self.tex_l._z32:
                wl = wl + tl_wav * (self.grip_gain * self.duck)
            if tr_wav is not self.tex_r._z32:
                wr = wr + tr_wav * (self.grip_gain * self.duck)
        self.phase = (self.phase + self._dphase) % (2 * np.pi)
        self.prev_amp_l, self.prev_amp_r = al, ar
        # trigger envelopes: mostly its own side, 30 % of the centre so centred hits reach both
        tsl = self.tl.step(ml, pl_ if self.tl.span == span else None, is_zero=lz)
        tsr = self.tr.step(mr, pr_ if self.tr.span == span else None, is_zero=rz)
        tl = float(np.max(self.tml.step(tsl, g, is_zero=(tsl is self.tl._zero_spec))))
        tr = float(np.max(self.tmr.step(tsr, g, is_zero=(tsr is self.tr._zero_spec))))
        if tl == 0.0 and tr == 0.0:
            trig = (0.0, 0.0)
        else:
            mix = (tl + tr) / 2
            trig = (trigger_curve((0.3 * mix + 0.7 * tl) * self.trigger_gain), trigger_curve((0.3 * mix + 0.7 * tr) * self.trigger_gain))
        self.level = float(max(np.max(al), np.max(ar)))
        gv = self.groove.step(left, right, peak)
        if gv is not None:                                     # 电影 / 音乐: rhythm + low end on top
            gl, gr, gtl, gtr = gv
            if gl is not self.groove._z_hop:
                wl = wl + gl * self.grip_gain * self.duck; wr = wr + gr * self.grip_gain * self.duck
                self.level = max(self.level, float(np.max(np.abs(gl))))
            if gtl or gtr:
                trig = (max(trig[0], min(1.0, gtl * self.trigger_gain)), max(trig[1], min(1.0, gtr * self.trigger_gain)))
        out_l = self._z_hop if wl is self._zero_zeros else np.tanh(wl * 1.2).astype(np.float32)
        out_r = self._z_hop if wr is self._zero_zeros else np.tanh(wr * 1.2).astype(np.float32)
        return out_l, out_r, trig


SPEAKER_MODES = ('关闭', '全部游戏声', '特殊事件')
SPEAKER_EQS = ('原味', '低音饱满', '人声清晰')
SPEAKER_MODE_ALIAS = {'全部游戏声音': '全部游戏声', '原声': '全部游戏声', '冲击声': '特殊事件', '仅关键事件': '特殊事件', '仅大招/特殊技': '特殊事件',
                      '智能增强': '全部游戏声'}


class RawSpeaker:
    """The pad speaker the PlayStation way: the real sound, only EQ'd, never re-dynamised.

    One set of filters per block, mixed with fixed weights:
      base      whole mix, 80 Hz protective high-pass
      bass      virtual bass: the 45-160 Hz part through a soft square law, low-passed to 400 Hz ->
                its 2nd harmonic, which the small speaker can play (no hard rectifier = no buzz)
      presence  2-4.5 kHz (voices, metal, string)
      top       > 6 kHz (hiss, sibilance)
    Presets (bass, presence, top):  原味 = untouched, 低音饱满, 人声清晰; event presets for the
    特殊事件 moments: clang (parry metal), whoosh (dodge), big (ultimate / special), thud (heavy hit).
    """
    PRESETS = {'原味': (0.0, 0.0, 0.0), '低音饱满': (0.9, 0.0, -0.15), '人声清晰': (0.0, 0.45, -0.1),
               'clang': (0.0, 0.15, -0.20), 'whoosh': (0.15, 0.15, -0.15), 'big': (0.4, 0.10, -0.15), 'thud': (0.4, 0.0, -0.25),
               None: (0.4, 0.3, 0.0)}

    def __init__(self):
        def f(*a, **k):
            return FastSOS(butter(*a, fs=RATE, output='sos', **k))
        self.hp = f(2, 80, 'highpass'); self.lo = f(2, (45, 160), 'bandpass'); self.h_lp = f(2, 400, 'lowpass')
        self.h_hp = f(1, 70, 'highpass'); self.pr = f(2, (2000, 4500), 'bandpass'); self.top = f(1, 6000, 'highpass')
        self._filters = (self.hp, self.lo, self.h_lp, self.h_hp, self.pr, self.top)
        self._z64 = np.zeros(HOP, dtype=np.float64); self._z64.setflags(write=False)
        self._z32 = np.zeros(HOP, dtype=np.float32); self._z32.setflags(write=False)
        self._z_parts = (self._z64, self._z64, self._z64, self._z64)
        self.parts = None; self.env = 0.05; self.lv = 0.05; self.mk = 1.0

    @staticmethod
    def _run(st, x):
        return st.step(x)

    def analyse(self, left, right):
        n = len(left)
        pk = max(float(np.max(np.abs(left))), float(np.max(np.abs(right)))) if n else 0.0
        if pk < 1e-8 and not any(flt.active for flt in self._filters):
            self.parts = self._z_parts if n == HOP else (np.zeros(n, np.float64),) * 4
            self.env *= 0.995
            self.mk += (min(4.0, max(1.0, 0.14 / max(self.lv, 1e-4))) - self.mk) * 0.02
            return
        x = (left + right) * 0.5
        b = self.lo.step(x, pk)
        b_pk = float(np.max(np.abs(b))) if len(b) else 0.0
        self.env = max(b_pk, self.env * 0.995)
        h_in = (b * b) / max(self.env, 1e-3) if (b_pk > 0.0 or self.lo.active) else b
        h_lp_out = self.h_lp.step(h_in, b_pk * b_pk / max(self.env, 1e-3) if b_pk else 0.0)
        h = self.h_hp.step(h_lp_out)
        self.parts = (self.hp.step(x, pk), h, self.pr.step(x, pk), self.top.step(x, pk))
        base = self.parts[0]
        rms = math.sqrt(float(np.dot(base, base)) / len(base)) if len(base) else 0.0
        if rms > 1e-4:                                   # silence does not count (no boost creeping up)
            self.lv = self.lv * 0.98 + rms * 0.02
        self.mk += (min(4.0, max(1.0, 0.14 / max(self.lv, 1e-4))) - self.mk) * 0.02

    def render(self, preset, volume, boost=1.0):
        base, h, pr, top = self.parts
        if base is self._z64:
            return self._z32
        wb, wp, wt = self.PRESETS.get(preset, self.PRESETS['原味'])
        base_rms = math.sqrt(float(np.dot(base, base)) / len(base)) if len(base) else 0.0
        y = base + wb * h * (base_rms + 1e-4) * 4 + wp * pr + wt * top
        return (y * np.clip(volume, 0., 1.) * boost * 1.5 * self.mk).astype(np.float32)  # final output lookahead limiter preserves waveform


class SmartSpeaker:
    """Game audio -> the pad's tiny speaker, shaped for it (智能增强).

    * only 400 Hz - 7 kHz is sent (the speaker cannot move air below ~500 Hz; bass only costs
      headroom and rattles), split into three bands: body 400-1200, mid 1.2-3 k, presence 3-7 k
    * per band transient shaper: a fast envelope against a slow one. Hits, clashes, whooshes
      (fast >> slow) are lifted up to x3; steady music / ambience (fast ~ slow) is pulled down
    * event EQ: while an event opens the speaker, a preset tilts the bands (clang = parry metal,
      whoosh = perfect dodge, big = ultimate / special, thud = heavy hit)
    * scene gain (0..1.5) from the scene classifier, slow auto gain, soft clip
    """
    BANDS = ((150, 1200), (1200, 3000), (3000, 7000))   # 150 Hz floor: keep what the speaker can still play
    BASE = (0.85, 1.0, 1.35)                       # presence lift: the speaker sounds clearer, not louder
    PRESETS = {None: (1.0, 1.0, 1.0), 'clang': (0.7, 1.2, 2.4), 'whoosh': (0.8, 1.9, 1.5),
               'big': (1.7, 1.4, 1.2), 'thud': (1.9, 1.1, 0.7)}

    def __init__(self, volume=0.6, boost=1.0, mild=False):
        self.volume = volume; self.boost = boost; self.mode = '智能增强'
        # mild (the normal 全部游戏声音 playback): hits lifted at most x1.6, steady sound kept at >= 0.7
        self.g_lo, self.g_hi, self.g_exp = (0.7, 1.6, 1.0) if mild else (0.35, 3.0, 1.3)
        self.flts = [FastSOS(butter(2, b, 'bandpass', fs=RATE, output='sos')) for b in self.BANDS]
        self.fast = [0.0] * 3; self.slow = [1e-3] * 3; self.g = [1.0] * 3
        self.f_rel = math.exp(-HOP / RATE / 0.030); self.s_k = math.exp(-HOP / RATE / 0.35)
        self.env = 0.02; self.env_rel = math.exp(-HOP / RATE / 3.0)
        self.event = None; self.event_mix = 0.0; self.scene = 1.0; self.scene_now = 1.0

    def process(self, left, right, impact=None):
        if self.volume <= 0:
            return None
        pk = max(float(np.max(np.abs(left))), float(np.max(np.abs(right)))) if len(left) else 0.0
        x = (left + right) * 0.5
        out = np.zeros(len(x))
        target_mix = 1.0 if self.event else 0.0
        self.event_mix += (target_mix - self.event_mix) * 0.25
        preset = self.PRESETS.get(self.event, self.PRESETS[None])
        for i, flt in enumerate(self.flts):
            y = flt.step(x, pk)
            e = math.sqrt(float(np.dot(y, y)) / len(y)) if len(y) else 0.0
            self.fast[i] = e if e > self.fast[i] else self.fast[i] * self.f_rel
            self.slow[i] = self.slow[i] * self.s_k + e * (1 - self.s_k)
            ratio = self.fast[i] / (self.slow[i] + 1e-5)
            tg = min(self.g_hi, max(self.g_lo, ratio ** self.g_exp)) * self.BASE[i] * (1.0 + (preset[i] - 1.0) * self.event_mix)
            out += y * np.linspace(self.g[i], tg, len(y)); self.g[i] = tg
        rms = math.sqrt(float(np.dot(out, out)) / len(out)) if len(out) else 0.0
        self.env = rms if rms > self.env else max(rms, self.env * self.env_rel)
        sc0 = self.scene_now; self.scene_now += (self.scene - self.scene_now) * 0.05
        gain = min(10.0, 0.22 / max(self.env, 0.004)) * self.boost
        out *= gain * np.linspace(sc0, self.scene_now, len(out))
        return np.tanh(out * self.volume * 1.4).astype(np.float32)


class SpeakerShaper:
    """Game audio -> controller speaker (DualSense USB audio ch1/ch2).

    冲击声: mono game audio, high-passed (the tiny speaker has no bass), opened only while the
    haptic analysis sees an impact (fast attack, ~80 ms release) -> hits, clashes, gunshots come
    out of the pad, music/dialogue mostly don't. 全部游戏声音: the whole mix, high-passed."""

    def __init__(self, mode='冲击声', volume=0.6, boost=1.0):
        self.mode = {'仅大招/特殊技': '仅关键事件', '智能增强': '全部游戏声音'}.get(mode, mode)
        self.volume = volume; self.boost = boost; self.scene = 1.0; self.scene_now = 1.0
        # 全部游戏声音 = the whole mix, but shaped for the tiny speaker: < 400 Hz removed (it cannot
        # play it, it only costs headroom and rattles), presence lifted, hits gently brought forward
        self.smart = SmartSpeaker(volume, boost, mild=True) if self.mode == '全部游戏声音' else None
        if self.mode == '原声':       # whole mix, untouched except: virtual bass + softer top + limiter
            self.flt_hp = FastSOS(butter(2, 150, 'highpass', fs=RATE, output='sos'))
            self.flt_b = FastSOS(butter(2, (45, 160), 'bandpass', fs=RATE, output='sos'))
            self.flt_h = FastSOS(butter(2, 90, 'highpass', fs=RATE, output='sos'))
            self.flt_t = FastSOS(butter(1, 6000, 'highpass', fs=RATE, output='sos'))
        self.a = math.exp(-2 * math.pi * 300 / RATE)     # one-pole high-pass at 300 Hz
        self.zi = np.zeros(1)
        self.gate = 0.0
        self.rel = math.exp(-HOP / RATE / 0.080)
        self.env = 0.02                                   # slow loudness follower for the auto gain
        self.env_rel = math.exp(-HOP / RATE / 3.0)

    def _hp(self, x):
        y, self.zi = lfilter([self.a, -self.a], [1.0, -self.a], x, zi=self.zi)
        return y

    def process(self, left, right, impact):
        if self.mode in ('关闭', '仅关键事件') or self.volume <= 0:
            return None
        if self.smart is not None:
            self.smart.scene = self.scene
            return self.smart.process(left, right)
        if self.mode == '原声':
            pk = max(float(np.max(np.abs(left))), float(np.max(np.abs(right)))) if len(left) else 0.0
            x = (left + right) * 0.5
            y = self.flt_hp.step(x, pk)
            b = self.flt_b.step(x, pk)
            # virtual bass: rectifying the 45-160 Hz part creates its harmonics (90-480 Hz), which the
            # small speaker can play; the ear fills the missing fundamental back in
            h = self.flt_h.step(np.abs(b))
            t = self.flt_t.step(x, pk)
            y = y + 1.4 * h - 0.35 * t                     # bass back, top -3 dB (less harsh)
            return np.tanh(y * self.volume * 1.2).astype(np.float32)
        mono = self._hp(((left + right) * 0.5).astype(np.float64))
        rms = float(np.sqrt(np.mean(mono * mono))) if len(mono) else 0.0
        self.env = rms if rms > self.env else max(rms, self.env * self.env_rel)
        mono = mono * min(10.0, 0.22 / max(self.env, 0.004))   # auto gain up to x10 (silence stays silent)
        if self.mode == '冲击声':
            target = min(1.0, max(0.0, (impact - 0.05) / 0.25))
            start = self.gate
            self.gate = target if target > self.gate else max(target, self.gate * self.rel)
            env = np.linspace(start, self.gate, len(mono))
            mono = mono * env * 1.6
        else:
            mono = mono * self.boost
        s0 = self.scene_now; self.scene_now += (self.scene - self.scene_now) * 0.05   # AI scene gain, smoothed
        mono = mono * np.linspace(s0, self.scene_now, len(mono))
        return np.tanh(mono * self.volume).astype(np.float32)
