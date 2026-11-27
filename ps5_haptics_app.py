"""PS5 手柄 · 声音触感 1.7

Game audio (WASAPI loopback of the output device) -> impact DSP -> DualSense
voice-coil grips (USB audio ch3/ch4) + trigger vibration (HID 0x26). Wired USB only.
"""
from __future__ import annotations
import os
# numpy's OpenBLAS reserved ~1.7 GB (a buffer per CPU thread) that slowly became resident;
# our maths is tiny, one thread is plenty. Must be set before numpy is imported.
for _v in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ.setdefault(_v, '1')
import collections, ctypes as C, json, pathlib, queue, sys, threading, time, traceback
from ctypes import wintypes as W
import tkinter as tk
import numpy as np
import pyaudiowpatch as pa
from scipy.signal import butter
from ds_audio import HapticOut, RATE, pa_new, pa_end, pa_open, rescan, close_stream
from ds_hid import DualSenseHID, effect_vibration, effect_off, effect_zones
import trigger_presets
from selfcheck import TL_KEYS
from haptic_dsp import FastSOS, HapticEngine, HOP, GRIP_PROFILES, TRIGGER_PROFILES, RawSpeaker, SPEAKER_MODES, SPEAKER_MODE_ALIAS, HAPTIC_STYLES
from button_haptics import ButtonClicks
from game_caps import GameCaps, NOT_GAMES
from pad_input import InputFilter, ButtonMap
from voice_gate import suppression_enabled
from takeover import VirtualDS4, VirtualX360, Isolation, IsolationUnavailable, RumbleCoil, fuse, FUSION_ALIAS
from xds import VirtualDualSense
from event_fx import EventFX, load_config as load_event_fx
from ww_vision import WWVision
from ww_bike import BoostResistance, DIVE_EFFECT
from ww_rules import STATE_TRIGGERS

ROOT = pathlib.Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else pathlib.Path(__file__).resolve().parent
CONFIG = ROOT / 'config.json'
DEFAULTS = {'grip_profile': '均衡', 'trigger_profile': '均衡冲击', 'grip_gain': 1.0, 'trigger_gain': 1.0,
            'skip_center': True, 'foreground_exe': '', 'source_hint': '',
            'mode': '原生透传', 'fusion': '叠加', 'trigger_mode': '自动', 'rumble_gain': 1.0, 'speaker_mode': '特殊事件', 'speaker_eq': '原味', 'speaker_volume': 0.8, 'haptic_style': '游戏', 'pbz_fx': True, 'ww_fx': True, 'ww_enabled': True,
            'cinema_rumble': 1.0, 'cinema_kick': 1.0, 'cinema_trigger': 1.0, 'music_kick': 1.0, 'music_tick': 1.0, 'music_trigger': 1.0, 'record_native': True, 'event_fx': True,
            'trigger_press_gate': True,
            **trigger_presets.scene_defaults(), 'user_games': [],
            'ww_vision': True, 'ww_skill_icons': False, 'ww_skill_shadow': False, 'ww_click_gate': True, 'ww_bike_swap': True, 'ww_bike_audio': True, 'game_profiles': True, 'capture': '自动', 'speaker_scene': True, 'pbz_log_dir': ''}


WW_EXE = 'client-win64-shipping.exe'
PBZ_EXE = 'pbz.exe'
# 影之刃零 stuck-vibration guard (reported after some moves, e.g. 少主剑 R1+△): in this game every trigger vibration
# the game sends is momentary and long coil rumbles (ship, slides) come with a stream of game events; so a trigger
# vibration held for seconds, motor values frozen for seconds, or coil rumble with no game event for seconds is a
# vibration the game may have forgotten to stop. Without event data, require a
# sustained plateau in both channels before suppressing the native waveform.
STUCK_TRIGGER_S, STUCK_MOTOR_S, STUCK_WAVE_S, QUIET_EVENTS_S = 3.0, 5.0, 6.0, 4.0
PBZ_STUCK_MOTOR_S = 2.5
WAVE_WINDOW_SAMPLES = RATE // 10


class StuckGuard:
    def __init__(self, log):
        self.log = log; self.seen = {}; self.said = set()
        self.reset_wave()

    def reset_wave(self):
        self.wave_since = None
        self.wave_reference = None
        self.wave_blocks = []
        self.wave_samples = 0
        self.wave_muted = False

    def reset(self):
        self.seen.clear()
        self.said.clear()
        self.reset_wave()

    def _say(self, what):
        if what not in self.said:
            self.said.add(what); self.log(f'检测到游戏的{what}疑似卡住，已自动松开；游戏换成新的效果时恢复。')

    def held(self, key, value, now):
        last = self.seen.get(key)
        if last is None or last[0] != value:
            self.seen[key] = (value, now); return 0.0
        return now - last[1]

    def trigger(self, side, eff, now):
        t = self.held('trig' + side, eff, now)
        if eff is not None and eff[0] == 0x26 and t > STUCK_TRIGGER_S:
            self._say('扳机震动'); return None
        return eff

    def motors(self, l, r, now, timeout=STUCK_MOTOR_S):
        t = self.held('motor', (l, r), now)
        if (l or r) and t >= timeout:
            self._say('马达震动'); return 0, 0
        return l, r

    def wave(self, left, right, last_event, now):
        """Watch the raw native coil signal, before it is mixed with other vibration."""
        if last_event and now - last_event <= QUIET_EVENTS_S:
            self.reset_wave()
            return False
        self.wave_blocks.append((left.copy(), right.copy()))
        self.wave_samples += len(left)
        if self.wave_samples < WAVE_WINDOW_SAMPLES:
            return self.wave_muted
        signature = []
        for blocks in zip(*self.wave_blocks):
            signal = np.concatenate(blocks)
            magnitude = np.abs(signal)
            peak = float(magnitude.max())
            level = float(magnitude.mean())
            # For a sine, total travel per second is 4 * amplitude * frequency.
            # Also track mean magnitude to distinguish pulses with different duty cycles.
            rate = (float(np.abs(np.diff(signal)).sum()) * RATE / (4 * peak * len(signal))
                    if peak >= 0.05 else 0.0)
            signature.append((peak, level, rate))
        self.wave_blocks.clear()
        self.wave_samples = 0
        if max(channel[0] for channel in signature) < 0.15:
            self.reset_wave()
            return False
        # Compare with the first window so a slow sweep can rearm the guard.
        if self.wave_reference is None or any(
            abs(current - original) > max(floor, relative * original)
            for channel, reference in zip(signature, self.wave_reference)
            for current, original, floor, relative in zip(channel, reference,
                                                          (0.025, 0.025, 12.0), (0.08, 0.12, 0.15))
        ):
            self.wave_reference = signature
            self.wave_since = now
            self.wave_muted = False
        elif now - self.wave_since >= STUCK_WAVE_S:
            self.wave_muted = True
            self._say('原生握把震动')
        return self.wave_muted


# per-game tuning applied while that game is in the foreground
GAME_PROFILES = {
    WW_EXE: {'name': '鸣潮', 'rumble_scale': 0.6, 'duck': 0.25, 'grip_profile': '细节丰富', 'vision': True},
}


def foreground_window():
    get_window = C.windll.user32.GetForegroundWindow
    get_window.restype = W.HWND
    return get_window()


def foreground_process(hwnd=None):
    u = C.windll.user32; k = C.windll.kernel32
    hwnd = hwnd or foreground_window()
    pid = W.DWORD(); u.GetWindowThreadProcessId(W.HWND(hwnd), C.byref(pid))
    k.OpenProcess.restype = W.HANDLE
    h = k.OpenProcess(0x1000, False, pid.value)
    if not h:
        # protected processes (anti-cheat) may refuse even limited queries: fall back to the title
        title = C.create_unicode_buffer(256); u.GetWindowTextW(W.HWND(hwnd), title, 256)
        return pid.value, WW_EXE if ('鸣潮' in title.value or 'Wuthering' in title.value) else ''
    try:
        buf = C.create_unicode_buffer(1024); n = W.DWORD(1024)
        name = pathlib.Path(buf.value).name.lower() if k.QueryFullProcessImageNameW(W.HANDLE(h), 0, buf, C.byref(n)) else ''
        return pid.value, name
    finally:
        k.CloseHandle(W.HANDLE(h))


def foreground_exe(hwnd=None):
    return foreground_process(hwnd)[1]


class _PMC(C.Structure):
    _fields_ = [('cb', W.DWORD), ('PageFaultCount', W.DWORD), ('PeakWorkingSetSize', C.c_size_t),
                ('WorkingSetSize', C.c_size_t), ('QuotaPeakPagedPoolUsage', C.c_size_t),
                ('QuotaPagedPoolUsage', C.c_size_t), ('QuotaPeakNonPagedPoolUsage', C.c_size_t),
                ('QuotaNonPagedPoolUsage', C.c_size_t), ('PagefileUsage', C.c_size_t), ('PeakPagefileUsage', C.c_size_t)]


def proc_mem_mb():
    """Private bytes of this process in MB (diagnostics for the memory growth seen on 2026-11-07)."""
    k = C.windll.kernel32
    k.GetCurrentProcess.restype = W.HANDLE
    k.K32GetProcessMemoryInfo.argtypes = [W.HANDLE, C.POINTER(_PMC), W.DWORD]
    c = _PMC(); c.cb = C.sizeof(c)
    k.K32GetProcessMemoryInfo(k.GetCurrentProcess(), C.byref(c), c.cb)
    return f'ws={c.WorkingSetSize >> 20}MB private={c.PagefileUsage >> 20}MB'


class Resampler:
    """Stream resampler with 4-point Catmull-Rom cubic interpolation and phase carried across blocks.

    Keeps a 3-sample history and exact continuity across unity (1.0) and fractional drift ratios.
    ratio = input samples consumed per output sample.
    """

    def __init__(self):
        self.pos = 1.0
        self.hist_l = None
        self.hist_r = None

    def process(self, l, r, ratio):
        m = len(l)
        if m == 0:
            return l, r
        if self.hist_l is None:
            self.hist_l = np.full(3, l[0], np.float32)
            self.hist_r = np.full(3, r[0], np.float32)
            self.pos = 1.0
        xl = np.concatenate((self.hist_l, l))
        xr = np.concatenate((self.hist_r, r))
        self.hist_l = xl[-3:].copy()
        self.hist_r = xr[-3:].copy()
        if abs(ratio - 1.0) < 1e-9 and abs(self.pos - 1.0) < 1e-9:
            self.pos = 1.0
            return xl[1:1 + m].astype(np.float32), xr[1:1 + m].astype(np.float32)
        limit = len(xl) - 2.0
        if self.pos >= limit - 1e-9:
            self.pos = max(1.0, self.pos - m)
            return np.zeros(0, np.float32), np.zeros(0, np.float32)
        n = int((limit - 1e-9 - self.pos) / ratio) + 1
        p = self.pos + np.arange(n, dtype=np.float64) * ratio
        p = p[p < limit - 1e-9]
        if not len(p):
            self.pos = max(1.0, self.pos - m)
            return np.zeros(0, np.float32), np.zeros(0, np.float32)
        self.pos = max(1.0, (self.pos + len(p) * ratio) - m)
        idx = np.clip(np.floor(p).astype(np.intp), 1, len(xl) - 3)
        t = p - idx

        def interp(x):
            y0 = x[idx - 1].astype(np.float64)
            y1 = x[idx].astype(np.float64)
            y2 = x[idx + 1].astype(np.float64)
            y3 = x[idx + 2].astype(np.float64)
            c0 = y1
            c1 = 0.5 * (y2 - y0)
            c2 = y0 - 2.5 * y1 + 2.0 * y2 - 0.5 * y3
            c3 = 0.5 * (y3 - y0) + 1.5 * (y1 - y2)
            return (((c3 * t + c2) * t + c1) * t + c0).astype(np.float32)

        return interp(xl), interp(xr)


def drift_speed(total_buf, prev_speed=1.0, has_ring=True):
    """Smooth bidirectional clock-drift compensation targeting ~30 ms total buffering."""
    if total_buf > 0.040:
        target = 1.0 + min(0.025, (total_buf - 0.040) * 0.5)
    elif has_ring and total_buf < 0.020:
        target = 1.0 - min(0.015, (0.020 - total_buf) * 0.8)
    else:
        target = 1.0
    speed = prev_speed + (target - prev_speed) * 0.05
    if target == 1.0 and abs(speed - 1.0) < 0.0005:
        return 1.0
    return speed


def downmix(frames, ch, skip_center):
    """Interleaved float32 -> (L, R). 5.1/7.1: centre (dialogue) optionally excluded."""
    x = frames.reshape(-1, ch)
    if ch == 1:
        return x[:, 0], x[:, 0]
    l, r = x[:, 0].copy(), x[:, 1].copy()
    if ch >= 6:
        if not skip_center:
            l += 0.5 * x[:, 2]; r += 0.5 * x[:, 2]
        l += 0.7 * x[:, 4]; r += 0.7 * x[:, 5]
    if ch >= 8:
        l += 0.7 * x[:, 6]; r += 0.7 * x[:, 7]
    return l, r


def groove_gains(cfg):
    """User gains for the 影院 / 律动 layer (mode cards' settings)."""
    st = cfg.get('haptic_style', '游戏')
    if st in ('影院', '电影'):
        return dict(kick=cfg.get('cinema_kick', 1.0), rumble=cfg.get('cinema_rumble', 1.0), trigger=cfg.get('cinema_trigger', 1.0))
    if st in ('律动', '音乐'):
        return dict(kick=cfg.get('music_kick', 1.0), tick=cfg.get('music_tick', 1.0), trigger=cfg.get('music_trigger', 1.0))
    return None


GSP_LOWCUT = butter(2, 150, 'highpass', fs=RATE, output='sos')
CONTROL_HAPTICS = {'button': 'button_haptics', 'trigger': 'trigger_haptics', 'stick': 'stick_haptics', 'touch': 'touch_haptics'}


def smooth_trigger_amp(raw_amp, prev_active, hold_until, now, hold_s=0.15, pressed=(True, True)):
    """Hysteresis + release hold for audio-driven triggers to stop rapid gear-clutch clicking."""
    out = [0, 0]
    active = list(prev_active)
    holds = list(hold_until)
    for i in (0, 1):
        if not pressed[i]:
            active[i] = False; holds[i] = 0.0; out[i] = 0
            continue
        a = raw_amp[i]
        if active[i]:
            if a >= 1:
                out[i] = a; holds[i] = now + hold_s
            elif now < holds[i]:
                out[i] = 1
            else:
                active[i] = False; out[i] = 0
        elif a >= 2:
            active[i] = True; out[i] = a; holds[i] = now + hold_s
    return (out[0], out[1]), active, holds


def stabilize_native_trigger(eff, last_phys, pressed, press_gate=True):
    """Stop idle/combat native trigger gear chatter (e.g. 0x22 <-> 0x05 pulses while triggers are unpressed)."""
    is_off = eff is None or eff[0] in (0x00, 0x05)
    eff = effect_off() if is_off else eff
    if not press_gate:
        return eff
    if eff[0] == 0x26 and not pressed:
        return last_phys if (last_phys is not None and last_phys[0] in (0x21, 0x22, 0x25)) else effect_off()
    if is_off and not pressed and last_phys is not None and last_phys[0] in (0x21, 0x22, 0x25):
        return last_phys
    return eff


def speaker_route(mode, pure, adapted, native_speaking, direct_speaking):
    if mode != '特殊事件':
        return mode
    if native_speaking:
        return '游戏原生'
    if direct_speaking:
        return '游戏接管'
    if adapted:
        return mode
    return '静默' if pure else '全部游戏声'


def capture_request(cfg, game_exes=()):
    setting = cfg.get('capture', '自动')
    if setting == '锁定进程':
        return 'pinned', pathlib.PureWindowsPath(cfg.get('capture_exe', '').strip().strip('"')).name.casefold()
    if setting == '仅游戏进程' or (setting == '自动' and cfg.get('scene', '游戏') == '游戏'):
        return 'game', tuple(game_exes)
    return ('auto' if setting == '自动' else 'mix'), None


class Runner:
    def __init__(self, cfg, log):
        self.base = cfg; self.profile = None   # base: the UI's settings; cfg: base + the foreground game's own settings
        self.timeline = collections.deque(maxlen=600)   # haptic sources, 10 per second, for the self-check report
        self.cfg = cfg; self.log = log; self.stop_ev = threading.Event(); self.q = queue.Queue(maxsize=400)   # ~2 s of blocks: the lag control drains it, nothing is dropped
        self.start_cfg = dict(cfg)          # what this run was started with (routing / source need a restart)
        self.caps = GameCaps()              # which games drive the pad speaker / triggers themselves
        self.pad_filter = None              # stick deadzones / gyro bias for the virtual pad
        self.live_ver = 0                   # bumped by the UI when settings change: applied within 0.5 s
        self.proc = None
        self.capture = capture_request(cfg)
        self.stats = {'level': 0.0, 'trigger': (0.0, 0.0), 'active': False, 'source': '', 'late': 0, 'rumble': (0.0, 0.0), 'vpad': None, 'takeover': False, 'native_trig': (False, False)}

    def profile_for(self, exe):
        """The person's own game card with its own settings for this exe (游戏 scene only)."""
        if not exe or self.base.get('scene', '游戏') != '游戏':
            return None
        for g in self.base.get('user_games') or []:
            if g.get('own') and pathlib.Path(g.get('exe', '')).name.lower() == exe:
                return g
        return None

    def effective(self, exe):
        g = self.profile_for(exe)
        if g is None:
            return self.base
        eff = dict(self.base); eff.update(g.get('settings') or {})
        return eff

    def user_exes(self):
        return [pathlib.Path(g.get('exe', '')).name.lower() for g in self.base.get('user_games') or [] if g.get('exe')]

    @property
    def capture_locked(self):
        return self.capture[0] in ('game', 'pinned')

    def _capture_request(self):
        games = (*GAME_PROFILES, PBZ_EXE, self.cfg.get('foreground_exe', '').strip().lower(), *self.user_exes())
        return capture_request(self.cfg, games)

    def _foreground_game(self, known):
        hwnd = foreground_window()
        if not hwnd:
            return None
        pid, exe = foreground_process(hwnd)
        if pid and exe not in NOT_GAMES and pid != os.getpid():
            from ui_scifi import other_fullscreen
            if exe in known or other_fullscreen(hwnd=hwnd):
                return pid, exe
        return None

    def _allow_system_mix(self):
        return not self.capture_locked and not (self.capture[0] == 'auto' and self.proc and self.proc.sounding())

    def _audio_cb(self, data, frames, info, status):
        if not self._allow_system_mix():
            return (None, pa.paContinue)
        try:
            self.q.put_nowait(np.frombuffer(data, np.float32).copy())
        except queue.Full:
            self.stats['late'] += 1
        return (None, pa.paContinue)

    def _open_stream(self, p, src):
        rate = int(src['defaultSampleRate'])
        return pa_open(p, format=pa.paFloat32, channels=src['maxInputChannels'], rate=rate, input=True,
                      input_device_index=src['index'], frames_per_buffer=int(rate * 0.005), stream_callback=self._audio_cb)

    def _refresh_stream(self, stream, p, src):
        if stream is not None:
            try:
                if stream.is_active():
                    return stream
            except OSError:
                pass
            close_stream(stream)
        try:
            stream = self._open_stream(p, src)
        except OSError:
            return None
        self.log('声音设备刚才重置过，已重新连接。')
        return stream

    def _open_loopback(self, hint_override=None):
        p = pa_new()
        try:
            hint = hint_override or self.cfg.get('source_hint')
            use_default = not hint
            if use_default:
                wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
                hint = p.get_device_info_by_index(wasapi['defaultOutputDevice'])['name']
                if 'dualsense' in hint.lower():
                    raise PermissionError('默认声音输出设备是 PS5 手柄本身。请在 Windows 声音设置里把默认输出改回音箱/电视，再点开始。')
            candidates = [d for d in p.get_loopback_device_info_generator()
                          if 'dualsense' not in d['name'].lower()]
            src = next((d for d in candidates
                        if d['name'].removesuffix(' [Loopback]').casefold() == hint.casefold()), None)
            if src is None and not use_default:
                matches = [d for d in candidates if hint.casefold() in d['name'].casefold()]
                if len(matches) == 1:
                    src = matches[0]
            if src is None:
                raise LookupError('找不到唯一的声音输出设备环回（loopback）：' + hint)
            return p, src, self._open_stream(p, src)
        except Exception:
            pa_end(p)
            raise

    def _open_audio(self, hint_override=None, output_name=None):
        for attempt in range(6):
            if self.stop_ev.is_set():
                raise RuntimeError('已取消')
            p = stream = out = None
            stage = '系统音频环回'
            try:
                rescan()
                if self.capture_locked:
                    p = pa_new()
                    src = {'name': '按进程截音', 'maxInputChannels': 2, 'defaultSampleRate': RATE}
                else:
                    p, src, stream = self._open_loopback(hint_override)
                stage = '手柄音频输出'
                out = HapticOut(p, output_name)
                return p, src, stream, out
            except PermissionError:
                raise
            except (LookupError, OSError) as exc:
                if attempt == 5:
                    raise RuntimeError(f'打开{stage}失败：{exc}') from exc
                self.log(f'{stage}暂不可用，正在重新枚举设备（{attempt + 1}/5）：{exc}')
            finally:
                if p is not None and out is None:
                    close_stream(stream)
                    pa_end(p)
            self.stop_ev.wait(0.75)

    def run(self):
        p = None; out = hid = stream = vpad = iso = None; error = None; vision = None; watcher = None; trace = None; volume = None
        music_gate = None
        voice_gate = None; pad_tap = None; g_env = 0.05; g_gain = 1.0; g_out_pk = 0.0; spk_clip = None; gsp_flt = FastSOS(GSP_LOWCUT)
        try:                                         # a game with its own settings already running: start with them (routing is fixed per run)
            import proc_loopback as plb
            here = foreground_exe()
            start_g = self.profile_for(here) or next((g for g in (self.profile_for(e) for e in self.user_exes()) if g and plb.find_pid(pathlib.Path(g['exe']).name)), None)
            if start_g is not None:
                self.cfg = self.effective(pathlib.Path(start_g['exe']).name.lower()); self.profile = start_g.get('name')
                self.log(f'使用「{self.profile}」的专属设置')
        except Exception:
            pass
        self.capture = self._capture_request()
        mode = self.cfg.get('mode')
        native = mode == '原生透传'                 # virtual DualSense: game's own triggers + haptics
        xbox = mode == 'Xbox'                       # virtual Xbox pad: games that only accept Xbox controllers
        takeover = mode == '接管' or native or xbox  # any mode that hides the real pad
        button_map = ButtonMap(self.start_cfg.get('button_map') if takeover else None)
        logical_report = bytes([1, 128, 128, 128, 128, 0, 0, 0, 8, 0])
        try:
            p, src, stream, out = self._open_audio()
            ch = src['maxInputChannels']; rate = int(src['defaultSampleRate'])
            self.stats['source'] = f"{src['name']}  {ch}声道 {rate}Hz"
            hid = DualSenseHID()                        # open the REAL pad before any virtual one exists
            real_out = out.dev['name']
            fw = hid.firmware_info()
            if fw:
                self.log(f"手柄固件：fw {fw['fw']:08x} / hw {fw['hw']:08x} / update {fw['update']:04x} / {fw['date']} {fw['time']}")

            def observe_ww_input(report):
                current = vision
                if current is not None:
                    current.notify_input(report)

            default_id = None; saved_default_id = None; saved_default_name = ''
            if native or not self.cfg.get('source_hint'):
                try:
                    from audio_default import DefaultOutputWatcher
                    watcher = DefaultOutputWatcher()
                    default_id, default_name = watcher.current_info()
                    if default_id and 'dualsense' not in default_name.lower():
                        saved_default_id, saved_default_name = default_id, default_name
                    elif default_name and 'dualsense' in default_name.lower() and self.capture_locked:
                        self.log('提示：当前 Windows 默认声音输出设备是 PS5 手柄，游戏主声音会只从手柄发声、电脑音箱无声；请在 Windows 声音设置里改回音箱/耳机。')
                except Exception as exc:
                    self.log('无法监视默认声音输出（不影响使用）：' + str(exc))

            if takeover:
                iso = Isolation(self.log)
                try:
                    iso.open(hid.path)
                except IsolationUnavailable as exc:
                    if not native:
                        raise
                    self.log(str(exc) + ' 已切到普通模式，声音震动可用，游戏直接连接真手柄。')
                    native = takeover = False
                    mode = '普通'
                    button_map = ButtonMap()
            if takeover:
                vpad = VirtualDualSense(self.log) if native else VirtualX360() if xbox else VirtualDS4()
                vpad.open()
                self.pad_filter = InputFilter(self.cfg.get('stick_dz_l', 0.0), self.cfg.get('stick_dz_r', 0.0), self.cfg.get('gyro_bias') or (0, 0, 0))
                def forward_input(report):
                    nonlocal logical_report
                    logical_report = button_map.apply(self.pad_filter.apply(report))
                    current = vision
                    if current is not None:
                        current.notify_input(logical_report)
                    before = vpad.trigger_press_times if native and current is not None else None
                    vpad.forward(logical_report)
                    if before is not None and before is not vpad.trigger_press_times:
                        for side, old, stamp in zip(('L', 'R'), before, vpad.trigger_press_times):
                            if stamp != old:
                                current.notify_trigger(side, stamp)

                hid.start_reader(forward_input, self.log)
                self.stats['takeover'] = True
                if native:                         # the game's own pad-speaker sound lands on the virtual pad
                    from pad_speaker import PadSpeakerTap, SpeakerClip
                    pad_tap = PadSpeakerTap(real_out, self.log, self.stop_ev); pad_tap.start()
                    if self.cfg.get('record_native'):
                        spk_clip = SpeakerClip(ROOT / 'traces')
            if self.cfg.get('speaker_mode', '冲击声') != '关闭':
                try: hid.enable_speaker(volume=0x64, headset=self.cfg.get('speaker_output') == '耳机孔')          # undo a mute / headset routing left by a game
                except Exception: pass
            try: hid.set_lightbar(0, 80, 255)
            except Exception: pass
            eng = HapticEngine(self.cfg['grip_profile'], self.cfg['trigger_profile'], self.cfg['grip_gain'], self.cfg['trigger_gain'],
                               self.cfg.get('haptic_style', '游戏'), groove_gains(self.cfg))
            ep_gain = 1.0; source_endpoint = False; pad_session_check = 0.0; game_speaker_active = False; pad_probe_busy = False; fg_since = time.monotonic(); last_game_trig = (None, None); trig_released = False
            def _probe_pad_speaker(exe_name):
                nonlocal game_speaker_active, pad_probe_busy
                try:
                    from pad_speaker import game_on_pad
                    res = game_on_pad(exe_name)
                    if fg == exe_name:
                        game_speaker_active = res
                except Exception:
                    pass
                finally:
                    pad_probe_busy = False
            rum = RumbleCoil(); clicks = ButtonClicks(); vol = self.cfg.get('speaker_volume', 0.6)
            spk_mode = SPEAKER_MODE_ALIAS.get(self.cfg.get('speaker_mode', '全部游戏声'), self.cfg.get('speaker_mode', '全部游戏声'))
            spk_eq = self.cfg.get('speaker_eq', '原味')
            spk = RawSpeaker() if spk_mode != '关闭' else None
            spk_mix = 0.0; spk_peak = 0.0; spk_assert = time.monotonic()
            fusion = self.cfg.get('fusion', '叠加'); rgain = self.cfg.get('rumble_gain', 1.0)
            fx = None
            if self.cfg.get('pbz_fx', True) or self.cfg.get('ww_fx', True) or self.cfg.get('ww_vision', True):   # vision drives triggers through fx
                fxcfg = load_event_fx(ROOT)
                if not self.cfg.get('pbz_fx', True):
                    fxcfg['rules'] = []                               # 影之刃零 module off
                if not self.cfg.get('ww_fx', True):
                    fxcfg['ww_rules'] = []                            # 鸣潮 event cues off (screen triggers stay)
                if self.cfg.get('pbz_log_dir'):                       # chosen in the UI
                    fxcfg['log_dir'] = self.cfg['pbz_log_dir']
                elif not pathlib.Path(fxcfg.get('log_dir', '')).parent.exists():   # default path not on this PC
                    try:
                        from sysfix import find_pbz_log_dir
                        found = find_pbz_log_dir()
                        if found: fxcfg['log_dir'] = str(found)
                    except Exception:
                        pass
                if fxcfg.get('enabled', True):
                    fx = EventFX(fxcfg)          # PBZ mod log is optional (tail waits for it); 鸣潮 uses the screen
            base_rgain = rgain; duck = 0.5; prof = None; fg = ''
            boost = BoostResistance() if native else None
            bike_mode = 'other'
            vision_key = None
            bike_window = None

            def bike_context():
                current = vision
                if self.stop_ev.is_set():
                    return 'unknown'
                if current is None:
                    return 'other'
                if bike_window != foreground_window():
                    return 'unknown'
                context, _ = current.bike_state()
                if context in ('boost', 'air') and (not self.cfg.get('ww_bike_swap', True) or button_map.uses_right_shoulder):
                    return 'bike'
                return context

            trace = None; trace_trig = None; trace_color = None
            if native and self.cfg.get('record_native'):
                (ROOT / 'traces').mkdir(exist_ok=True)
                tpath = ROOT / 'traces' / time.strftime('native-%Y%m%d-%H%M%S.tsv')
                trace = open(tpath, 'w', encoding='utf8', buffering=1 << 16)
                trace.write(f'{int(time.time() * 1000)}\tinfo\tstart\tPS5Haptics native trace\n')
                self.log(f'正在录制原生触感：{tpath.name}')
            msg = f'已启动：{self.stats["source"]} → {out.dev["name"]}'
            if native:
                msg += '\n原生透传中：游戏看到的是虚拟 PS5 手柄，原生扳机和音圈触感由本程序转到真手柄。游戏如果已经开着，请重启游戏。'
            elif xbox:
                msg += '\n虚拟 Xbox 手柄中：游戏看到的是 Xbox 手柄，游戏震动由本程序转到真手柄。游戏如果已经开着，请重启游戏。'
            elif takeover:
                msg += '\n接管中：游戏看到的是虚拟 PS4 手柄，真手柄只由本程序控制。游戏如果已经开着，请重启游戏。'
            self.log(msg); self.stats["late"] = 0          # drops during pad isolation / virtual pad setup are expected
            pend_l = np.zeros(0, np.float32); pend_r = np.zeros(0, np.float32)
            last_trig = (-1, -1); want_exe = self.cfg.get('foreground_exe', '').strip().lower()
            fg_check = 0.0; active = True; last_color = None
            clock0 = time.monotonic(); produced = 0       # haptic samples emitted vs wall clock
            alive_check = time.monotonic(); heard = time.monotonic(); last_blk = 0.0; rs = Resampler()
            diag_t = diag_prev = time.monotonic(); diag_loop = 0.0; live_seen = 0; spk_eff = spk_mode
            late_mark = 0                         # drops counted up to the last normal loop pass
            trig = (0.0, 0.0)                     # a block shorter than one hop (resampled) must not leave it unset
            tl_acc = dict.fromkeys(TL_KEYS, 0.0); tl_t = 0.0; static = (None, None); last_static = static
            mode_warned = set(); stuck = StuckGuard(self.log); guard_game = None
            speed_smooth = 1.0; last_spk_eff = spk_eff; spk_fade_in = False
            hid_reopens = getattr(hid, 'reopen_count', 0)
            last_spk_cfg = (spk_mode != '关闭', self.cfg.get('speaker_output') == '耳机孔')

            suppress_background = suppression_enabled(self.base)

            def ours(i):                          # this side's own effect: a trigger-feel preset, else follow the sound
                if static[i] is not None:
                    return static[i]
                return effect_vibration(amp[i], 27 + 3 * amp[i]) if amp[i] else effect_off()
            try:
                from proc_loopback import ProcSource
                self.proc = ProcSource(self.q, self.log, self.stop_ev, self._foreground_game)
                self.proc.request = self.capture
                self.proc.start()
            except Exception as exc:
                self.proc = None
                self.log('按进程截音不可用：' + str(exc))
            ep_source = self.stats['source']
            was_locked = self.capture_locked
            input_route = None
            rum_tail = 0
            trig_active = [False, False]; trig_hold_until = [0.0, 0.0]
            trig_pressed = [False, False]; native_phys = [effect_off(), effect_off()]
            _z_hop32 = np.zeros(HOP, dtype=np.float32); _z_hop32.setflags(write=False)
            while not self.stop_ev.is_set():
                capture_changed = was_locked != self.capture_locked
                default_changed = False
                if time.monotonic() - alive_check > 1.0:
                    alive_check = time.monotonic()
                    now_id, now_name = watcher.current_info() if watcher else (None, '')
                    if now_id and 'dualsense' in now_name.lower() and saved_default_id and now_id != saved_default_id:
                        if watcher.set_default(saved_default_id):
                            self.log(f'检测到虚拟 PS5 手柄接管了系统默认音频，已自动恢复到「{saved_default_name}」。')
                            now_id, now_name = saved_default_id, saved_default_name
                    if now_id and now_id != default_id:
                        default_id = now_id
                        if 'dualsense' not in now_name.lower():
                            saved_default_id, saved_default_name = now_id, now_name
                        default_changed = not self.capture_locked and not self.cfg.get('source_hint')
                    elif not self.capture_locked and not capture_changed:
                        stream = self._refresh_stream(stream, p, src)
                if capture_changed or default_changed or (not self.capture_locked and stream is None) or out.failure is not None:
                    was_locked = self.capture_locked
                    previous = src['name'].removesuffix(' [Loopback]')
                    close_stream(stream); stream = None
                    out.close(); out = None
                    pa_end(p); p = None
                    try:
                        p, src, stream, out = self._open_audio(output_name=real_out)
                    except (RuntimeError, PermissionError) as exc:
                        if not default_changed or capture_changed or self.capture_locked or self.stop_ev.is_set():
                            raise
                        self.log(str(exc) + ' 尝试恢复原来的声音设备。')
                        p, src, stream, out = self._open_audio(previous, real_out)
                    ch = src['maxInputChannels']; rate = int(src['defaultSampleRate'])
                    self.stats['source'] = ep_source = f"{src['name']}  {ch}声道 {rate}Hz"
                    while not self.q.empty():
                        try: self.q.get_nowait()
                        except queue.Empty: break
                    pend_l = np.zeros(0, np.float32); pend_r = np.zeros(0, np.float32)
                    clock0 = time.monotonic(); produced = 0; rs = Resampler()
                    speed_smooth = 1.0; spk_fade_in = True
                    spk_assert = 0.0
                    self.log('音频设备已重新连接：' + self.stats['source'])
                    continue
                if hid.read_error is not None:
                    raise RuntimeError('PS5 手柄输入中断：' + str(hid.read_error))
                # WASAPI loopback may stop delivering packets altogether during silence.
                # Keep processing until the last audio-driven trigger has actually been released.
                needs_idle_audio = (self.capture_locked or takeover or spk is not None or vision is not None or max(last_trig) > 0
                                    or any(self.cfg.get(k) for k in CONTROL_HAPTICS.values())
                                    or (fx is not None and (fx.voices or any(fx.triggers()))))
                use_ring = isinstance(getattr(out, '_use_ring', False), bool) and out._use_ring
                try:
                    blk = self.q.get(timeout=0.01 if needs_idle_audio or spk is not None else 0.2)
                    tnow = time.monotonic()
                    if tnow - diag_t > 30:              # audio health line in the log every 30 s
                        pr = self.proc
                        self.log(f'音频诊断：主循环最长停顿 {diag_loop * 1000:.0f} ms，游戏声音包最长间隔 '
                                 f'{(pr.max_gap * 1000 if pr else 0):.0f} ms，重连 {pr.reattach if pr else 0} 次，'
                                 f'积压清空 {self.stats.get("resync", 0)} 次，丢块 {self.stats["late"]}'
                                 + (f'，游戏原生扬声器峰值 {self.stats.get("game_spk", 0)}（断续 {pad_tap.underruns if pad_tap else 0} 次）（{vpad.spk_frames} 帧有声）' if native and vpad is not None else '')
                                 + (f'，转到真手柄峰值 {g_out_pk:.2f}（当前增益 {g_gain:.1f}x）' if pad_tap is not None else '')
                                 + (f'，缓冲 {out.buffered_s() * 1000:.0f} ms，输出溢出 {getattr(out, "overruns", 0)} 次' if use_ring else '')
                                 + f'，输出欠载 {out.xruns} 次，扬声器限幅 {out.speaker_safety.limited_samples} 采样，异常采样 {out.speaker_safety.invalid_samples}')
                        g_out_pk = 0.0
                        self.stats['game_spk'] = 0
                        diag_t = tnow; diag_loop = 0.0
                        if pr: pr.max_gap = 0.0
                    gap = tnow - diag_prev
                    if gap > 1.0:
                        # the whole loop was held up (game starting / loading, audio devices being set up):
                        # the blocks that piled up meanwhile were skipped on purpose - not a playback problem
                        lost = self.stats['late'] - late_mark
                        self.stats['late'] = late_mark
                        self.stats['stall_drops'] = self.stats.get('stall_drops', 0) + max(0, lost)
                        self.log(f'处理暂停了 {gap:.1f} 秒（游戏启动 / 加载或音频设备切换时常见），这段积压的声音已跳过')
                    late_mark = self.stats['late']
                    diag_loop = max(diag_loop, gap); diag_prev = tnow
                except queue.Empty:
                    blk = None
                audio_exe = ''
                proc_key = self.proc.key if self.proc else None
                route = self.capture, None if self._allow_system_mix() else proc_key
                if route != input_route:
                    input_route = route
                    pend_l = np.zeros(0, np.float32); pend_r = np.zeros(0, np.float32)
                    rs = Resampler()
                    speed_smooth = 1.0; spk_fade_in = True
                if blk is not None:
                    from_endpoint = not isinstance(blk, tuple)
                    if isinstance(blk, tuple):                      # per-process capture: stereo 48 kHz
                        tag, audio_exe, blk = blk; bch, brate = 2, RATE
                        if not (tag and tag == proc_key and tag[0] == self.capture):
                            blk = None
                    else:
                        bch, brate = ch, rate
                        if not self._allow_system_mix():
                            blk = None
                if blk is not None:
                    source_endpoint = from_endpoint
                    if np.any(blk):
                        heard = time.monotonic()
                    l, r = downmix(blk, bch, self.cfg['skip_center'])
                    in_lag = self.q.qsize() * len(l) / brate + len(pend_l) / RATE      # seconds waiting
                    out_buf = out.buffered_s() if use_ring else 0.0
                    total_buf = in_lag + out_buf
                    self.stats['lag_ms'] = int(total_buf * 1000)
                    if in_lag > 0.3:                                   # far behind: drop the backlog once
                        while not self.q.empty():
                            try: self.q.get_nowait()
                            except queue.Empty: break
                        pend_l = pend_l[-HOP:]; pend_r = pend_r[-HOP:]
                        speed_smooth = 1.0; rs = Resampler(); spk_fade_in = True
                        self.stats['resync'] = self.stats.get('resync', 0) + 1
                    speed_smooth = drift_speed(total_buf, speed_smooth, has_ring=use_ring)
                    l, r = rs.process(l, r, brate / RATE * speed_smooth)
                    if last_blk and time.monotonic() - last_blk > 0.12:
                        spk_fade_in = True
                    last_blk = time.monotonic()
                    pend_l = np.concatenate([pend_l, l]); pend_r = np.concatenate([pend_r, r])
                elif (needs_idle_audio and len(pend_l) < HOP
                      and time.monotonic() - last_blk > 0.12
                      and (time.monotonic() - clock0) * RATE - produced > 4 * HOP):
                    # only when the source has really stopped (0.12 s): padding inside a stream of 10-80 ms
                    # packets inserted gaps = the speaker crackled and stuttered
                    # game is silent (loopback delivers nothing): pad with real-time silence so the game's
                    # own haptics keep flowing; only when genuinely behind the clock, never ahead of it
                    if len(pend_l) > 0:
                        k_end = min(len(pend_l), 48)
                        ramp = np.linspace(1.0, 0.0, k_end, dtype=np.float32)
                        pend_l[-k_end:] *= ramp; pend_r[-k_end:] *= ramp
                    z = np.zeros(2 * HOP, np.float32)
                    pend_l = np.concatenate([pend_l, z]); pend_r = np.concatenate([pend_r, z])
                else:
                    continue
                now = time.monotonic()
                if (now - clock0) * RATE - produced > RATE:    # >1 s behind (stall): resync the clock
                    clock0 = now; produced = 0
                if now - fg_check > 0.5:
                    fg_check = now; hwnd = foreground_window(); new_fg = foreground_exe(hwnd)
                    if new_fg != fg:
                        fg_since = now
                        stuck.reset_wave()
                        game_speaker_active = False
                        pad_session_check = 0.0
                        native_phys = [effect_off(), effect_off()]
                    fg = new_fg
                    g_now = self.profile_for(fg)
                    g_name = g_now.get('name') if g_now else None
                    if g_name != self.profile:                  # the person's own settings for the game in front
                        self.profile = g_name; self.cfg = self.effective(fg); live_seen = -1
                        self.log(f'使用「{g_name}」的专属设置' if g_name else '前台不是有专属设置的游戏，使用通用设置')
                        if g_now and self.cfg.get('mode') != mode and g_name not in mode_warned:
                            mode_warned.add(g_name)
                            self.log(f'「{g_name}」的连接方式和当前不同：先打开游戏，再停止并重新启动本软件后生效。')
                    elif self.live_ver != live_seen:
                        self.cfg = self.effective(fg)
                    if self.cfg.get('volume_comp'):             # lowering the system volume should not weaken the haptics
                        if volume is None:
                            try:
                                from audio_volume import VolumeMonitor
                                volume = VolumeMonitor()
                            except Exception as exc:
                                self.cfg['volume_comp'] = False; self.log('无法读取系统音量（不影响使用）：' + str(exc))
                        ep_gain = volume.gain() if volume is not None else 1.0
                    else:
                        ep_gain = 1.0
                    if self.live_ver != live_seen:              # settings changed in the UI: apply what can change live
                        live_seen = self.live_ver
                        suppress_background = suppression_enabled(self.base)
                        vol = self.cfg.get('speaker_volume', 0.6)
                        spk_mode = SPEAKER_MODE_ALIAS.get(self.cfg.get('speaker_mode', '全部游戏声'), self.cfg.get('speaker_mode', '全部游戏声'))
                        spk_eq = self.cfg.get('speaker_eq', '原味'); spk_assert = 0.0   # output path applies at once
                        if self.pad_filter is not None:
                            self.pad_filter.configure(self.cfg.get('stick_dz_l', 0.0), self.cfg.get('stick_dz_r', 0.0), self.cfg.get('gyro_bias') or (0, 0, 0))
                        if spk is None and spk_mode != '关闭':
                            spk = RawSpeaker()
                        elif spk_mode == '关闭':
                            spk = None
                        fusion = self.cfg.get('fusion', '叠加'); base_rgain = self.cfg.get('rumble_gain', 1.0)
                        tune = prof is not None and self.cfg.get('game_profiles', True)
                        rgain = base_rgain * (prof['rumble_scale'] if tune else 1.0)
                        eng = HapticEngine(prof['grip_profile'] if tune and self.cfg['grip_profile'] != '细腻' else self.cfg['grip_profile'], self.cfg['trigger_profile'],
                                           self.cfg['grip_gain'], self.cfg['trigger_gain'], self.cfg.get('haptic_style', '游戏'), groove_gains(self.cfg))
                        request = self._capture_request()
                        if request != self.capture:
                            self.capture = request
                            if self.proc is not None:
                                self.proc.request = request
                            pend_l = np.zeros(0, np.float32); pend_r = np.zeros(0, np.float32)
                            rs = Resampler()
                            continue
                    # 全部游戏声: the whole mix, even over a game's own pad-speaker sound.
                    # 特殊事件: a game that drives the pad speaker itself keeps it; 影之刃零 keeps its event tuning;
                    # games that don't use the pad speaker play the whole mix.
                    # grips on 纯用游戏 + 特殊事件 = nothing of ours at all: no event tuning, no whole mix as a fallback
                    if (spk_mode == '特殊事件' and not takeover and fg not in NOT_GAMES
                            and now - pad_session_check >= 1.0 and not pad_probe_busy):
                        pad_probe_busy = True
                        pad_session_check = now
                        threading.Thread(target=_probe_pad_speaker, args=(fg,), daemon=True).start()
                    if spk_mode != '特殊事件':
                        game_speaker_active = False
                    if self.proc is not None and self.proc.key and self.proc.key[0] == self.capture:
                        self.stats['source'] = f'{self.proc.name}（按进程截音）  2声道 48000Hz'
                    elif self.capture_locked:
                        self.stats['source'] = '等待锁定进程' if self.capture[0] == 'pinned' else '等待游戏进程'
                    else:
                        self.stats['source'] = ep_source
                    # a game in front but pure digital silence on the loopback for 15 s: its audio bypasses
                    # the shared mix (Windows spatial sound / Dolby Atmos, or exclusive mode)
                    silent = not self.capture_locked and fg in GAME_PROFILES and now - heard > 15
                    if silent and not self.stats.get('silent'):
                        self.log('截不到游戏声音：' + src['name'].replace(' [Loopback]', '') + ' 上 15 秒全是静音数据。'
                                 '请在 Windows 声音设置里关闭这个输出设备的「空间音效」(Dolby Atmos / Windows Sonic)，'
                                 '并在设备属性→高级里取消「允许应用程序独占控制此设备」，然后重启游戏。')
                    self.stats['silent'] = silent
                    if want_exe:
                        active = fg == want_exe
                    newprof = GAME_PROFILES.get(fg) if self.cfg.get('ww_enabled', True) else None   # 鸣潮 card on/off
                    if newprof is not prof:                    # entering / leaving a tuned game
                        prof = newprof
                        tune = prof is not None and self.cfg.get('game_profiles', True)              # 专属调校
                        rgain = base_rgain * (prof['rumble_scale'] if tune else 1.0)
                        duck = prof['duck'] if tune else 0.5
                        gprof = prof['grip_profile'] if tune and self.cfg['grip_profile'] != '细腻' else self.cfg['grip_profile']   # a game's tuning doesn't override an explicit 细腻
                        eng = HapticEngine(gprof, self.cfg['trigger_profile'], self.cfg['grip_gain'], self.cfg['trigger_gain'],
                                           self.cfg.get('haptic_style', '游戏'), groove_gains(self.cfg))
                        self.stats['profile'] = prof['name'] if prof else ''
                    skills = bool(self.cfg.get('ww_vision', True) and fx is not None)
                    observe_ww = skills or (native and self.cfg.get('scene', '游戏') == '游戏')
                    skill_icons = self.cfg.get('ww_skill_icons', False)
                    new_vision_key = (hwnd, skills, skill_icons) if prof and prof.get('vision') and observe_ww else None
                    if new_vision_key != vision_key:
                        vision_key = new_vision_key
                        if native:
                            vpad.gate['L'] = vpad.gate['R'] = None
                        if fx is not None:
                            fx.set_state_trigger('L', None); fx.set_state_trigger('R', None)
                        previous, vision = vision, None
                        if previous is not None:
                            previous.stop()
                        if new_vision_key is not None:
                            tp = None
                            shadow = skill_icons and self.cfg.get('ww_skill_shadow', False)
                            if self.cfg.get('record_native', True) or shadow:
                                (ROOT / 'traces').mkdir(exist_ok=True)
                                tp = ROOT / 'traces' / time.strftime('ww-%Y%m%d-%H%M%S.tsv')
                            cd = (ROOT / 'traces' / 'ww-crops') if self.cfg.get('ww_collect', False) else None
                            sd = (ROOT / 'traces' / 'ww-shots') if self.cfg.get('ww_shots', True) else None
                            trigger_input = (lambda: vpad.trigger_input) if native else (lambda: (hid.l2 / 255, hid.r2 / 255))
                            hat_input = lambda: hid.dpad
                            held_input = lambda: hid.held
                            if takeover and button_map.bindings:
                                hat_input = lambda: logical_report[8] & 15
                                held_input = lambda: (logical_report[8] >> 4) | ((logical_report[9] & 3) << 4)
                                if not native:
                                    trigger_input = lambda: (logical_report[5] / 255, logical_report[6] / 255)
                            vision = WWVision(trigger_input, dpad=hat_input,
                                              buttons=held_input, trace_path=tp, mem=proc_mem_mb,
                                              crops_dir=cd, shots_dir=sd, fps=10, bike=True, skills=skills,
                                              skill_shadow=shadow, skill_icons=skill_icons,
                                              trigger_presses=(lambda: vpad.trigger_press_times) if native else None)
                            bike_window = hwnd
                            if native:
                                vpad.bike_context = bike_context
                            vision.start()
                press = hid.poll_input(observe_ww_input if vision is not None else None)
                self.stats['battery'] = hid.battery
                new_bike_mode = bike_context() if vision is not None else 'other'
                if new_bike_mode != bike_mode:
                    if boost is not None:
                        boost.reset()
                    if new_bike_mode != 'other' and fx is not None:
                        fx.trig.update(L=(None, 0), R=(None, 0))
                    if new_bike_mode in ('air', 'boost') and bike_mode not in ('air', 'boost'):
                        self.log('科考摩托：R2 氮气加速，R1 对应原 R2')
                    elif bike_mode in ('air', 'boost') and new_bike_mode not in ('air', 'boost'):
                        self.log('科考摩托 R1/R2 已恢复')
                    bike_mode = new_bike_mode
                self.stats['ww_bike'] = bike_mode
                new_guard_game = fg if fg == PBZ_EXE or (fg == WW_EXE and bike_mode == 'other') else None
                if new_guard_game != guard_game:
                    stuck.reset()
                    guard_game = new_guard_game

                if native and bike_mode in ('boost', 'air'):
                    if not self.cfg.get('ww_bike_audio', True):
                        boost.reset()
                    elif blk is not None:
                        if audio_exe == WW_EXE and in_lag <= 0.3:
                            boost.feed(l, r, now)
                        else:
                            boost.reset()

                control_triggers = None
                if vision is not None and fx is not None:
                    for ev in vision.poll():
                        if bike_mode == 'other' and not vision.after_bike:
                            name = fx.feed(*ev)
                            if name: self.stats['fx_last'] = name
                    st = vision.states() if bike_mode == 'other' else dict.fromkeys(vision.final)
                    trig_off_mode = self.cfg.get('trigger_mode', '自动') == '关闭'
                    for side, (skill, eff, gate) in STATE_TRIGGERS.items():
                        side_disabled = trig_off_mode or self.cfg.get(trigger_presets.preset_key(side)) == trigger_presets.OFF
                        on = bool(st.get(skill)) and not side_disabled
                        fx.set_state_trigger(side, eff if on else None)
                        if native and vpad is not None:            # game sees the press only past the click
                            vpad.gate[side] = self.cfg.get('ww_gate_' + side, gate) if on and self.cfg.get('ww_click_gate', True) and self.cfg.get('trigger_mode') != '纯用游戏' else None
                    if vision.skills and not vision.skill_icons and bike_mode in ('other', 'unknown'):
                        control_triggers = tuple(
                            STATE_TRIGGERS[side][1]
                            if st.get(STATE_TRIGGERS[side][0]) and not trig_off_mode and self.cfg.get(trigger_presets.preset_key(side)) != trigger_presets.OFF
                            else effect_off()
                            for side in ('L', 'R')
                        )
                    self.stats['ww'] = (st, vision.frames, str(vision.error) if vision.error else '')
                if fx is not None:
                    for name in fx.poll():
                        self.stats['fx_last'] = name
                pure = FUSION_ALIAS.get(fusion, fusion) == '纯用游戏' and spk_mode == '特殊事件'
                adapted = fg == PBZ_EXE and self.cfg.get('pbz_fx', True) and not pure
                native_speaking = pad_tap is not None and (pad_tap.sounding(2.0) or (adapted and bool(pad_tap.last_sound)))
                spk_eff = speaker_route(spk_mode, pure, adapted, native_speaking,
                                        not takeover and fg not in NOT_GAMES and game_speaker_active)
                if spk_eff != last_spk_eff:
                    last_spk_eff = spk_eff
                    spk_fade_in = True
                self.stats['spk_eff'] = spk_eff
                large = small = 0.0; gpk_l = gpk_r = 0.0; spk_peak = 0.0
                spk_cfg_now = (spk is not None, self.cfg.get('speaker_output') == '耳机孔')
                cur_hid_reopens = getattr(hid, 'reopen_count', 0)
                need_spk_assert = False
                if spk is not None:
                    if spk_cfg_now != last_spk_cfg or spk_assert == 0.0 or cur_hid_reopens != hid_reopens:
                        need_spk_assert = True
                    elif not takeover and time.monotonic() - spk_assert > 30.0:
                        need_spk_assert = True
                last_spk_cfg = spk_cfg_now
                hid_reopens = cur_hid_reopens
                if need_spk_assert:   # route on change, HID reconnect, or periodically in 普通 mode only
                    spk_assert = time.monotonic()
                    try: hid.enable_speaker(volume=0x64, headset=spk_cfg_now[1])
                    except Exception: pass
                while len(pend_l) >= HOP and out.failure is None:
                    bl, br = pend_l[:HOP], pend_r[:HOP]; pend_l, pend_r = pend_l[HOP:], pend_r[HOP:]
                    produced += HOP
                    if not active:
                        bl = _z_hop32; br = _z_hop32
                    if ep_gain != 1.0 and source_endpoint:           # haptics only: the speaker keeps following the system volume
                        bl = np.clip(bl * ep_gain, -1, 1); br = np.clip(br * ep_gain, -1, 1)
                    wl, wr, trig = eng.process(bl, br, press)
                    vg = 1.0
                    if suppress_background:
                        if voice_gate is None:
                            try:
                                from voice_gate import VoiceGate
                                voice_gate = VoiceGate()
                            except Exception as exc:
                                voice_gate = False
                                self.log('人声识别不可用：' + str(exc))
                        if voice_gate:
                            vg = voice_gate.gain(bl, br)
                        if music_gate is None:
                            from voice_gate import GameSoundGate
                            music_gate = GameSoundGate()
                        recent_action = 0 <= now - hid.buttons.last_action < 0.22
                        vg = min(vg, music_gate.gain(bl, br, recent_action))
                    else:
                        music_gate = voice_gate = None
                    self.stats['voice_closed'] = vg < 0.15
                    if vg < 0.999:
                        wl = wl * vg; wr = wr * vg; trig = (trig[0] * vg, trig[1] * vg)
                    tl_snd_l, tl_snd_r = wl, wr
                    if native:
                        (rleft, rright), _, color = vpad.feedback()
                        if guard_game is not None:
                            timeout = PBZ_STUCK_MOTOR_S if guard_game == PBZ_EXE else STUCK_MOTOR_S
                            rleft, rright = stuck.motors(rleft, rright, now, timeout)
                        large, small = rleft / 255, rright / 255
                        nl, nr = vpad.wave.pull(HOP)                  # game's own voice-coil waveform
                        if guard_game is not None and stuck.wave(
                                nl, nr, fx.last_event if fx is not None else 0.0, time.monotonic()):
                            nl = _z_hop32; nr = _z_hop32
                        if large or small or rum_tail > 0:
                            rum_tail = 12 if (large or small) else rum_tail - 1
                            rl, rr = rum.step(large, small, rgain)         # legacy rumble, if the game uses it
                        else:
                            rl = rr = _z_hop32
                        nl = nl * rgain + rl; nr = nr * rgain + rr
                        if fx is not None and FUSION_ALIAS.get(fusion, fusion) != '纯用游戏':   # event tuning: grade / duck the native layer
                            g = fx.native_gain(HOP); nl = nl * g; nr = nr * g
                        nlevel = float(max(np.max(np.abs(nl)), np.max(np.abs(nr))))
                        tl_acc['nat'] = max(tl_acc['nat'], nlevel); tl_acc['mot'] = max(tl_acc['mot'], large, small)
                        gpk = vpad.spk_peak; vpad.spk_peak = 0              # game's own pad-speaker channels
                        if gpk:
                            self.stats['game_spk'] = max(self.stats.get('game_spk', 0), gpk)
                            if trace is not None:
                                trace.write(f'{int(time.time() * 1000)}\tpadspk\t{gpk}\t{self.stats.get("fx_last", "")}\n')
                            if gpk > 300 and not self.stats.get('game_spk_seen'):
                                self.stats['game_spk_seen'] = True
                                self.log(f'检测到游戏在往手柄扬声器发声音（游戏原生的扬声器音效，峰值 {gpk}）')
                        if trace is not None:
                            ms = int(time.time() * 1000)
                            if nlevel > 0.005 or large > 0 or small > 0:
                                trace.write(f'{ms}\thaptic\t{float(np.max(np.abs(nl))):.3f}\t{float(np.max(np.abs(nr))):.3f}\t{large:.2f}\t{small:.2f}\n')
                            if color != trace_color:
                                trace_color = color; trace.write(f'{ms}\tlight\t{color}\n')
                        wl, wr = fuse(wl, wr, nl, nr, nlevel, fusion, duck)
                        large = max(large, nlevel)
                        if color and color != last_color and any(color):
                            try: hid.set_lightbar(*color); last_color = color
                            except Exception: pass
                    elif takeover:
                        large, small, color = vpad.rumble()
                        if guard_game == PBZ_EXE:
                            large, small = stuck.motors(large, small, now, PBZ_STUCK_MOTOR_S)
                        tl_acc['mot'] = max(tl_acc['mot'], large, small)
                        if large or small or rum_tail > 0:
                            rum_tail = 12 if (large or small) else rum_tail - 1
                            rl, rr = rum.step(large, small, rgain)
                        else:
                            rl = rr = _z_hop32
                        wl, wr = fuse(wl, wr, rl, rr, max(large, small), fusion, duck)
                        if color and color != last_color and any(color):
                            try: hid.set_lightbar(*color); last_color = color
                            except Exception: pass
                    if fx is not None:                               # event tuning: synthesized grip effects
                        fl, fr = fx.render(HOP)
                        tl_acc['fx'] = max(tl_acc['fx'], float(np.max(np.abs(fl))), float(np.max(np.abs(fr))))
                        if FUSION_ALIAS.get(fusion, fusion) != '纯用游戏':
                            wl = np.clip(wl + fl, -1, 1).astype(np.float32); wr = np.clip(wr + fr, -1, 1).astype(np.float32)
                    for group, side, kind, pressed in hid.buttons.drain():    # always drained: enabling never replays old presses
                        if active and self.cfg.get(CONTROL_HAPTICS[group]):
                            clicks.add(side, kind, pressed)
                    if clicks.voices:
                        fl, fr = clicks.render(HOP, self.cfg.get('button_gain', 1.0))
                        wl = np.clip(wl + fl, -1, 1).astype(np.float32); wr = np.clip(wr + fr, -1, 1).astype(np.float32)
                    sp = se = None
                    gsp = pad_tap.pull(HOP) if pad_tap is not None else None     # game's own pad-speaker sound
                    if gsp is not None:
                        if spk_clip is not None:
                            spk_clip.feed(gsp)                         # raw, before anything below
                        # the tiny speaker cannot play below ~150 Hz: with the make-up gain it only rattles (no other EQ)
                        gsp = gsp_flt.step(gsp).astype(np.float32)
                        # the game mixes its pad-speaker cues quietly (on PS5 the console volume lifts them):
                        # slow make-up gain to ~70 % peak, at most 10x; the speaker-volume slider applies on top
                        gpk_now = float(np.max(np.abs(gsp)))
                        g_env = max(gpk_now, g_env * 0.9995, 0.02)
                        g_target = min(10.0, 0.7 / g_env)
                        g_next = g_gain + (g_target - g_gain) * 0.1
                        if gpk_now > 0.0:
                            gsp = gsp * np.linspace(g_gain, g_next, len(gsp))
                        g_gain = g_next
                    if spk is not None:
                        need_spk = spk_eff in ('全部游戏声', '特殊事件') or spk_mix > 0.0
                        if need_spk:
                            spk.analyse(bl, br)
                        if spk_eff == '全部游戏声':
                            sp = spk.render(spk_eq, vol)
                        elif spk_eff == '游戏原生':
                            sp = (gsp * vol).astype(np.float32) if gsp is not None else None
                        if gsp is not None:
                            g_out_pk = max(g_out_pk, float(np.max(np.abs(gsp))) * vol)
                        ev_on = fx is not None and fx.speaker_on() and spk_eff == '特殊事件'
                        if ev_on or spk_mix:                       # 特殊事件: the moment's own sound, its EQ, louder
                            se = spk.render(fx.speaker_eq if (fx is not None and fx.speaker_eq) else None, vol, 1.0)
                    if se is not None:                             # key event (parry, perfect dodge, ultimate...):
                        spk_want = 1.0 if ev_on else 0.0           # crossfade in / out
                        if spk_want or spk_mix:
                            old = spk_mix                          # ~3 hops fade: no clicks at the edges
                            spk_mix = min(spk_want, old + 0.34) if spk_want > old else max(spk_want, old - 0.34)
                            ramp = np.linspace(old, spk_mix, HOP, dtype=np.float32)
                            base = sp if sp is not None else np.zeros(HOP, np.float32)
                            sp = (base * (1 - ramp) + se * ramp).astype(np.float32)
                    if sp is not None:
                        if spk_fade_in:
                            k_fade = min(144, len(sp))
                            if k_fade:
                                sp = sp.copy()
                                sp[:k_fade] *= np.linspace(0.0, 1.0, k_fade, dtype=np.float32)
                            spk_fade_in = False
                        spk_peak = max(spk_peak, float(np.max(np.abs(sp))))
                    if self.cfg.get('swap_grips'):
                        wl, wr = wr, wl
                    gl_gain, gr_gain = self.cfg.get('grip_left_gain', 1.0), self.cfg.get('grip_right_gain', 1.0)
                    if gl_gain != 1.0 or gr_gain != 1.0:              # per-hand strength: everything that reaches that grip
                        wl = np.clip(wl * gl_gain, -1, 1).astype(np.float32); wr = np.clip(wr * gr_gain, -1, 1).astype(np.float32)
                    out.feed(wl, wr, sp, headset=self.cfg.get('speaker_output') == '耳机孔')
                    try:
                        tl_acc['snd'] = max(tl_acc['snd'], float(np.max(np.abs(tl_snd_l))), float(np.max(np.abs(tl_snd_r))))
                    except NameError:
                        pass
                    tl_acc['out_l'] = max(tl_acc['out_l'], float(np.max(np.abs(wl)))); tl_acc['out_r'] = max(tl_acc['out_r'], float(np.max(np.abs(wr))))
                    gpk_l = max(gpk_l, float(np.max(np.abs(wl)))); gpk_r = max(gpk_r, float(np.max(np.abs(wr))))
                if len(pend_l) > 4 * HOP:          # never build up latency
                    pend_l, pend_r = pend_l[-HOP:], pend_r[-HOP:]
                for i in (0, 1):
                    trig_pressed[i] = press[i] >= (0.02 if trig_pressed[i] else 0.05)
                press_gate = bool(self.cfg.get('trigger_press_gate', True))
                static = trigger_presets.effects(self.cfg)
                side_off = (self.cfg.get(trigger_presets.preset_key('L')) == trigger_presets.OFF,
                            self.cfg.get(trigger_presets.preset_key('R')) == trigger_presets.OFF)
                raw_amp = (0 if static[0] is not None else int(round(min(1.0, trig[0] * self.cfg.get('trigger_left_gain', 1.0)) * 8)),
                           0 if static[1] is not None else int(round(min(1.0, trig[1] * self.cfg.get('trigger_right_gain', 1.0)) * 8)))
                gate_open = ((trig_pressed[0] if press_gate else True) and static[0] is None,
                             (trig_pressed[1] if press_gate else True) and static[1] is None)
                amp, trig_active, trig_hold_until = smooth_trigger_amp(raw_amp, trig_active, trig_hold_until, now, pressed=gate_open)

                def filter_ov(ov_l, ov_r):
                    out_ov = [ov_l, ov_r]
                    tg = self.cfg.get('trigger_gain', 1.0)
                    for idx, gk in ((0, 'trigger_left_gain'), (1, 'trigger_right_gain')):
                        e = out_ov[idx]
                        if e is None:
                            continue
                        if side_off[idx]:
                            out_ov[idx] = None
                        elif e[0] == 0x26 and (tg <= 0 or self.cfg.get(gk, 1.0) <= 0 or (press_gate and not trig_pressed[idx])):
                            out_ov[idx] = None
                    return out_ov[0], out_ov[1]

                trig_mode = self.cfg.get('trigger_mode', '自动')
                game_l = game_r = None
                if native:
                    game_l, game_r, _ = vpad.triggers()
                    if guard_game is not None:
                        tnow = time.monotonic()
                        game_l = stuck.trigger('L', game_l, tnow); game_r = stuck.trigger('R', game_r, tnow)
                    if trace is not None and (game_l, game_r) != trace_trig:
                        trace_trig = (game_l, game_r)
                        trace.write(f"{int(time.time() * 1000)}\ttrigger\t{game_l.hex() if game_l else '-'}\t{game_r.hex() if game_r else '-'}\n")
                    off = lambda e: e is None or e[0] in (0x00, 0x05)
                    game_trig = not (off(game_l) and off(game_r))
                    # a fresh command while this program has been in front for a while: a background game's
                    # latched effect must not be blamed on the browser in front
                    same_proc = not (self.proc and self.proc.name and self.proc.name.casefold() != fg.casefold())
                    if (game_trig and (game_l, game_r) != last_game_trig and time.monotonic() - fg_since > 3.0
                            and same_proc and self.caps.note(fg, 'trigger') and trig_mode == '自动'):
                        self.log(f'检测到 {fg} 自己在用自适应扳机，之后这款游戏不再叠加扳机震动')
                    last_game_trig = (game_l, game_r)
                    game_l, game_r = button_map.trigger_effects(game_l, game_r)
                if trig_mode == '关闭':
                    if native and vpad is not None:
                        vpad.gate['L'] = vpad.gate['R'] = None
                    if not trig_released:
                        hid.set_triggers(effect_off(), effect_off(), force=True)
                        trig_released = True; last_trig = (-1, -1); last_static = (None, None)
                        native_phys = [effect_off(), effect_off()]
                    trig_active = [False, False]; trig_hold_until = [0.0, 0.0]; amp = None
                elif native:
                    trig_released = False
                    stab_l = effect_off() if (side_off[0] and trig_mode != '纯用游戏') else stabilize_native_trigger(game_l, native_phys[0], trig_pressed[0], press_gate)
                    stab_r = effect_off() if (side_off[1] and trig_mode != '纯用游戏') else stabilize_native_trigger(game_r, native_phys[1], trig_pressed[1], press_gate)
                    native_phys = [stab_l, stab_r]
                    if trig_mode == '纯用游戏':                       # ours is off: exactly what the game sends
                        hid.set_triggers(stab_l, stab_r)
                        last_trig = (-1, -1); amp = None
                    elif control_triggers is not None:
                        hid.set_triggers(*button_map.trigger_effects(*control_triggers, off=effect_off()))
                        last_trig = (-1, -1); amp = None
                    elif bike_mode != 'other':
                        left = stab_l
                        right = stab_r
                        if bike_mode in ('air', 'boost'):
                            # The game receives R1 from physical R2. Its R2 effect
                            # belongs to the pitch action now on the physical shoulder.
                            right_gain = self.cfg.get('trigger_gain', 1.0) * self.cfg.get('trigger_right_gain', 1.0)
                            right = effect_off() if side_off[1] else boost.effect(vpad.bike_input.boost_pressed, now, right_gain)
                            if bike_mode == 'air' and 'L2' not in button_map.bindings and off(game_l):
                                gain = min(1.0, max(0.0, self.cfg.get('trigger_gain', 1.0) * self.cfg.get('trigger_left_gain', 1.0)))
                                left = effect_off() if side_off[0] else (effect_zones(round(force * gain) for force in DIVE_EFFECT) if hid.l2 > 8 else effect_off())
                        hid.set_triggers(left, right)
                        last_trig = (-1, -1); amp = None
                    elif trig_mode == '自动' and (game_trig or self.caps.has(fg, 'trigger')):
                        # the game drives the triggers: pass its effects through untouched
                        ov_l, ov_r = filter_ov(*button_map.trigger_effects(*fx.triggers())) if fx is not None else (None, None)
                        hid.set_triggers(effect_off() if side_off[0] else (ov_l or stab_l),
                                         effect_off() if side_off[1] else (ov_r or stab_r))
                        last_trig = (-1, -1); amp = None
                    elif trig_mode == '叠加' and game_trig:
                        # a trigger runs one effect at a time: a side the game is using stays the game's, the other is ours
                        ov_l, ov_r = filter_ov(*button_map.trigger_effects(*fx.triggers())) if fx is not None else (None, None)
                        lf = effect_off() if side_off[0] else (ov_l or (stab_l if not off(game_l) else ours(0)))
                        rf = effect_off() if side_off[1] else (ov_r or (stab_r if not off(game_r) else ours(1)))
                        hid.set_triggers(lf, rf); last_trig = (-1, -1); amp = None
                elif trig_mode == '纯用游戏':
                    # no game commands reach us here; in overlay mode the game writes to the pad itself, so stay off it
                    if not trig_released:
                        hid.set_triggers(effect_off(), effect_off(), force=True); trig_released = True; last_trig = (-1, -1); last_static = (None, None)
                    amp = None
                elif control_triggers is not None:
                    hid.set_triggers(*button_map.trigger_effects(*control_triggers, off=effect_off()))
                    trig_released = False
                    last_trig = (-1, -1); amp = None
                else:
                    trig_released = False
                if amp is not None and fx is not None:
                    ov_l, ov_r = filter_ov(*button_map.trigger_effects(*fx.triggers()))
                    if ov_l or ov_r:                                 # event trigger effect wins for its duration
                        lf = ov_l or ours(0)
                        rf = ov_r or ours(1)
                        hid.set_triggers(lf, rf); last_trig = (-1, -1); amp = None
                if amp is not None and (amp != last_trig or static != last_static):
                    if hid.set_triggers(ours(0), ours(1), min_interval=0.05):
                        last_trig = amp; last_static = static
                now_tl = time.monotonic()
                if now_tl - tl_t >= 0.1:                          # self-check timeline: where the vibration came from
                    tl_t = now_tl; le, re_ = getattr(hid, 'last', (None, None))
                    self.timeline.append((round(time.time(), 1), *(round(tl_acc[k], 2) for k in TL_KEYS), self.stats.get('fx_last', ''),
                                          trigger_presets.effect_name(le), trigger_presets.effect_name(re_)))
                    tl_acc = dict.fromkeys(TL_KEYS, 0.0)
                self.stats.update(level=eng.level, trigger=trig, grip=(gpk_l, gpk_r), speaker=spk_peak, xruns=out.xruns, press=(hid.l2 / 255, hid.r2 / 255), active=active, rumble=(large, small),
                                  vpad=((vpad.frames['in'], vpad.frames[1] + vpad.frames[3]) if native else
                                        (vpad.updates, vpad.feedbacks)) if vpad else None,
                                  native_trig=(game_l is not None and game_l[0] not in (0, 5), game_r is not None and game_r[0] not in (0, 5)))
        except Exception as exc:
            error = str(exc); self.log('出错：' + error); (ROOT / 'last-error.txt').write_text(traceback.format_exc(), 'utf8')
        finally:
            self.stop_ev.set()  # the capture helpers also need to stop when a device fails
            close_stream(stream)
            try:
                if trace is not None: trace.close()
            except Exception: pass
            if vision is not None:
                vision.stop()
            for o in (watcher, spk_clip, volume, hid, vpad):
                try:
                    if o: o.close()
                except Exception: pass
            if iso:
                try: iso.close()
                except Exception as exc: self.log('恢复手柄可见性失败，请运行「一键恢复手柄可见性」：' + str(exc))
            try:
                if out: out.close()
            except Exception: pass
            if p: pa_end(p)
            tail = '已释放扳机和握把。' if hid is not None and hid.read_error is None else '已停止触感输出。'
            self.log(('已停止。原因：' + error + '\n' + tail) if error else ('已停止，' + tail))


def restore_leftover_hide():
    """A crash in 接管 / 原生透传 leaves the real pad hidden from every program (HidHide).
    Undo it as soon as the app opens - not only on the next passthrough start."""
    rp = ROOT / 'isolation-restore.json'
    if not rp.exists():
        return
    try:
        from takeover import Isolation
        Isolation(lambda m: None).restore_receipt(json.loads(rp.read_text('utf8')))
        (ROOT / 'traces').mkdir(exist_ok=True)
        with open(ROOT / 'traces' / 'app-log.txt', 'a', encoding='utf8') as f:
            f.write(time.strftime('%Y-%m-%d %H:%M:%S') + '  上次异常退出时手柄被隐藏，已自动恢复可见。\n')
    except Exception:
        pass


def main():
    try:                                    # native crashes (no Python traceback) leave their stack here
        import faulthandler
        (ROOT / 'traces').mkdir(exist_ok=True)
        faulthandler.enable(open(ROOT / 'traces' / 'crash.log', 'a', encoding='utf8'), all_threads=True)
    except Exception:
        pass
    # the audio loop must run every 5 ms; with the default 5 ms GIL switch interval the UI and the
    # screen reader could hold it for tens of ms -> speaker gaps / crackle
    sys.setswitchinterval(0.002)
    cfg = dict(DEFAULTS)
    if CONFIG.exists():
        try: cfg.update(json.loads(CONFIG.read_text('utf-8-sig')))
        except Exception: pass
    import i18n                            # interface language: saved choice, else the Windows display language
    i18n.set_lang(cfg.get('lang') or i18n.system_lang())
    import ui_scifi

    def save(c):
        CONFIG.write_text(json.dumps(c, ensure_ascii=False, indent=2), 'utf8')

    def test():
        try:
            from ds_audio import test_burst; test_burst()
            return '测试完成：左握把低沉一下 → 右握把清脆一下 → 两边“哒哒”。'
        except Exception as exc:
            return '测试失败：' + str(exc)
    if '--demo' not in sys.argv:
        restore_leftover_hide()
    runner = ui_scifi.DemoRunner if '--demo' in sys.argv else Runner
    lp = None if '--demo' in sys.argv else ROOT / 'traces' / 'app-log.txt'
    if lp: lp.parent.mkdir(exist_ok=True)
    ui_scifi.run_ui(cfg, runner, (lambda c: None) if '--demo' in sys.argv else save, test, lp).mainloop()


def selftest():
    """PS5Haptics.exe --selftest : no GUI, run 3 s in '普通' mode, write selftest.log next to the exe."""
    lines = []
    log = lambda m: lines.append(m)
    try:
        import platform
        p = pa_new()
        lines.append(f'python {platform.python_version()} frozen={getattr(sys, "frozen", False)} portaudio={pa.get_portaudio_version_text()}')
        w = p.get_host_api_info_by_type(pa.paWASAPI)
        d = p.get_device_info_by_index(w['defaultOutputDevice'])
        lines.append(f"default {d['name']} ch={d['maxOutputChannels']} rate={d['defaultSampleRate']}")
        for lb in p.get_loopback_device_info_generator():
            lines.append(f"loopback {lb['index']} {lb['name']} ch={lb['maxInputChannels']} rate={lb['defaultSampleRate']}")
        pa_end(p)
        try:                                       # screen reader for 鸣潮 (capture only, no game needed)
            v = WWVision(lambda: (0.0, 0.0)); v._setup()
            fr = None
            for _ in range(20):
                fr = v.cam.grab(region=v.region, new_frame_only=False)
                if fr is not None: break
                time.sleep(0.05)
            lines.append(f'ww_vision screen {v.cam.width}x{v.cam.height} region {v.region} '
                         f'grab {"ok " + str(fr.shape) if fr is not None else "none"} '
                         f'ocr {"ok" if v.ocr else repr(v.error)}')
            if v.ocr:
                import numpy as _np
                from PIL import Image, ImageDraw, ImageFont
                im = Image.new('RGB', (135, 113), (40, 50, 70))
                ImageDraw.Draw(im).text((67, 56), '7.4', fill=(255, 255, 255), font=ImageFont.truetype('C:/Windows/Fonts/arialbd.ttf', 44), anchor='mm')
                a = _np.asarray(im)[..., ::-1]
                lines.append('ocr reads 7.4 as ' + str(v.ocr.cooldown(_np.concatenate([a, _np.full(a.shape[:2] + (1,), 255, _np.uint8)], -1))))
        except Exception as exc:
            lines.append('ww_vision error: ' + repr(exc))
        cfg = dict(DEFAULTS); cfg.update(mode='普通', foreground_exe='')
        r = Runner(cfg, log); t = threading.Thread(target=r.run); t.start(); time.sleep(3); r.stop_ev.set(); t.join(15)
    except Exception:
        lines.append(traceback.format_exc())
    err = ROOT / 'last-error.txt'
    if err.exists():
        lines.append('last-error:\n' + err.read_text('utf8'))
    (ROOT / 'selftest.log').write_text('\n'.join(lines), 'utf8')


if __name__ == '__main__':
    if '--offload-guard' in sys.argv:          # elevated helper: Intel USB audio off while the app runs
        from sysfix import guard_offload
        guard_offload(sys.argv[sys.argv.index('--offload-guard') + 1])
    elif '--selftest' in sys.argv:
        selftest()
    else:
        main()

