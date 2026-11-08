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

RATE = 48000
HOP = 480            # 10 ms (was 5 ms: half the per-block work, latency +5 ms)
NFFT = 1024
BIN_HZ = RATE / NFFT

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
        self.hist = collections.deque(maxlen=int(8 * RATE / HOP))
        self.gain_db = 0.0
        self.target = target_db; self.max_boost = max_boost; self.max_cut = max_cut; self.rise = rise_db_s

    def update(self, level_db):
        if level_db > -90:
            self.hist.append(level_db)
        if len(self.hist) < 40:
            return 10 ** (self.gain_db / 20)
        peak = float(np.percentile(np.fromiter(self.hist, float), 97.5))
        want = max(-self.max_cut, min(self.max_boost, self.target - peak))
        step = self.rise * HOP / RATE
        if want < self.gain_db:
            self.gain_db = max(want, self.gain_db - 15 * step)
        else:
            self.gain_db = min(want, self.gain_db + step)
        return 10 ** (self.gain_db / 20)


class STFT:
    """Shared per-channel spectrum (computed once, used by grip and trigger analyzers)."""

    def __init__(self):
        self.win = np.hanning(NFFT).astype(np.float32)
        self.buf = np.zeros(NFFT, np.float32)

    def step(self, block):
        self.buf = np.concatenate([self.buf[len(block):], block])
        return np.abs(np.fft.rfft(self.buf * self.win)) / (NFFT / 4)


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

    def step(self, mag, P=None):
        if self.H is None:
            self.H = mag.copy()
        d = mag - self.H
        self.H += np.where(d > 0, self.rise, self.fall) * d
        H = self.H
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
        med = float(np.median(self.flux_hist)) + 1e-6
        self.enhance *= math.exp(-HOP / RATE / 0.025)
        if self.lock > 0:
            self.lock -= 1
        elif flux / med > self.p['onset'] and flux > 0.5:
            self.enhance = 1.0; self.lock = int(0.035 * RATE / HOP)
        boost = 10 ** (self.p['accent_db'] * self.enhance / 20)
        return perc * boost


class BandMapper:
    """Impact spectrum -> per-band envelopes (attack ~1 ms = immediate per hop, release per band)."""

    def __init__(self, bands):
        self.bands = bands
        self.idx = [(int(lo / BIN_HZ), max(int(lo / BIN_HZ) + 1, int(hi / BIN_HZ))) for lo, hi, *_ in bands]
        self.env = np.zeros(len(bands))
        self.rel = np.array([math.exp(-HOP / RATE / (b[4] / 1000)) for b in bands])

    def step(self, spec, gain):
        out = np.zeros(len(self.bands))
        for i, ((a, b), (lo, hi, thr, gdb, rel_ms, car)) in enumerate(zip(self.idx, self.bands)):
            level = _db(np.sqrt(np.mean(spec[a:b] ** 2)) * gain)
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
        self.sos_low = butter(2, (35, 150) if style == '音乐' else (25, 150), 'bandpass', fs=RATE, output='sos')
        self.sos_hi = butter(2, (2500, 8000), 'bandpass', fs=RATE, output='sos')
        self.zl = sosfilt_zi(self.sos_low) * 0.0; self.zh = sosfilt_zi(self.sos_hi) * 0.0
        self.fast = [0.0, 0.0]; self.slow = [1e-4, 1e-4]; self.peak = [1e-3, 1e-3]
        self.kick = 0.0; self.tick = 0.0; self.rumble = 0.0; self.hold = [0, 0]
        dec = 0.07 if style == '音乐' else 0.25
        self.kick_dec = math.exp(-HOP / RATE / dec); self.tick_dec = math.exp(-HOP / RATE / 0.03)
        self.f_rel = math.exp(-HOP / RATE / 0.025); self.s_k = math.exp(-HOP / RATE / 0.4)
        self.p_rel = math.exp(-HOP / RATE / 6.0); self.r_k = math.exp(-HOP / RATE / 0.08)
        self.ph = np.zeros(3); self.car = np.array([55.0 if style == '音乐' else 45.0, 170.0, 45.0])
        self.prev = np.zeros(3)

    def _band(self, i, y):
        e = float(np.sqrt(np.mean(y * y))) if len(y) else 0.0
        self.fast[i] = e if e > self.fast[i] else self.fast[i] * self.f_rel
        self.slow[i] = self.slow[i] * self.s_k + e * (1 - self.s_k)
        self.peak[i] = e if e > self.peak[i] else max(1e-4, self.peak[i] * self.p_rel)
        norm = e / self.peak[i]
        onset = self.fast[i] / (self.slow[i] + 1e-6)
        return norm, onset

    def step(self, left, right):
        """-> (grip_l, grip_r, trig_l, trig_r); zeros in 标准."""
        n = len(left)
        if self.style not in ('电影', '音乐') or n == 0:
            return None
        x = ((left + right) * 0.5).astype(np.float64)
        yl, self.zl = sosfilt(self.sos_low, x, zi=self.zl); yh, self.zh = sosfilt(self.sos_hi, x, zi=self.zh)
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
        k = np.arange(n)
        ramp = self.prev[:, None] + (amps - self.prev)[:, None] * (k / n)[None, :]
        ph = self.ph[:, None] + 2 * np.pi * self.car[:, None] * k[None, :] / RATE
        w = ramp * np.sin(ph)
        self.ph = (self.ph + 2 * np.pi * self.car * n / RATE) % (2 * np.pi); self.prev = amps
        self.kick *= self.kick_dec; self.tick *= self.tick_dec
        gl = w[0] + w[2]; gr = w[0] + w[1] + w[2]
        return gl.astype(np.float32), gr.astype(np.float32), tl, tr


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
        self.phase = np.zeros(len(self.carriers))
        self.prev_amp_l = np.zeros(len(self.carriers)); self.prev_amp_r = np.zeros(len(self.carriers))
        self.agc = AutoGain()
        self.grip_gain = grip_gain; self.trigger_gain = trigger_gain
        self.duck = 1.0
        self.level = 0.0
        self.impact = 0.0

    def _synth(self, amps, prev):
        n = np.arange(HOP)
        ramp = prev[:, None] + (amps - prev)[:, None] * (n / HOP)[None, :]   # per-sample amplitude ramp
        ph = self.phase[:, None] + 2 * np.pi * self.carriers[:, None] * n[None, :] / RATE
        return ramp, ph

    def process(self, left, right, trigger_pressed=(0.0, 0.0)):
        rms = float(np.sqrt(np.mean((left ** 2 + right ** 2) / 2)) + 1e-9)
        g = self.agc.update(_db(rms))
        ml, mr = self.sl_fft.step(left), self.sr_fft.step(right)
        span = self.gl.span                                     # one broadband median per side, shared by grip + trigger
        pl_, pr_ = median_filter(ml, size=span, mode='nearest'), median_filter(mr, size=span, mode='nearest')
        sl, sr = self.gl.step(ml, pl_), self.gr.step(mr, pr_)
        al, ar = self.ml.step(sl, g), self.mr.step(sr, g)
        self.impact = float(max(np.max(al), np.max(ar)))      # before user gain/duck: drives the speaker gate
        # grips step back while a trigger is held, so the trigger feedback reads clearly (keeps 30 %)
        press = max(trigger_pressed)
        target_duck = 1.0 - 0.7 * min(1.0, max(0.0, (press - 0.12) / 0.18))
        self.duck += (target_duck - self.duck) * 0.3
        al = al * self.grip_gain * self.duck; ar = ar * self.grip_gain * self.duck
        rl, ph = self._synth(al, self.prev_amp_l)
        wl = np.sum(rl * np.sin(ph), axis=0)
        rr, _ = self._synth(ar, self.prev_amp_r)
        wr = np.sum(rr * np.sin(ph + np.pi), axis=0)          # right coil inverted (two hands, not one centred buzz)
        self.phase = (self.phase + 2 * np.pi * self.carriers * HOP / RATE) % (2 * np.pi)
        self.prev_amp_l, self.prev_amp_r = al, ar
        # trigger envelopes: mostly its own side, 30 % of the centre so centred hits reach both
        tl = float(np.max(self.tml.step(self.tl.step(ml, pl_ if self.tl.span == span else None), g)))
        tr = float(np.max(self.tmr.step(self.tr.step(mr, pr_ if self.tr.span == span else None), g)))
        mix = (tl + tr) / 2
        trig = (trigger_curve((0.3 * mix + 0.7 * tl) * self.trigger_gain), trigger_curve((0.3 * mix + 0.7 * tr) * self.trigger_gain))
        self.level = float(max(np.max(al), np.max(ar)))
        gv = self.groove.step(left, right)
        if gv is not None:                                     # 电影 / 音乐: rhythm + low end on top
            gl, gr, gtl, gtr = gv
            wl = wl + gl * self.grip_gain * self.duck; wr = wr + gr * self.grip_gain * self.duck
            trig = (max(trig[0], min(1.0, gtl * self.trigger_gain)), max(trig[1], min(1.0, gtr * self.trigger_gain)))
            self.level = max(self.level, float(np.max(np.abs(gl))))
        return np.tanh(wl * 1.2).astype(np.float32), np.tanh(wr * 1.2).astype(np.float32), trig


SPEAKER_MODES = ('关闭', '原声', '特殊事件')
SPEAKER_EQS = ('原味', '低音饱满', '人声清晰')
SPEAKER_MODE_ALIAS = {'全部游戏声音': '原声', '冲击声': '特殊事件', '仅关键事件': '特殊事件', '仅大招/特殊技': '特殊事件',
                      '智能增强': '原声'}


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
               'clang': (0.0, 0.7, 0.15), 'whoosh': (0.2, 0.5, 0.1), 'big': (1.0, 0.25, 0.0), 'thud': (1.1, 0.0, -0.25),
               None: (0.4, 0.3, 0.0)}

    def __init__(self):
        def f(*a, **k):
            sos = butter(*a, fs=RATE, output='sos', **k); return [sos, sosfilt_zi(sos) * 0.0]
        self.hp = f(2, 80, 'highpass'); self.lo = f(2, (45, 160), 'bandpass'); self.h_lp = f(2, 400, 'lowpass')
        self.h_hp = f(1, 70, 'highpass'); self.pr = f(2, (2000, 4500), 'bandpass'); self.top = f(1, 6000, 'highpass')
        self.parts = None; self.env = 0.05; self.lv = 0.05; self.mk = 1.0

    @staticmethod
    def _run(st, x):
        y, st[1] = sosfilt(st[0], x, zi=st[1]); return y

    def analyse(self, left, right):
        x = ((left + right) * 0.5).astype(np.float64)
        b = self._run(self.lo, x)
        self.env = max(abs(float(np.max(np.abs(b)))), self.env * 0.995)
        h = self._run(self.h_hp, self._run(self.h_lp, b * b / max(self.env, 1e-3)))   # square law -> 2nd harmonic (octave up), DC removed
        self.parts = (self._run(self.hp, x), h, self._run(self.pr, x), self._run(self.top, x))
        rms = float(np.sqrt(np.mean(self.parts[0] ** 2))) if len(x) else 0.0
        if rms > 1e-4:                                   # silence does not count (no boost creeping up)
            self.lv = self.lv * 0.98 + rms * 0.02
        self.mk += (min(4.0, max(1.0, 0.14 / max(self.lv, 1e-4))) - self.mk) * 0.02

    def render(self, preset, volume, boost=1.0):
        base, h, pr, top = self.parts; wb, wp, wt = self.PRESETS.get(preset, self.PRESETS['原味'])
        y = base + wb * h * float(np.sqrt(np.mean(base * base)) + 1e-4) * 4 + wp * pr + wt * top
        return np.tanh(y * volume * boost * 1.5 * self.mk).astype(np.float32)     # SmartSpeaker stays internal (key-event EQ)


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
        self.sos = [butter(2, b, 'bandpass', fs=RATE, output='sos') for b in self.BANDS]
        self.zi = [sosfilt_zi(s) * 0.0 for s in self.sos]
        self.fast = [0.0] * 3; self.slow = [1e-3] * 3; self.g = [1.0] * 3
        self.f_rel = math.exp(-HOP / RATE / 0.030); self.s_k = math.exp(-HOP / RATE / 0.35)
        self.env = 0.02; self.env_rel = math.exp(-HOP / RATE / 3.0)
        self.event = None; self.event_mix = 0.0; self.scene = 1.0; self.scene_now = 1.0

    def process(self, left, right, impact=None):
        if self.volume <= 0:
            return None
        x = ((left + right) * 0.5).astype(np.float64)
        out = np.zeros(len(x))
        target_mix = 1.0 if self.event else 0.0
        self.event_mix += (target_mix - self.event_mix) * 0.25
        preset = self.PRESETS.get(self.event, self.PRESETS[None])
        for i, sos in enumerate(self.sos):
            y, self.zi[i] = sosfilt(sos, x, zi=self.zi[i])
            e = float(np.sqrt(np.mean(y * y))) if len(y) else 0.0
            self.fast[i] = e if e > self.fast[i] else self.fast[i] * self.f_rel
            self.slow[i] = self.slow[i] * self.s_k + e * (1 - self.s_k)
            ratio = self.fast[i] / (self.slow[i] + 1e-5)
            tg = min(self.g_hi, max(self.g_lo, ratio ** self.g_exp)) * self.BASE[i] * (1.0 + (preset[i] - 1.0) * self.event_mix)
            out += y * np.linspace(self.g[i], tg, len(y)); self.g[i] = tg
        rms = float(np.sqrt(np.mean(out * out))) if len(out) else 0.0
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
            self.sos_hp = butter(2, 150, 'highpass', fs=RATE, output='sos'); self.z_hp = sosfilt_zi(self.sos_hp) * 0.0
            self.sos_b = butter(2, (45, 160), 'bandpass', fs=RATE, output='sos'); self.z_b = sosfilt_zi(self.sos_b) * 0.0
            self.sos_h = butter(2, 90, 'highpass', fs=RATE, output='sos'); self.z_h = sosfilt_zi(self.sos_h) * 0.0
            self.sos_t = butter(1, 6000, 'highpass', fs=RATE, output='sos'); self.z_t = sosfilt_zi(self.sos_t) * 0.0
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
            x = ((left + right) * 0.5).astype(np.float64)
            y, self.z_hp = sosfilt(self.sos_hp, x, zi=self.z_hp)
            b, self.z_b = sosfilt(self.sos_b, x, zi=self.z_b)
            # virtual bass: rectifying the 45-160 Hz part creates its harmonics (90-480 Hz), which the
            # small speaker can play; the ear fills the missing fundamental back in
            h, self.z_h = sosfilt(self.sos_h, np.abs(b), zi=self.z_h)
            t, self.z_t = sosfilt(self.sos_t, x, zi=self.z_t)
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
