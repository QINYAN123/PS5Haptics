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
from scipy.signal import butter, sosfilt, sosfilt_zi
from ds_audio import HapticOut, RATE, pa_new, pa_end, pa_open, rescan
from ds_hid import DualSenseHID, effect_vibration, effect_off
import trigger_presets
from selfcheck import TL_KEYS
from haptic_dsp import HapticEngine, HOP, GRIP_PROFILES, TRIGGER_PROFILES, RawSpeaker, SPEAKER_MODES, SPEAKER_MODE_ALIAS, HAPTIC_STYLES
from button_haptics import ButtonClicks
from game_caps import GameCaps, NOT_GAMES
from pad_input import InputFilter
from takeover import VirtualDS4, VirtualX360, Isolation, RumbleCoil, fuse, FUSION_ALIAS
from xds import VirtualDualSense
from event_fx import EventFX, load_config as load_event_fx
from ww_vision import WWVision
from ww_rules import STATE_TRIGGERS

ROOT = pathlib.Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else pathlib.Path(__file__).resolve().parent
CONFIG = ROOT / 'config.json'
DEFAULTS = {'grip_profile': '均衡', 'trigger_profile': '均衡冲击', 'grip_gain': 1.0, 'trigger_gain': 1.0,
            'skip_center': True, 'foreground_exe': '', 'source_hint': '',
            'mode': '原生透传', 'fusion': '叠加', 'trigger_mode': '自动', 'rumble_gain': 1.0, 'speaker_mode': '特殊事件', 'speaker_eq': '原味', 'speaker_volume': 0.8, 'haptic_style': '游戏', 'pbz_fx': True, 'ww_fx': True, 'ww_enabled': True,
            'cinema_rumble': 1.0, 'cinema_kick': 1.0, 'cinema_trigger': 1.0, 'music_kick': 1.0, 'music_tick': 1.0, 'music_trigger': 1.0, 'record_native': True, 'event_fx': True,
            **trigger_presets.scene_defaults(), 'user_games': [],
            'ww_vision': True, 'ww_click_gate': True, 'game_profiles': True, 'capture': '自动', 'speaker_scene': True, 'pbz_log_dir': ''}


WW_EXE = 'client-win64-shipping.exe'
PBZ_EXE = 'pbz.exe'
# 影之刃零 stuck-vibration guard (reported after some moves, e.g. 少主剑 R1+△): in this game every trigger vibration
# the game sends is momentary and long coil rumbles (ship, slides) come with a stream of game events; so a trigger
# vibration held for seconds, motor values frozen for seconds, or coil rumble with no game event for seconds is a
# vibration the game forgot to stop, and is let go until the game changes it.
STUCK_TRIGGER_S, STUCK_MOTOR_S, STUCK_WAVE_S, QUIET_EVENTS_S = 3.0, 5.0, 6.0, 4.0


class StuckGuard:
    def __init__(self, log):
        self.log = log; self.seen = {}; self.wave_since = None; self.said = set()

    def _say(self, what):
        if what not in self.said:
            self.said.add(what); self.log(f'检测到游戏的{what}一直没有停（游戏没发停止指令），已自动松开；游戏换成新的效果时恢复。')

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

    def motors(self, l, r, now):
        t = self.held('motor', (l, r), now)
        if (l or r) and t > STUCK_MOTOR_S:
            self._say('马达震动'); return 0, 0
        return l, r

    def wave(self, level, last_event, now):
        """True = mute the game's coil wave: strong for a while and no game event lately."""
        if level < 0.15:
            self.wave_since = None; return False
        self.wave_since = self.wave_since or now
        if now - self.wave_since > STUCK_WAVE_S and last_event and now - last_event > QUIET_EVENTS_S:
            self._say('握把震动'); return True
        return False


# per-game tuning applied while that game is in the foreground
GAME_PROFILES = {
    WW_EXE: {'name': '鸣潮', 'rumble_scale': 0.6, 'duck': 0.25, 'grip_profile': '细节丰富', 'vision': True},
}


def foreground_exe():
    u = C.windll.user32; k = C.windll.kernel32
    u.GetForegroundWindow.restype = W.HWND
    hwnd = u.GetForegroundWindow()
    pid = W.DWORD(); u.GetWindowThreadProcessId(hwnd, C.byref(pid))
    k.OpenProcess.restype = W.HANDLE
    h = k.OpenProcess(0x1000, False, pid.value)
    if not h:
        # protected processes (anti-cheat) may refuse even limited queries: fall back to the title
        title = C.create_unicode_buffer(256); u.GetWindowTextW(hwnd, title, 256)
        return WW_EXE if ('鸣潮' in title.value or 'Wuthering' in title.value) else ''
    try:
        buf = C.create_unicode_buffer(1024); n = W.DWORD(1024)
        return pathlib.Path(buf.value).name.lower() if k.QueryFullProcessImageNameW(h, 0, buf, C.byref(n)) else ''
    finally:
        k.CloseHandle(h)


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
    """Stream resampler with phase carried across blocks (per-block np.interp restarted at every block
    edge and clicked on the speaker). ratio = input samples consumed per output sample."""

    def __init__(self):
        self.pos = 0.0; self.prev = None

    def process(self, l, r, ratio):
        if len(l) == 0:
            return l, r
        if self.prev is None:
            self.prev = (l[0], r[0])
        xl = np.concatenate([[self.prev[0]], l]); xr = np.concatenate([[self.prev[1]], r])
        last = len(xl) - 1
        n = int(np.floor((last - self.pos) / ratio)) + 1 if self.pos <= last else 0
        p = self.pos + np.arange(n) * ratio
        self.pos = (self.pos + n * ratio) - last
        self.prev = (xl[-1], xr[-1])
        idx = np.arange(len(xl))
        return np.interp(p, idx, xl).astype(np.float32), np.interp(p, idx, xr).astype(np.float32)


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

    def _audio_cb(self, data, frames, info, status):
        if self.proc is not None and self.proc.sounding():  # game captured per process: ignore the mix
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

    def _open_loopback(self, hint_override=None):
        """Find the default output's loopback and open it. Right after a device change (pad re-plugged,
        audio device disabled) WASAPI can fail for a few seconds: re-enumerate and retry."""
        last = None
        for attempt in range(8):
            if self.stop_ev.is_set():
                raise RuntimeError('已取消')
            p = pa_new()
            try:
                w = p.get_host_api_info_by_type(pa.paWASAPI)
                default_name = p.get_device_info_by_index(w['defaultOutputDevice'])['name']
                if not (hint_override or self.cfg.get('source_hint')) and 'DualSense' in default_name:
                    pa_end(p)
                    raise PermissionError('默认声音输出设备是 PS5 手柄本身。请在 Windows 声音设置里把默认输出改回音箱/电视，再点开始。')
                hint = hint_override or self.cfg.get('source_hint') or default_name
                src = next((d for d in p.get_loopback_device_info_generator() if hint in d['name'] and 'DualSense' not in d['name']), None)
                if src is None:
                    raise LookupError('找不到声音输出设备的环回（loopback）：' + hint)
                return p, src, self._open_stream(p, src)
            except PermissionError as exc:
                raise RuntimeError(str(exc))
            except Exception as exc:
                last = exc
                try: pa_end(p)
                except Exception: pass
                self.log(f'声音设备还没准备好，正在重试（{attempt + 1}/8）…')
                time.sleep(1.5)
        raise RuntimeError('打开声音设备失败：' + str(last))

    def run(self):
        p = None; out = hid = stream = vpad = iso = None; error = None; vision = None; watcher = None; trace = None; volume = None
        voice_gate = None; pad_tap = None; g_env = 0.05; g_out_pk = 0.0; spk_clip = None; gsp_z = sosfilt_zi(GSP_LOWCUT) * 0.0
        try:                                         # a game with its own settings already running: start with them (routing is fixed per run)
            import proc_loopback as plb
            here = foreground_exe()
            start_g = self.profile_for(here) or next((g for g in (self.profile_for(e) for e in self.user_exes()) if g and plb.find_pid(pathlib.Path(g['exe']).name)), None)
            if start_g is not None:
                self.cfg = self.effective(pathlib.Path(start_g['exe']).name.lower()); self.profile = start_g.get('name')
                self.log(f'使用「{self.profile}」的专属设置')
        except Exception:
            pass
        mode = self.cfg.get('mode')
        native = mode == '原生透传'                 # virtual DualSense: game's own triggers + haptics
        xbox = mode == 'Xbox'                       # virtual Xbox pad: games that only accept Xbox controllers
        takeover = mode == '接管' or native or xbox  # any mode that hides the real pad
        try:
            rescan()                                  # see a pad / audio device plugged in since the app opened
            p, src, stream = self._open_loopback()
            ch = src['maxInputChannels']; rate = int(src['defaultSampleRate'])
            self.stats['source'] = f"{src['name']}  {ch}声道 {rate}Hz"
            out = HapticOut(p); hid = DualSenseHID()   # open the REAL pad before any virtual one exists
            real_out = out.dev['name']
            fw = hid.firmware_info()
            if fw:
                self.log(f"手柄固件：fw {fw['fw']:08x} / hw {fw['hw']:08x} / update {fw['update']:04x} / {fw['date']} {fw['time']}")
            if takeover:
                iso = Isolation(self.log); iso.open(hid.path)
                vpad = VirtualDualSense(self.log) if native else VirtualX360() if xbox else VirtualDS4()
                vpad.open()
                self.pad_filter = InputFilter(self.cfg.get('stick_dz_l', 0.0), self.cfg.get('stick_dz_r', 0.0), self.cfg.get('gyro_bias') or (0, 0, 0))
                hid.start_reader(lambda rep, f=self.pad_filter, send=vpad.forward: send(f.apply(rep)))
                self.stats['takeover'] = True
                if native:                         # the game's own pad-speaker sound lands on the virtual pad
                    from pad_speaker import PadSpeakerTap, SpeakerClip
                    pad_tap = PadSpeakerTap(real_out, self.log, self.stop_ev); pad_tap.start()
                    if self.cfg.get('record_native'):
                        spk_clip = SpeakerClip(ROOT / 'traces')
            if self.cfg.get('speaker_mode', '冲击声') != '关闭':
                try: hid.enable_speaker(headset=self.cfg.get('speaker_output') == '耳机孔')          # undo a mute / headset routing left by a game
                except Exception: pass
            try: hid.set_lightbar(0, 80, 255)
            except Exception: pass
            eng = HapticEngine(self.cfg['grip_profile'], self.cfg['trigger_profile'], self.cfg['grip_gain'], self.cfg['trigger_gain'],
                               self.cfg.get('haptic_style', '游戏'), groove_gains(self.cfg))
            ep_gain = 1.0; source_endpoint = False; pad_session_check = 0.0; fg_since = time.monotonic(); last_game_trig = (None, None); trig_released = False
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
            base_rgain = rgain; duck = 0.5; prof = None; vision = None; fg = ''
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
            keep_running = takeover or spk is not None
            clock0 = time.monotonic(); produced = 0       # haptic samples emitted vs wall clock
            alive_check = time.monotonic(); heard = time.monotonic(); last_blk = 0.0; rs = Resampler()
            diag_t = diag_prev = time.monotonic(); diag_loop = 0.0; live_seen = 0; spk_eff = spk_mode
            late_mark = 0                         # drops counted up to the last normal loop pass
            trig = (0.0, 0.0)                     # a block shorter than one hop (resampled) must not leave it unset
            tl_acc = dict.fromkeys(TL_KEYS, 0.0); tl_t = 0.0; static = (None, None); last_static = static
            mode_warned = set(); stuck = StuckGuard(self.log)

            def ours(i):                          # this side's own effect: a trigger-feel preset, else follow the sound
                if static[i] is not None:
                    return static[i]
                return effect_vibration(amp[i], 30 + 25 * trig[i]) if amp[i] else effect_off()
            if self.cfg.get('capture', '自动') == '自动':
                # known games are captured per process: works with Dolby Atmos / spatial sound, where the
                # endpoint loopback is silent, and leaves music / voice chat out of the haptics
                try:
                    from proc_loopback import ProcSource
                    scene_name = self.cfg.get('scene', '游戏')
                    if scene_name == '游戏':                    # adapted games first, then the program in front
                        exes = [e for e in list(GAME_PROFILES) + [PBZ_EXE, self.cfg.get('foreground_exe', '').strip().lower()] + self.user_exes() if e]
                        players = 'foreground'
                    else:                                 # films / music: whatever is playing (player, browser, music app)
                        exes, players = [], 'loudest'
                    self.proc = ProcSource(self.q, exes, self.log, self.stop_ev, players); self.proc.start()
                except Exception as exc:
                    self.log('按进程截音不可用，使用系统混音：' + str(exc))
            ep_source = self.stats['source']
            default_id = None
            if not self.cfg.get('source_hint'):
                try:
                    from audio_default import DefaultOutputWatcher
                    watcher = DefaultOutputWatcher(); default_id = watcher.current()
                except Exception as exc:
                    self.log('无法监视默认声音输出（不影响使用）：' + str(exc))
            while not self.stop_ev.is_set():
                if time.monotonic() - alive_check > 1.0:          # audio device reset mid-session -> reopen
                    alive_check = time.monotonic()
                    now_id = watcher.current() if watcher else None
                    if now_id and now_id != default_id:
                        # default output switched (e.g. laptop speakers -> TV): PortAudio only sees the new
                        # default after a full re-initialisation, so close everything audio and reopen
                        default_id = now_id; prev = src['name'].replace(' [Loopback]', '')
                        try: stream.stop_stream(); stream.close()
                        except Exception: pass
                        try: out.close()
                        except Exception: pass
                        pa_end(p); p = None; rescan()
                        try:
                            p, src, stream = self._open_loopback()
                        except RuntimeError as exc:
                            self.log(str(exc) + ' 继续使用原来的设备。')
                            p, src, stream = self._open_loopback(hint_override=prev)
                        ch = src['maxInputChannels']; rate = int(src['defaultSampleRate'])
                        out = HapticOut(p, real_out)               # the REAL pad, not the virtual one
                        self.stats['source'] = ep_source = f"{src['name']}  {ch}声道 {rate}Hz"
                        while not self.q.empty():
                            try: self.q.get_nowait()
                            except queue.Empty: break
                        pend_l = np.zeros(0, np.float32); pend_r = np.zeros(0, np.float32)
                        clock0 = time.monotonic(); produced = 0
                        self.log('默认声音输出已切换，触感声源跟随到：' + self.stats['source'])
                        continue
                    if not stream.is_active():
                        try: stream.close()
                        except Exception: pass
                        try:
                            stream = self._open_stream(p, src); self.log('声音设备刚才重置过，已重新连接。')
                        except Exception:
                            pass
                if hid.read_error is not None:
                    raise RuntimeError('PS5 手柄断开了（' + str(hid.read_error) + '）')
                try:
                    blk = self.q.get(timeout=0.01 if keep_running or self.cfg.get('button_haptics') or self.cfg.get('trigger_haptics') or self.cfg.get('stick_haptics') or self.cfg.get('touch_haptics') else 0.2)
                    tnow = time.monotonic()
                    if tnow - diag_t > 30:              # audio health line in the log every 30 s
                        pr = self.proc
                        self.log(f'音频诊断：主循环最长停顿 {diag_loop * 1000:.0f} ms，游戏声音包最长间隔 '
                                 f'{(pr.max_gap * 1000 if pr else 0):.0f} ms，重连 {pr.reattach if pr else 0} 次，'
                                 f'积压清空 {self.stats.get("resync", 0)} 次，丢块 {self.stats["late"]}'
                                 + (f'，游戏原生扬声器峰值 {self.stats.get("game_spk", 0)}（断续 {pad_tap.underruns if pad_tap else 0} 次）（{vpad.spk_frames} 帧有声）' if native and vpad is not None else '')
                                 + (f'，转到真手柄峰值 {g_out_pk:.2f}（增益 {min(10.0, 0.7 / g_env):.1f}x）' if pad_tap is not None else ''))
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
                if blk is not None:
                    from_endpoint = not isinstance(blk, tuple)
                    if isinstance(blk, tuple):                      # per-process capture: stereo 48 kHz
                        blk = blk[1]; bch, brate = 2, RATE
                        if not (self.proc and self.proc.sounding()):
                            blk = None                             # silent game process: the mix is used
                    else:
                        bch, brate = ch, rate
                        if self.proc is not None and self.proc.sounding():
                            blk = None                             # game process has sound: it wins
                if blk is not None:
                    source_endpoint = from_endpoint
                    if np.any(blk):
                        heard = time.monotonic()
                    l, r = downmix(blk, bch, self.cfg['skip_center'])
                    lag = self.q.qsize() * len(l) / brate + len(pend_l) / RATE      # seconds waiting
                    self.stats['lag_ms'] = int(lag * 1000)
                    if lag > 0.3:                                   # far behind: drop the backlog once
                        while not self.q.empty():
                            try: self.q.get_nowait()
                            except queue.Empty: break
                        pend_l = pend_l[-HOP:]; pend_r = pend_r[-HOP:]
                        self.stats['resync'] = self.stats.get('resync', 0) + 1
                    speed = 1.0 + min(0.04, max(0.0, lag - 0.008) * 1.5)   # <=4 % faster while behind
                    if brate != RATE or speed != 1.0 or rs.pos:
                        l, r = rs.process(l, r, brate / RATE * speed)
                    last_blk = time.monotonic()
                    pend_l = np.concatenate([pend_l, l]); pend_r = np.concatenate([pend_r, r])
                elif ((takeover or any(self.cfg.get(k) for k in CONTROL_HAPTICS.values())) and len(pend_l) < HOP and time.monotonic() - last_blk > 0.1
                      and (time.monotonic() - clock0) * RATE - produced > 4 * HOP):
                    # only when the source has really stopped (0.1 s): padding inside a stream of 10 ms
                    # packets inserted gaps = the speaker crackled and stuttered
                    # game is silent (loopback delivers nothing): pad with real-time silence so the game's
                    # own haptics keep flowing; only when genuinely behind the clock, never ahead of it
                    z = np.zeros(2 * HOP, np.float32)
                    pend_l = np.concatenate([pend_l, z]); pend_r = np.concatenate([pend_r, z])
                else:
                    continue
                now = time.monotonic()
                if (now - clock0) * RATE - produced > RATE:    # >1 s behind (stall): resync the clock
                    clock0 = now; produced = 0
                if now - fg_check > 0.5:
                    fg_check = now; new_fg = foreground_exe()
                    if new_fg != fg:
                        fg_since = now
                    fg = new_fg
                    g_now = self.profile_for(fg)
                    g_name = g_now.get('name') if g_now else None
                    if g_name != self.profile:                  # the person's own settings for the game in front
                        self.profile = g_name; self.cfg = self.effective(fg); live_seen = -1
                        self.log(f'使用「{g_name}」的专属设置' if g_name else '前台不是有专属设置的游戏，使用通用设置')
                        if g_now and (self.cfg.get('mode') != mode or self.cfg.get('capture') != self.start_cfg.get('capture')) and g_name not in mode_warned:
                            mode_warned.add(g_name)
                            self.log(f'「{g_name}」的连接方式或声音来源和当前不同：先打开游戏，再停止并重新启动本软件后生效。')
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
                        if self.proc is not None:              # scene changed: which program to listen to
                            game_scene = self.cfg.get('scene', '游戏') == '游戏'
                            self.proc.players = 'foreground' if game_scene else 'loudest'
                            self.proc.exes = [e for e in list(GAME_PROFILES) + [PBZ_EXE, self.cfg.get('foreground_exe', '').strip().lower()] + self.user_exes() if e] if game_scene else []
                    # 全部游戏声: the whole mix, even over a game's own pad-speaker sound.
                    # 特殊事件: a game that drives the pad speaker itself keeps it; 影之刃零 keeps its event tuning;
                    # games that don't use the pad speaker play the whole mix.
                    # grips on 纯用游戏 + 特殊事件 = nothing of ours at all: no event tuning, no whole mix as a fallback
                    pure = FUSION_ALIAS.get(fusion, fusion) == '纯用游戏' and spk_mode == '特殊事件'
                    adapted = fg == PBZ_EXE and self.cfg.get('pbz_fx', True) and not pure     # 特殊事件: only 影之刃零 is adapted
                    spk_eff = ('静默' if pure else '全部游戏声') if (spk_mode == '特殊事件' and not adapted) else spk_mode
                    if spk_mode == '特殊事件' and pad_tap is not None and (pad_tap.sounding(2.0) or (adapted and pad_tap.last_sound)):
                        spk_eff = '游戏原生'          # the game drives the pad speaker itself (like on PS5): use exactly that
                    if spk_mode == '特殊事件' and spk is not None and 'DualSense' not in src['name']:
                        # the game in front uses the controller speaker itself: step aside, now and on later launches.
                        # Its own audio session decides, so a game left in the background is not blamed on the browser in front.
                        may_use_pad = (pad_tap is not None and pad_tap.sounding(0.6)) or (pad_tap is None and not takeover)
                        if may_use_pad and fg not in NOT_GAMES and not self.caps.has(fg, 'speaker') and now - pad_session_check > 1.0:
                            pad_session_check = now
                            try:
                                from pad_speaker import game_on_pad
                                if game_on_pad(fg) and self.caps.note(fg, 'speaker'):
                                    self.log(f'检测到 {fg} 自己在用手柄扬声器，之后这款游戏的扬声器交给游戏')
                            except Exception:
                                pass                               # can't tell: keep playing as chosen
                        if self.caps.has(fg, 'speaker') and not adapted:
                            spk_eff = '游戏原生' if pad_tap is not None else '游戏接管'
                    self.stats['spk_eff'] = spk_eff
                    if self.proc is not None and self.proc.sounding():
                        self.stats['source'] = f'{self.proc.name}（按进程截音）  2声道 48000Hz'
                    else:
                        self.stats['source'] = ep_source
                    # a game in front but pure digital silence on the loopback for 15 s: its audio bypasses
                    # the shared mix (Windows spatial sound / Dolby Atmos, or exclusive mode)
                    silent = bool(fg) and fg in GAME_PROFILES and now - heard > 15
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
                        if vision is not None:
                            vision.stop(); vision = None
                            if fx is not None:
                                fx.set_state_trigger('L', None); fx.set_state_trigger('R', None)
                            if native and vpad is not None:
                                vpad.gate['L'] = vpad.gate['R'] = None
                        if prof and prof.get('vision') and fx is not None and self.cfg.get('ww_vision', True):
                            tp = None
                            if self.cfg.get('record_native', True):
                                (ROOT / 'traces').mkdir(exist_ok=True)
                                tp = ROOT / 'traces' / time.strftime('ww-%Y%m%d-%H%M%S.tsv')
                            cd = (ROOT / 'traces' / 'ww-crops') if self.cfg.get('ww_collect', False) else None
                            sd = (ROOT / 'traces' / 'ww-shots') if self.cfg.get('ww_shots', True) else None
                            vision = WWVision(lambda: (hid.l2 / 255, hid.r2 / 255), dpad=lambda: getattr(hid, 'dpad', None), buttons=lambda: getattr(hid, 'held', 0), trace_path=tp, mem=proc_mem_mb, crops_dir=cd, shots_dir=sd, fps=10); vision.start()   # 10 fps: half the screen-capture load (was 20)
                        self.stats['profile'] = prof['name'] if prof else ''
                press = hid.poll_input(); self.stats['battery'] = hid.battery
                if vision is not None and fx is not None:
                    for ev in vision.poll():
                        name = fx.feed(*ev)
                        if name: self.stats['fx_last'] = name
                    st = vision.states()
                    for side, (skill, eff, gate) in STATE_TRIGGERS.items():
                        on = bool(st.get(skill))
                        fx.set_state_trigger(side, eff if on else None)
                        if native and vpad is not None:            # game sees the press only past the click
                            vpad.gate[side] = self.cfg.get('ww_gate_' + side, gate) if on and self.cfg.get('ww_click_gate', True) else None
                    self.stats['ww'] = (st, vision.frames, str(vision.error) if vision.error else '')
                if fx is not None:
                    for name in fx.poll():
                        self.stats['fx_last'] = name
                large = small = 0.0; gpk_l = gpk_r = 0.0; spk_peak = 0.0
                if spk is not None and time.monotonic() - spk_assert > 5.0:   # keep the speaker routed
                    spk_assert = time.monotonic()
                    try: hid.enable_speaker(headset=self.cfg.get('speaker_output') == '耳机孔')
                    except Exception: pass
                while len(pend_l) >= HOP:
                    bl, br = pend_l[:HOP], pend_r[:HOP]; pend_l, pend_r = pend_l[HOP:], pend_r[HOP:]
                    produced += HOP
                    if not active:
                        bl = np.zeros(HOP, np.float32); br = bl
                    if ep_gain != 1.0 and source_endpoint:           # haptics only: the speaker keeps following the system volume
                        bl = np.clip(bl * ep_gain, -1, 1); br = np.clip(br * ep_gain, -1, 1)
                    wl, wr, trig = eng.process(bl, br, press)
                    tl_snd_l, tl_snd_r = wl, wr
                    if self.cfg.get('voice_gate') and voice_gate is not False:   # pure dialogue must not shake the grips
                        if voice_gate is None:
                            try:
                                from voice_gate import VoiceGate
                                voice_gate = VoiceGate()
                            except Exception as exc:
                                voice_gate = False; self.log('人声识别不可用（不影响其他功能）：' + str(exc))
                        if voice_gate:
                            vg = voice_gate.gain(bl, br)
                            self.stats['voice_closed'] = voice_gate.closed
                            if vg < 0.999:
                                wl = wl * vg; wr = wr * vg; trig = (trig[0] * vg, trig[1] * vg)
                    else:
                        self.stats['voice_closed'] = False
                    if native:
                        (rleft, rright), _, color = vpad.feedback()
                        if fg == PBZ_EXE:
                            rleft, rright = stuck.motors(rleft, rright, time.monotonic())
                        large, small = rleft / 255, rright / 255
                        nl, nr = vpad.wave.pull(HOP)                  # game's own voice-coil waveform
                        rl, rr = rum.step(large, small, rgain)         # legacy rumble, if the game uses it
                        nl = nl * rgain + rl; nr = nr * rgain + rr
                        if fx is not None and FUSION_ALIAS.get(fusion, fusion) != '纯用游戏':   # event tuning: grade / duck the native layer
                            g = fx.native_gain(HOP); nl = nl * g; nr = nr * g
                        nlevel = float(max(np.max(np.abs(nl)), np.max(np.abs(nr))))
                        if fg == PBZ_EXE and fx is not None and stuck.wave(nlevel, getattr(fx, 'last_event', 0.0), time.monotonic()):
                            nl = nl * 0; nr = nr * 0; nlevel = 0.0
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
                        tl_acc['mot'] = max(tl_acc['mot'], large, small)
                        rl, rr = rum.step(large, small, rgain)
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
                        gsp, gsp_z = sosfilt(GSP_LOWCUT, gsp, zi=gsp_z)
                        gsp = gsp.astype(np.float32)
                        # the game mixes its pad-speaker cues quietly (on PS5 the console volume lifts them):
                        # slow make-up gain to ~70 % peak, at most 10x; the speaker-volume slider applies on top
                        gpk_now = float(np.max(np.abs(gsp)))
                        g_env = max(gpk_now, g_env * 0.9995, 0.02)
                        gsp = gsp * min(10.0, 0.7 / g_env)
                    if spk is not None:
                        spk.analyse(bl, br)
                        if spk_eff == '全部游戏声':
                            sp = spk.render(spk_eq, vol)
                        elif spk_eff == '游戏原生':
                            sp = np.clip(gsp * vol, -1, 1).astype(np.float32)
                        if gsp is not None:
                            g_out_pk = max(g_out_pk, float(np.max(np.abs(gsp))) * vol)
                        ev_on = fx is not None and fx.speaker_on() and spk_eff == '特殊事件'
                        if ev_on or spk_mix:                       # 特殊事件: the moment's own sound, its EQ, louder
                            se = spk.render(fx.speaker_eq if (fx is not None and fx.speaker_eq) else None, max(0.8, vol), 1.5)
                    if se is not None:                             # key event (parry, perfect dodge, ultimate...):
                        spk_want = 1.0 if ev_on else 0.0           # crossfade in / out
                        if spk_want or spk_mix:
                            old = spk_mix                          # ~3 hops fade: no clicks at the edges
                            spk_mix = min(spk_want, old + 0.34) if spk_want > old else max(spk_want, old - 0.34)
                            ramp = np.linspace(old, spk_mix, HOP, dtype=np.float32)
                            base = sp if sp is not None else np.zeros(HOP, np.float32)
                            sp = (base * (1 - ramp) + se * ramp).astype(np.float32)
                    if sp is not None:
                        spk_peak = max(spk_peak, float(np.max(np.abs(sp))))
                    if self.cfg.get('swap_grips'):
                        wl, wr = wr, wl
                    gl_gain, gr_gain = self.cfg.get('grip_left_gain', 1.0), self.cfg.get('grip_right_gain', 1.0)
                    if gl_gain != 1.0 or gr_gain != 1.0:              # per-hand strength: everything that reaches that grip
                        wl = np.clip(wl * gl_gain, -1, 1).astype(np.float32); wr = np.clip(wr * gr_gain, -1, 1).astype(np.float32)
                    out.feed(wl, wr, sp)
                    try:
                        tl_acc['snd'] = max(tl_acc['snd'], float(np.max(np.abs(tl_snd_l))), float(np.max(np.abs(tl_snd_r))))
                    except NameError:
                        pass
                    tl_acc['out_l'] = max(tl_acc['out_l'], float(np.max(np.abs(wl)))); tl_acc['out_r'] = max(tl_acc['out_r'], float(np.max(np.abs(wr))))
                    gpk_l = max(gpk_l, float(np.max(np.abs(wl)))); gpk_r = max(gpk_r, float(np.max(np.abs(wr))))
                if len(pend_l) > 4 * HOP:          # never build up latency
                    pend_l, pend_r = pend_l[-HOP:], pend_r[-HOP:]
                static = trigger_presets.effects(self.cfg)
                amp = (int(round(min(1.0, trig[0] * self.cfg.get('trigger_left_gain', 1.0)) * 8)),
                       int(round(min(1.0, trig[1] * self.cfg.get('trigger_right_gain', 1.0)) * 8)))
                game_l = game_r = None
                if native:
                    game_l, game_r, _ = vpad.triggers()
                    if fg == PBZ_EXE:
                        tnow = time.monotonic()
                        game_l = stuck.trigger('L', game_l, tnow); game_r = stuck.trigger('R', game_r, tnow)
                    if trace is not None and (game_l, game_r) != trace_trig:
                        trace_trig = (game_l, game_r)
                        trace.write(f"{int(time.time() * 1000)}\ttrigger\t{game_l.hex() if game_l else '-'}\t{game_r.hex() if game_r else '-'}\n")
                    off = lambda e: e is None or e[0] in (0x00, 0x05)
                    game_trig = not (off(game_l) and off(game_r))
                    trig_mode = self.cfg.get('trigger_mode', '自动')
                    # a fresh command while this program has been in front for a while: a background game's
                    # latched effect must not be blamed on the browser in front
                    if (game_trig and (game_l, game_r) != last_game_trig and time.monotonic() - fg_since > 3.0
                            and self.caps.note(fg, 'trigger') and trig_mode == '自动'):
                        self.log(f'检测到 {fg} 自己在用自适应扳机，之后这款游戏不再叠加扳机震动')
                    last_game_trig = (game_l, game_r)
                    if trig_mode == '纯用游戏':                       # ours is off: exactly what the game sends
                        hid.set_triggers(game_l or effect_off(), game_r or effect_off())
                        last_trig = (-1, -1); amp = None
                    elif trig_mode == '自动' and (game_trig or self.caps.has(fg, 'trigger')):
                        # the game drives the triggers: pass its effects through untouched
                        ov_l, ov_r = fx.triggers() if fx is not None else (None, None)
                        hid.set_triggers(ov_l or game_l or effect_off(), ov_r or game_r or effect_off())
                        last_trig = (-1, -1); amp = None
                    elif trig_mode == '叠加' and game_trig:
                        # a trigger runs one effect at a time: a side the game is using stays the game's, the other is ours
                        ov_l, ov_r = fx.triggers() if fx is not None else (None, None)
                        lf = ov_l or (game_l if not off(game_l) else ours(0))
                        rf = ov_r or (game_r if not off(game_r) else ours(1))
                        hid.set_triggers(lf, rf); last_trig = (-1, -1); amp = None
                elif self.cfg.get('trigger_mode', '自动') == '纯用游戏':
                    # no game commands reach us here; in overlay mode the game writes to the pad itself, so stay off it
                    if not trig_released:
                        hid.set_triggers(effect_off(), effect_off(), force=True); trig_released = True; last_trig = (-1, -1)
                    amp = None
                else:
                    trig_released = False
                if amp is not None and fx is not None:
                    ov_l, ov_r = fx.triggers()
                    if ov_l or ov_r:                                 # event trigger effect wins for its duration
                        lf = ov_l or ours(0)
                        rf = ov_r or ours(1)
                        hid.set_triggers(lf, rf); last_trig = (-1, -1); amp = None
                if amp is not None and (amp != last_trig or static != last_static):
                    if hid.set_triggers(ours(0), ours(1)):
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
            for o in (stream,):
                try:
                    if o: o.stop_stream(); o.close()
                except Exception: pass
            try:
                if trace is not None: trace.close()
            except Exception: pass
            if vision is not None:
                vision.stop()
            try:
                if watcher is not None: watcher.close()
                if spk_clip is not None: spk_clip.close()
                if volume is not None: volume.close()
            except Exception: pass
            for o in (hid, vpad):
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
            tail = '扳机和握把已释放。' + ('真手柄已对游戏恢复可见。' if takeover else '')
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
