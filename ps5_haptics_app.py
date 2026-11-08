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
from ds_audio import HapticOut, RATE, pa_new, pa_end, pa_open, rescan
from ds_hid import DualSenseHID, effect_vibration, effect_off
from haptic_dsp import HapticEngine, HOP, GRIP_PROFILES, TRIGGER_PROFILES, RawSpeaker, SPEAKER_MODES, SPEAKER_MODE_ALIAS, HAPTIC_STYLES
from takeover import VirtualDS4, Isolation, RumbleCoil, fuse
from xds import VirtualDualSense
from event_fx import EventFX, load_config as load_event_fx
from ww_vision import WWVision
from ww_rules import STATE_TRIGGERS

ROOT = pathlib.Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else pathlib.Path(__file__).resolve().parent
CONFIG = ROOT / 'config.json'
DEFAULTS = {'grip_profile': '均衡', 'trigger_profile': '均衡冲击', 'grip_gain': 1.0, 'trigger_gain': 1.0,
            'skip_center': True, 'foreground_exe': '', 'source_hint': '',
            'mode': '原生透传', 'fusion': '融合', 'rumble_gain': 1.0, 'speaker_mode': '原声', 'speaker_eq': '原味', 'speaker_volume': 0.8, 'haptic_style': '游戏', 'pbz_fx': True, 'ww_fx': True, 'ww_enabled': True,
            'cinema_rumble': 1.0, 'cinema_kick': 1.0, 'cinema_trigger': 1.0, 'music_kick': 1.0, 'music_tick': 1.0, 'music_trigger': 1.0, 'record_native': True, 'event_fx': True,
            'ww_vision': True, 'ww_click_gate': True, 'game_profiles': True, 'capture': '自动', 'speaker_scene': True, 'pbz_log_dir': ''}


WW_EXE = 'client-win64-shipping.exe'
PBZ_EXE = 'pbz.exe'
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


class Runner:
    def __init__(self, cfg, log):
        self.cfg = cfg; self.log = log; self.stop_ev = threading.Event(); self.q = queue.Queue(maxsize=400)   # ~2 s of blocks: the lag control drains it, nothing is dropped
        self.start_cfg = dict(cfg)          # what this run was started with (routing / source need a restart)
        self.live_ver = 0                   # bumped by the UI when settings change: applied within 0.5 s
        self.proc = None
        self.stats = {'level': 0.0, 'trigger': (0.0, 0.0), 'active': False, 'source': '', 'late': 0, 'rumble': (0.0, 0.0), 'vpad': None, 'takeover': False, 'native_trig': (False, False)}

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
        p = None; out = hid = stream = vpad = iso = None; error = None; vision = None; watcher = None; trace = None
        pad_tap = None; g_env = 0.05; g_out_pk = 0.0
        mode = self.cfg.get('mode')
        native = mode == '原生透传'                 # virtual DualSense: game's own triggers + haptics
        takeover = mode == '接管' or native          # any mode that hides the real pad
        try:
            rescan()                                  # see a pad / audio device plugged in since the app opened
            p, src, stream = self._open_loopback()
            ch = src['maxInputChannels']; rate = int(src['defaultSampleRate'])
            self.stats['source'] = f"{src['name']}  {ch}声道 {rate}Hz"
            out = HapticOut(p); hid = DualSenseHID()   # open the REAL pad before any virtual one exists
            real_out = out.dev['name']
            if takeover:
                iso = Isolation(self.log); iso.open()
                vpad = VirtualDualSense(self.log) if native else VirtualDS4()
                vpad.open()
                hid.start_reader(vpad.forward)
                self.stats['takeover'] = True
                if native:                         # the game's own pad-speaker sound lands on the virtual pad
                    from pad_speaker import PadSpeakerTap
                    pad_tap = PadSpeakerTap(real_out, self.log, self.stop_ev); pad_tap.start()
            if self.cfg.get('speaker_mode', '冲击声') != '关闭':
                try: hid.enable_speaker()          # undo a mute / headset routing left by a game
                except Exception: pass
            try: hid.set_lightbar(0, 80, 255)
            except Exception: pass
            eng = HapticEngine(self.cfg['grip_profile'], self.cfg['trigger_profile'], self.cfg['grip_gain'], self.cfg['trigger_gain'],
                               self.cfg.get('haptic_style', '游戏'), groove_gains(self.cfg))
            rum = RumbleCoil(); vol = self.cfg.get('speaker_volume', 0.6)
            spk_mode = SPEAKER_MODE_ALIAS.get(self.cfg.get('speaker_mode', '原声'), self.cfg.get('speaker_mode', '原声'))
            spk_eq = self.cfg.get('speaker_eq', '原味')
            spk = RawSpeaker() if spk_mode != '关闭' else None
            spk_mix = 0.0; spk_peak = 0.0; spk_assert = time.monotonic()
            fusion = self.cfg.get('fusion', '融合'); rgain = self.cfg.get('rumble_gain', 1.0)
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
            if self.cfg.get('capture', '自动') == '自动':
                # known games are captured per process: works with Dolby Atmos / spatial sound, where the
                # endpoint loopback is silent, and leaves music / voice chat out of the haptics
                try:
                    from proc_loopback import ProcSource
                    scene_name = self.cfg.get('scene', '游戏')
                    if scene_name == '游戏':                    # adapted games first, then the program in front
                        exes = [e for e in list(GAME_PROFILES) + [PBZ_EXE, self.cfg.get('foreground_exe', '').strip().lower()] if e]
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
                    blk = self.q.get(timeout=0.01 if keep_running else 0.2)
                    tnow = time.monotonic()
                    if tnow - diag_t > 30:              # audio health line in the log every 30 s
                        pr = self.proc
                        self.log(f'音频诊断：主循环最长停顿 {diag_loop * 1000:.0f} ms，游戏声音包最长间隔 '
                                 f'{(pr.max_gap * 1000 if pr else 0):.0f} ms，重连 {pr.reattach if pr else 0} 次，'
                                 f'积压清空 {self.stats.get("resync", 0)} 次，丢块 {self.stats["late"]}'
                                 + (f'，游戏原生扬声器峰值 {self.stats.get("game_spk", 0)}（{vpad.spk_frames} 帧有声）' if native and vpad is not None else '')
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
                    if isinstance(blk, tuple):                      # per-process capture: stereo 48 kHz
                        blk = blk[1]; bch, brate = 2, RATE
                        if not (self.proc and self.proc.sounding()):
                            blk = None                             # silent game process: the mix is used
                    else:
                        bch, brate = ch, rate
                        if self.proc is not None and self.proc.sounding():
                            blk = None                             # game process has sound: it wins
                if blk is not None:
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
                elif (takeover and len(pend_l) < HOP and time.monotonic() - last_blk > 0.1
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
                    fg_check = now; fg = foreground_exe()
                    if self.live_ver != live_seen:              # settings changed in the UI: apply what can change live
                        live_seen = self.live_ver
                        vol = self.cfg.get('speaker_volume', 0.6)
                        spk_mode = SPEAKER_MODE_ALIAS.get(self.cfg.get('speaker_mode', '原声'), self.cfg.get('speaker_mode', '原声'))
                        spk_eq = self.cfg.get('speaker_eq', '原味')
                        if spk is None and spk_mode != '关闭':
                            spk = RawSpeaker()
                        elif spk_mode == '关闭':
                            spk = None
                        fusion = self.cfg.get('fusion', '融合'); base_rgain = self.cfg.get('rumble_gain', 1.0)
                        tune = prof is not None and self.cfg.get('game_profiles', True)
                        rgain = base_rgain * (prof['rumble_scale'] if tune else 1.0)
                        eng = HapticEngine(prof['grip_profile'] if tune else self.cfg['grip_profile'], self.cfg['trigger_profile'],
                                           self.cfg['grip_gain'], self.cfg['trigger_gain'], self.cfg.get('haptic_style', '游戏'), groove_gains(self.cfg))
                        if self.proc is not None:              # scene changed: which program to listen to
                            game_scene = self.cfg.get('scene', '游戏') == '游戏'
                            self.proc.players = 'foreground' if game_scene else 'loudest'
                            self.proc.exes = [e for e in list(GAME_PROFILES) + [PBZ_EXE, self.cfg.get('foreground_exe', '').strip().lower()] if e] if game_scene else []
                    # 特殊事件 only exists for the adapted games: anywhere else the speaker plays 原声
                    adapted = fg == PBZ_EXE and self.cfg.get('pbz_fx', True)     # 特殊事件: only 影之刃零 is adapted
                    spk_eff = '原声' if (spk_mode == '特殊事件' and not adapted) else spk_mode
                    if spk_mode == '特殊事件' and pad_tap is not None and pad_tap.last_sound:
                        spk_eff = '游戏原生'          # the game drives the pad speaker itself (like on PS5): use exactly that
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
                        gprof = prof['grip_profile'] if tune else self.cfg['grip_profile']
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
                            vision = WWVision(lambda: (hid.l2 / 255, hid.r2 / 255), trace_path=tp, mem=proc_mem_mb, crops_dir=cd, shots_dir=sd, fps=10); vision.start()   # 10 fps: half the screen-capture load (was 20)
                        self.stats['profile'] = prof['name'] if prof else ''
                press = hid.poll_input()
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
                    try: hid.enable_speaker()
                    except Exception: pass
                while len(pend_l) >= HOP:
                    bl, br = pend_l[:HOP], pend_r[:HOP]; pend_l, pend_r = pend_l[HOP:], pend_r[HOP:]
                    produced += HOP
                    if not active:
                        bl = np.zeros(HOP, np.float32); br = bl
                    wl, wr, trig = eng.process(bl, br, press)
                    if native:
                        (rleft, rright), _, color = vpad.feedback()
                        large, small = rleft / 255, rright / 255
                        nl, nr = vpad.wave.pull(HOP)                  # game's own voice-coil waveform
                        rl, rr = rum.step(large, small, rgain)         # legacy rumble, if the game uses it
                        nl = nl * rgain + rl; nr = nr * rgain + rr
                        if fx is not None:                           # event tuning: grade / duck the native layer
                            g = fx.native_gain(HOP); nl = nl * g; nr = nr * g
                        nlevel = float(max(np.max(np.abs(nl)), np.max(np.abs(nr))))
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
                        rl, rr = rum.step(large, small, rgain)
                        wl, wr = fuse(wl, wr, rl, rr, max(large, small), fusion, duck)
                        if color and color != last_color and any(color):
                            try: hid.set_lightbar(*color); last_color = color
                            except Exception: pass
                    if fx is not None:                               # event tuning: synthesized grip effects
                        fl, fr = fx.render(HOP)
                        wl = np.clip(wl + fl, -1, 1).astype(np.float32); wr = np.clip(wr + fr, -1, 1).astype(np.float32)
                    sp = se = None
                    gsp = pad_tap.pull(HOP) if pad_tap is not None else None     # game's own pad-speaker sound
                    if gsp is not None:
                        # the game mixes its pad-speaker cues quietly (on PS5 the console volume lifts them):
                        # slow make-up gain to ~70 % peak, at most 10x; the speaker-volume slider applies on top
                        gpk_now = float(np.max(np.abs(gsp)))
                        g_env = max(gpk_now, g_env * 0.9995, 0.02)
                        gsp = gsp * min(10.0, 0.7 / g_env)
                    if spk is not None:
                        spk.analyse(bl, br)
                        if spk_eff == '原声':
                            sp = spk.render(spk_eq, vol)
                            if gsp is not None and pad_tap.sounding():          # full game sound + its pad-speaker cues
                                sp = np.clip(sp + gsp * vol, -1, 1).astype(np.float32)
                        elif spk_eff == '游戏原生':
                            sp = np.clip(gsp * vol, -1, 1).astype(np.float32)
                        if gsp is not None:
                            g_out_pk = max(g_out_pk, float(np.max(np.abs(gsp))) * vol)
                        ev_on = fx is not None and fx.speaker_on() and spk_eff != '游戏原生'
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
                    out.feed(wl, wr, sp)
                    gpk_l = max(gpk_l, float(np.max(np.abs(wl)))); gpk_r = max(gpk_r, float(np.max(np.abs(wr))))
                if len(pend_l) > 4 * HOP:          # never build up latency
                    pend_l, pend_r = pend_l[-HOP:], pend_r[-HOP:]
                amp = tuple(int(round(t * 8)) for t in trig)
                game_l = game_r = None
                if native:
                    game_l, game_r, _ = vpad.triggers()
                    if trace is not None and (game_l, game_r) != trace_trig:
                        trace_trig = (game_l, game_r)
                        trace.write(f"{int(time.time() * 1000)}\ttrigger\t{game_l.hex() if game_l else '-'}\t{game_r.hex() if game_r else '-'}\n")
                    off = lambda e: e is None or e[0] in (0x00, 0x05)
                    if not (off(game_l) and off(game_r)) or fusion == '仅游戏震动':
                        # the game drives the triggers: pass its effects through untouched
                        ov_l, ov_r = fx.triggers() if fx is not None else (None, None)
                        hid.set_triggers(ov_l or game_l or effect_off(), ov_r or game_r or effect_off())
                        last_trig = (-1, -1); amp = None
                if amp is not None and fx is not None:
                    ov_l, ov_r = fx.triggers()
                    if ov_l or ov_r:                                 # event trigger effect wins for its duration
                        lf = ov_l or (effect_vibration(amp[0], 30 + 25 * trig[0]) if amp[0] else effect_off())
                        rf = ov_r or (effect_vibration(amp[1], 30 + 25 * trig[1]) if amp[1] else effect_off())
                        hid.set_triggers(lf, rf); last_trig = (-1, -1); amp = None
                if amp is not None and amp != last_trig:
                    lf = effect_vibration(amp[0], 30 + 25 * trig[0]) if amp[0] else effect_off()
                    rf = effect_vibration(amp[1], 30 + 25 * trig[1]) if amp[1] else effect_off()
                    if hid.set_triggers(lf, rf):
                        last_trig = amp
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
