"""Event-driven haptic tuning ("事件特调") layered on top of the game's own DualSense output.

Reads the PS5EventLog UE4SS mod's live log (events-*.tsv, one line per game event, flushed at
once) and, per matching rule:
  * adds short synthesized grip waveforms (voice coils) - clicks, thumps, sweeps, ramps;
  * scales the game's native haptic waveform for a while (native_gain), e.g. to make a normal
    parry lighter than a perfect one, or to grade ForceFeedbackLevel1..4 properly;
  * overrides the adaptive triggers for a few hundred ms, then hands them back to the game.
Rules live in event_fx.json next to the exe (written with defaults on first run).
"""
import collections
import copy
import json
import math
import pathlib
import time
import numpy as np
from haptic_dsp import RATE
from ds_hid import effect_vibration, effect_feedback

DEFAULT_LOG_DIR = r'C:\Program Files (x86)\Steam\steamapps\common\PBZ\platform\steam\ue4ss\Mods\PS5EventLog\logs'   # fallback; the app searches all Steam libraries

# voice: freq Hz, amp 0..1, dur ms, attack ms, decay ms (exp time constant, 0 = flat),
#        delay ms, pan [L, R] or pan_from/pan_to (sweep), amp_to (linear ramp)
from pbz_rules import RULES as DEFAULT_RULES   # Phantom Blade Zero rules, designed in pbz_rules.py
from ww_rules import RULES as WW_RULES         # Wuthering Waves rules (screen-read skill states)
DEFAULT_CONFIG = {"enabled": True, "strength": 1.0, "log_dir": DEFAULT_LOG_DIR, "rules": DEFAULT_RULES}


def load_config(root):
    path = pathlib.Path(root) / 'event_fx.json'
    if not path.exists():
        path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=1), 'utf8')
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        cfg = json.loads(path.read_text('utf-8-sig'))
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        return cfg
    except Exception:
        return copy.deepcopy(DEFAULT_CONFIG)


class EventTail:
    """Follow the newest events-*.tsv in log_dir, starting at its end (no replay of old events)."""

    def __init__(self, log_dir):
        self.dir = pathlib.Path(log_dir); self.path = None; self.fh = None; self.buf = ''
        self.next_check = 0.0

    def _newest(self):
        files = sorted(self.dir.glob('events-*.tsv'), key=lambda p: p.stat().st_mtime) if self.dir.exists() else []
        return files[-1] if files else None

    def close(self):
        if self.fh:
            self.fh.close(); self.fh = None

    def poll(self):
        now = time.monotonic()
        if now >= self.next_check:
            self.next_check = now + 1.0
            newest = self._newest()
            if newest is not None and newest != self.path:
                if self.fh: self.fh.close()
                first = self.path is None
                self.path = newest; self.fh = open(newest, 'r', encoding='utf8', errors='replace'); self.buf = ''
                if first: self.fh.seek(0, 2)          # app started mid-session: skip history
        if not self.fh:
            return []
        chunk = self.fh.read()
        if not chunk:
            return []
        self.buf += chunk
        lines = self.buf.split('\n'); self.buf = lines.pop()
        out = []
        for ln in lines:
            p = ln.split('\t')
            if len(p) >= 3:
                out.append((p[1], p[2], p[3] if len(p) > 3 else ''))
        return out


def rule_matches(rule, kind, fn, detail):
    if rule.get('kind') and rule['kind'] != kind:
        return False
    if any(s not in fn for s in rule.get('fn_has', [])):
        return False
    if any(s in fn for s in rule.get('fn_not', [])):
        return False
    if any(s not in detail for s in rule.get('detail_has', [])):
        return False
    if any(s in detail for s in rule.get('detail_not', [])):
        return False
    # a perfect parry must not also match when it was really a perfect dodge
    if kind == 'deflect' and '|dodge' in detail and '|dodge' not in ''.join(rule.get('detail_has', [])):
        return False
    return True


class EventFX:
    def __init__(self, cfg, log_dir=None):
        self.cfg = cfg; self.strength = float(cfg.get('strength', 1.0))
        self.tail = EventTail(log_dir or cfg.get('log_dir') or DEFAULT_LOG_DIR)
        self.voices = []                       # [start_sample, voice dict, group]
        self.clock = 0                         # samples rendered so far
        self.native = {}                       # channel -> (gain, until_sample, priority); channels multiply
        self.native_now = 1.0
        self.trig = {'L': (None, 0), 'R': (None, 0)}
        self.state_trig = {'L': None, 'R': None}   # persistent effect while a game state holds
        self.speaker_until = 0                     # controller speaker opened by an event until this sample
        self.speaker_eq = None
        self.sound_tail = None                     # voice_tail.SoundTail: ends follow-the-sound moments
        self.follow = None                         # (min_until, max_until, start) of the moment being followed
        self.regrab = None                         # (until, max_until, start): closed, but a voice line may resume
        self.last_len = None                       # length of the last followed moment, seconds (shown in the UI)
        self.rules = list(cfg.get('rules', [])) + list(cfg.get('ww_rules', WW_RULES))
        self.groups = {}                       # group -> (priority, start_sample, until_sample)
        self.last_fire = {}; self.recent = collections.deque(maxlen=6); self.count = 0

    # ---------------------------------------------------------------- events
    def feed(self, kind, fn, detail):
        for i, rule in enumerate(self.rules):
            if not rule_matches(rule, kind, fn, detail):
                continue
            key = rule.get('name', str(i))
            cd = rule.get('cooldown', 0) * RATE / 1000
            if key in self.last_fire and self.clock - self.last_fire[key] < cd:
                return None
            # one event per group at a time: a stronger event in the same group masks weaker ones
            # (the game often reports a guard break, a light hit and a force-feedback level within 1 ms)
            group = rule.get('group'); prio = rule.get('priority', 0)
            if group:
                g = self.groups.get(group)
                if g and self.clock < g[2]:
                    if prio < g[0]:
                        return None
                    if prio > g[0] and self.clock - g[1] < 0.06 * RATE:
                        self.voices = [v for v in self.voices if v[2] != group]   # upgrade: drop the weaker cue
                until = self.clock + int(rule.get('suppress_ms', 150) * RATE / 1000)
                self.groups[group] = (prio, self.clock, until)
                for other in rule.get('masks', []):          # e.g. a guard break silences parry/attack cues
                    self.groups[other] = (prio, self.clock, until)
                    self.voices = [v for v in self.voices if v[2] != other]
            self.last_fire[key] = self.clock
            self.fire(rule)
            return key
        return None

    def fire(self, rule):
        if rule.get('speaker_follow') and self.sound_tail is not None:
            # length unknown (differs per character, voice line outlives the animation): open now,
            # close when the game's voice / effects are over (listen()), within [min, max]
            lo = self.clock + int(rule.get('speaker_min_ms', 1000) * RATE / 1000)
            hi = self.clock + int(rule.get('speaker_max_ms', 10000) * RATE / 1000)
            if self.follow is None:
                self.regrab = None; self.sound_tail.arm(); self.follow = (lo, hi, self.clock)
            else:                                             # re-cast while open: extend
                self.follow = (max(lo, self.follow[0]), max(hi, self.follow[1]), self.follow[2])
            self.speaker_until = max(self.speaker_until, self.follow[1])
            self.speaker_eq = rule.get('speaker_eq')
        elif rule.get('speaker_ms'):
            self.speaker_until = max(self.speaker_until, self.clock + int(rule['speaker_ms'] * RATE / 1000))
            self.speaker_eq = rule.get('speaker_eq')          # event EQ preset for the speaker
        for v in rule.get('grip', []):
            self.voices.append([self.clock + int(v.get('delay', 0) * RATE / 1000), v, rule.get('group')])
        if 'native_gain' in rule:
            prio = rule.get('priority', 0)
            ch = 'level' if rule.get('kind') == 'ffb' and 'ForceFeedbackLevel' in ''.join(rule.get('detail_has', [])) else 'event'
            cur = self.native.get(ch)
            if cur is None or self.clock >= cur[1] or prio >= cur[2]:
                self.native[ch] = (float(rule['native_gain']), self.clock + int(rule.get('native_ms', 300) * RATE / 1000), prio)
        t = rule.get('trigger')
        if t:
            if t.get('effect') == 'feedback':
                eff = effect_feedback(int(t.get('strength', 6)), int(t.get('zone', 0)))
            else:
                eff = effect_vibration(int(t.get('amp', 6)), int(t.get('freq', 40)), int(t.get('zone', 0)))
            until = self.clock + int(t.get('ms', 200) * RATE / 1000)
            for side in t.get('side', 'R'):
                self.trig[side] = (eff, until)
        self.count += 1; self.recent.append(rule.get('name', '?'))

    def poll(self):
        fired = []
        for kind, fn, detail in self.tail.poll():
            k = self.feed(kind, fn, detail)
            if k: fired.append(k)
        return fired

    # ---------------------------------------------------------------- output
    def native_gain(self, n):
        """Per-sample gain ramp for the game's native haptics over the next n samples."""
        target = 1.0
        for gain, until, _p in self.native.values():
            if self.clock < until:
                target *= gain
        g = np.linspace(self.native_now, target, n, dtype=np.float32)
        self.native_now = target
        return g

    def render(self, n):
        l = np.zeros(n, np.float32); r = np.zeros(n, np.float32)
        keep = []
        for item in self.voices:
            start, v = item[0], item[1]
            dur = int(v.get('dur', 100) * RATE / 1000)
            t0 = self.clock - start                       # sample offset of this block in the voice
            if t0 + n <= 0:
                keep.append(item); continue
            if t0 >= dur:
                continue
            idx = np.arange(max(0, t0), min(dur, t0 + n))
            off = idx - t0
            ts = idx / RATE
            att = max(1e-4, v.get('attack', 2) / 1000); dec = v.get('decay', 0) / 1000
            env = np.minimum(1.0, ts / att)
            if dec > 0:
                env = env * np.exp(-np.maximum(0.0, ts - att) / dec)
            amp = v.get('amp', 0.5)
            if 'amp_to' in v:
                amp = amp + (v['amp_to'] - amp) * (idx / max(1, dur))
            frac = idx / max(1, dur)
            if 'pan_from' in v:
                pf, pt = v['pan_from'], v.get('pan_to', v['pan_from'])
                pl = pf[0] + (pt[0] - pf[0]) * frac; pr = pf[1] + (pt[1] - pf[1]) * frac
            else:
                pl, pr = v.get('pan', [1, 1])
            f0 = v.get('freq', 100)
            if 'freq_to' in v:                            # linear chirp: phase = 2*pi*(f0*t + (f1-f0)*t^2/(2*T))
                T = max(1e-3, dur / RATE)
                phase = 2 * math.pi * (f0 * ts + (v['freq_to'] - f0) * ts * ts / (2 * T))
            else:
                phase = 2 * math.pi * f0 * ts
            if 'am_freq' in v:                            # wobble (stagger / dizziness)
                env = env * (1 - v.get('am_depth', 0.5) * (0.5 - 0.5 * np.cos(2 * math.pi * v['am_freq'] * ts)))
            wave = np.sin(phase) * env * amp * self.strength
            l[off] += (wave * pl).astype(np.float32); r[off] += (wave * pr).astype(np.float32)
            if t0 + n < dur:
                keep.append(item)
        self.voices = keep
        self.clock += n
        return l, r

    def listen(self, l, r):
        """Game audio of this hop (before render advances the clock)."""
        t = self.sound_tail
        if t is None:
            return
        t.push(l, r)
        g = self.regrab
        if g is not None:
            if self.clock >= g[0] or self.clock >= g[1]:
                self.regrab = None; t.disarm()
            elif t.prob > t.SPEECH_ON:                        # the line went on after a long pause: re-open
                self.follow = (self.clock, g[1], g[2]); self.regrab = None
                self.speaker_until = max(self.speaker_until, g[1])
        f = self.follow
        if f is None:
            return
        lo, hi, start = f
        if (self.clock >= lo and not t.busy()) or self.clock >= hi:
            end = min(hi, self.clock)                         # the hold time already let the last syllable ring
            self.speaker_until = end
            self.last_len = round((end - start) / RATE, 2)
            self.follow = None
            if self.clock < hi:
                self.regrab = (self.clock + RATE, hi, start)  # listen 1 s more for the rest of the line
            else:
                t.disarm()

    def speaker_on(self):
        return self.clock < self.speaker_until

    def set_state_trigger(self, side, effect):
        self.state_trig[side] = effect

    def triggers(self):
        """(left, right): a timed event effect if active, else the state effect, else None."""
        out = []
        for side in 'LR':
            eff, until = self.trig[side]
            out.append(eff if eff is not None and self.clock < until else self.state_trig[side])
        return tuple(out)
