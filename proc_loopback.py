"""Per-process audio capture (Windows 10 2004+ "process loopback").

Captures only what one process tree renders, taken from that process's audio session - meant for
setups where the endpoint loopback is silent, e.g. Dolby Atmos for home theater (the game's audio
goes to the Atmos encoder, not the shared mix). Read-only: nothing is injected into the game.

ProcessLoopback(pid).read() -> float32 interleaved stereo at 48 kHz (may be empty).
"""
import ctypes
import threading
import time
from ctypes import POINTER, Structure, byref, c_int, c_ubyte, c_ushort, c_void_p, c_uint32, c_uint64, c_longlong, c_wchar_p
from ctypes import wintypes as W

import numpy as np
import comtypes
from comtypes import GUID, IUnknown, COMMETHOD, HRESULT, COMObject

RATE = 48000
CH = 2
VT_BLOB = 65
ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
LOOPBACK_MODE_INCLUDE_TREE = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2


class LOOPBACK_PARAMS(Structure):
    _fields_ = [('TargetProcessId', W.DWORD), ('ProcessLoopbackMode', c_int)]


class ACTIVATION_PARAMS(Structure):
    _fields_ = [('ActivationType', c_int), ('ProcessLoopbackParams', LOOPBACK_PARAMS)]


class BLOB(Structure):
    _fields_ = [('cbSize', W.ULONG), ('pBlobData', c_void_p)]


class PROPVARIANT(Structure):
    _fields_ = [('vt', c_ushort), ('r1', c_ushort), ('r2', c_ushort), ('r3', c_ushort), ('blob', BLOB)]


class WAVEFORMATEX(Structure):
    _fields_ = [('wFormatTag', W.WORD), ('nChannels', W.WORD), ('nSamplesPerSec', W.DWORD), ('nAvgBytesPerSec', W.DWORD),
                ('nBlockAlign', W.WORD), ('wBitsPerSample', W.WORD), ('cbSize', W.WORD)]


class WAVEFORMATEXTENSIBLE(Structure):
    _fields_ = [('Format', WAVEFORMATEX), ('wValidBitsPerSample', W.WORD), ('dwChannelMask', W.DWORD), ('SubFormat', GUID)]


KSDATAFORMAT_SUBTYPE_PCM = GUID('{00000001-0000-0010-8000-00aa00389b71}')
CHANNEL_MASKS = {2: 0x3, 6: 0x3F, 8: 0x63F}      # stereo, 5.1, 7.1 (FL FR FC LFE BL BR SL SR)


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID('{C8ADBD64-E71E-48a0-A4DE-185C395CD317}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'GetBuffer', (['out'], POINTER(POINTER(c_ubyte))), (['out'], POINTER(c_uint32)),
                  (['out'], POINTER(W.DWORD)), (['out'], POINTER(c_uint64)), (['out'], POINTER(c_uint64))),
        COMMETHOD([], HRESULT, 'ReleaseBuffer', (['in'], c_uint32)),
        COMMETHOD([], HRESULT, 'GetNextPacketSize', (['out'], POINTER(c_uint32))),
    ]


class IAudioClient(IUnknown):
    _iid_ = GUID('{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'Initialize', (['in'], c_int), (['in'], W.DWORD), (['in'], c_longlong), (['in'], c_longlong),
                  (['in'], POINTER(WAVEFORMATEX)), (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'GetBufferSize', (['out'], POINTER(c_uint32))),
        COMMETHOD([], HRESULT, 'GetStreamLatency', (['out'], POINTER(c_longlong))),
        COMMETHOD([], HRESULT, 'GetCurrentPadding', (['out'], POINTER(c_uint32))),
        COMMETHOD([], HRESULT, 'IsFormatSupported', (['in'], c_int), (['in'], c_void_p), (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'GetMixFormat', (['out'], POINTER(c_void_p))),
        COMMETHOD([], HRESULT, 'GetDevicePeriod', (['out'], POINTER(c_longlong)), (['out'], POINTER(c_longlong))),
        COMMETHOD([], HRESULT, 'Start'),
        COMMETHOD([], HRESULT, 'Stop'),
        COMMETHOD([], HRESULT, 'Reset'),
        COMMETHOD([], HRESULT, 'SetEventHandle', (['in'], W.HANDLE)),
        COMMETHOD([], HRESULT, 'GetService', (['in'], POINTER(GUID)), (['out'], POINTER(POINTER(IUnknown)))),
    ]


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID('{72A22D78-CDE4-431D-B8CC-843A71199B6D}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'GetActivateResult', (['out'], POINTER(HRESULT)), (['out'], POINTER(POINTER(IUnknown)))),
    ]


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID('{41D949AB-9862-444A-80F6-C261334DA5EB}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'ActivateCompleted', (['in'], POINTER(IActivateAudioInterfaceAsyncOperation))),
    ]


class IAgileObject(IUnknown):
    _iid_ = GUID('{94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90}')
    _methods_ = []


class _Handler(COMObject):
    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

    def __init__(self):
        super().__init__()
        self.done = threading.Event(); self.op = None

    def ActivateCompleted(self, op):
        self.op = op; self.done.set()
        return 0


def _activate(pid):
    params = ACTIVATION_PARAMS(ACTIVATION_TYPE_PROCESS_LOOPBACK, LOOPBACK_PARAMS(pid, LOOPBACK_MODE_INCLUDE_TREE))
    pv = PROPVARIANT(); pv.vt = VT_BLOB
    pv.blob.cbSize = ctypes.sizeof(params); pv.blob.pBlobData = ctypes.cast(ctypes.pointer(params), c_void_p)
    fn = ctypes.windll.Mmdevapi.ActivateAudioInterfaceAsync
    fn.argtypes = [c_wchar_p, POINTER(GUID), POINTER(PROPVARIANT), c_void_p, POINTER(c_void_p)]
    fn.restype = HRESULT
    h = _Handler()
    hp = h.QueryInterface(IActivateAudioInterfaceCompletionHandler)
    op = c_void_p()
    fn('VAD\\Process_Loopback', byref(IAudioClient._iid_), byref(pv), ctypes.cast(hp, c_void_p), byref(op))
    if not h.done.wait(5):
        raise TimeoutError('process loopback activation timed out')
    try:
        hr, unk = h.op.GetActivateResult()
        if hr != 0:
            raise OSError(f'process loopback activation failed 0x{hr & 0xFFFFFFFF:08X}')
        client = unk.QueryInterface(IAudioClient); del unk
    finally:
        # release everything activation handed us now, in this thread. The handler is a reference
        # cycle; if it kept the operation (which holds the audio client) the garbage collector would
        # free it later on whatever thread happens to run - that is how the engine crashed.
        h.op = None
        if op.value:
            _release_raw(op.value)
    _HANDLERS.append(h)              # completion handlers are tiny; keep them so GC never frees COM state
    del _HANDLERS[:-64]
    return client, (hp, params, pv)


_HANDLERS = []
_RELEASE = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)


def _release_raw(addr):
    vt = ctypes.cast(c_void_p.from_address(addr).value, POINTER(c_void_p))
    _RELEASE(vt[2])(addr)


class ProcessLoopback:
    """Use from one thread (COM initialised multithreaded here)."""

    def __init__(self, pid, ch=CH):
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass                                # thread already has COM (e.g. STA from comtypes import)
        self.client, self._keep = _activate(pid)
        self.ch = ch
        if ch == 2:
            fmt = WAVEFORMATEX(1, ch, RATE, RATE * ch * 2, ch * 2, 16, 0)     # 16-bit PCM, converted for us
        else:                                                               # multichannel needs EXTENSIBLE
            fmt = WAVEFORMATEXTENSIBLE(WAVEFORMATEX(0xFFFE, ch, RATE, RATE * ch * 2, ch * 2, 16, 22), 16,
                                       CHANNEL_MASKS[ch], KSDATAFORMAT_SUBTYPE_PCM)
        self.client.Initialize(0, AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK |
                               AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
                               1000000, 0, ctypes.cast(ctypes.pointer(fmt), POINTER(WAVEFORMATEX)), None)              # 100 ms buffer
        self.event = ctypes.windll.kernel32.CreateEventW(None, False, False, None)
        self.client.SetEventHandle(self.event)
        self.capture = self.client.GetService(byref(IAudioCaptureClient._iid_)).QueryInterface(IAudioCaptureClient)
        self.client.Start()

    def read(self, timeout_ms=20):
        ctypes.windll.kernel32.WaitForSingleObject(self.event, timeout_ms)
        out = []
        while self.capture.GetNextPacketSize():
            data, n, flags, _, _ = self.capture.GetBuffer()
            if n:
                if flags & AUDCLNT_BUFFERFLAGS_SILENT:
                    out.append(np.zeros(n * self.ch, np.float32))
                else:
                    a = np.ctypeslib.as_array(data, shape=(n * self.ch * 2,)).view(np.int16)
                    out.append(a.astype(np.float32) / 32768.0)
            self.capture.ReleaseBuffer(n)
        return np.concatenate(out) if out else np.zeros(0, np.float32)

    def close(self):
        if self.client is None:
            return
        try: self.client.Stop()
        except Exception: pass
        self.capture = None; self.client = None; self._keep = None     # released here, in the owning thread
        try: ctypes.windll.kernel32.CloseHandle(self.event)            # the event only after the client is gone
        except Exception: pass


class _PE32(Structure):
    _fields_ = [('dwSize', W.DWORD), ('cntUsage', W.DWORD), ('th32ProcessID', W.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                ('th32ModuleID', W.DWORD), ('cntThreads', W.DWORD), ('th32ParentProcessID', W.DWORD),
                ('pcPriClassBase', ctypes.c_long), ('dwFlags', W.DWORD), ('szExeFile', ctypes.c_wchar * 260)]


class _PMC(Structure):
    _fields_ = [('cb', W.DWORD), ('PageFaultCount', W.DWORD), ('PeakWorkingSetSize', ctypes.c_size_t),
                ('WorkingSetSize', ctypes.c_size_t), ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPagedPoolUsage', ctypes.c_size_t), ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                ('QuotaNonPagedPoolUsage', ctypes.c_size_t), ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]


def _process_ws_bytes(pid):
    """Best-effort WorkingSetSize in bytes via limited query access, or None if unavailable."""
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = W.HANDLE
    h = k.OpenProcess(0x1000, False, int(pid))
    if not h:
        return None
    try:
        c = _PMC(); c.cb = ctypes.sizeof(c)
        return int(c.WorkingSetSize) if k.K32GetProcessMemoryInfo(h, byref(c), c.cb) else None
    finally:
        k.CloseHandle(W.HANDLE(h))


def _pid_alive(pid):
    """Fast O(1) liveness check without taking a system-wide Toolhelp snapshot."""
    if not pid:
        return False
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = W.HANDLE
    h = k.OpenProcess(0x1000, False, int(pid))
    if not h:
        return False
    try:
        code = W.DWORD()
        return bool(k.GetExitCodeProcess(W.HANDLE(h), byref(code)) and code.value == 259)
    finally:
        k.CloseHandle(W.HANDLE(h))


def _select_best_pid(matches, mem_fn=_process_ws_bytes, prefer_pid=None):
    """Pick the real game process when a launcher stub shares the exe name (e.g. steam\\PBZ.exe -> platform\\steam\\PBZ.exe)."""
    if len(matches) == 1:
        return matches[0][0], matches[0][1]
    match_pids = {m[0] for m in matches}
    mem_map = {m[0]: mem_fn(m[0]) for m in matches}
    known_ws = [v for v in mem_map.values() if v is not None]
    max_ws = max(known_ws) if known_ws else 0

    def rank(item):
        pid, _exe, ppid = item
        ws = mem_map[pid]
        is_child = 1 if ppid in match_pids else 0
        eff_ws = (max_ws + 1 if is_child else 0) if ws is None else ws
        keep_pref = 1 if (prefer_pid is not None and pid == prefer_pid and eff_ws >= max_ws * 0.5) else 0
        return (keep_pref, eff_ws, is_child, pid)

    best = max(matches, key=rank)
    return best[0], best[1]


def find_pid(*exe_names, prefer_pid=None):
    """Best running process whose exe name matches (case-insensitive), via one Toolhelp snapshot."""
    want = [n.lower() for n in exe_names if n]
    if not want:
        return None
    want_set = set(want)
    by_exe = {}
    k = ctypes.windll.kernel32
    k.CreateToolhelp32Snapshot.restype = W.HANDLE
    snap = k.CreateToolhelp32Snapshot(0x2, 0)
    try:
        e = _PE32(); e.dwSize = ctypes.sizeof(e)
        ok = k.Process32FirstW(snap, byref(e))
        while ok:
            low = e.szExeFile.lower()
            if low in want_set:
                by_exe.setdefault(low, []).append((int(e.th32ProcessID), e.szExeFile, int(e.th32ParentProcessID)))
            ok = k.Process32NextW(snap, byref(e))
    finally:
        k.CloseHandle(snap)
    for name in want:
        if name in by_exe:
            return _select_best_pid(by_exe[name], prefer_pid=prefer_pid)
    return None


def _pid_has_audio(pid):
    """Best-effort check whether `pid` has a currently audible session on default output (no snapshot)."""
    if not pid:
        return False
    try:
        from audio_sessions import (CLSID_MMDeviceEnumerator, IMMDeviceEnumerator,
                                    IAudioSessionManager2, IAudioSessionControl2, IAudioMeterInformation)
        en = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_ALL)
        dev = en.GetDefaultAudioEndpoint(0, 1)
        mgr = dev.Activate(byref(IAudioSessionManager2._iid_), comtypes.CLSCTX_ALL, None).QueryInterface(IAudioSessionManager2)
        sessions = mgr.GetSessionEnumerator()
        for i in range(sessions.GetCount()):
            try:
                unk = sessions.GetSession(i)
                spid = unk.QueryInterface(IAudioSessionControl2).GetProcessId()
                if spid == pid and unk.QueryInterface(IAudioMeterInformation).GetPeakValue() > 1e-4:
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return None


def find_pids(*exe_names):
    """Set of matching lowercase exe names currently running, queried in one Toolhelp snapshot."""
    want = {n.lower() for n in exe_names if n}
    found = set()
    if not want:
        return found
    k = ctypes.windll.kernel32
    k.CreateToolhelp32Snapshot.restype = W.HANDLE
    snap = k.CreateToolhelp32Snapshot(0x2, 0)
    try:
        e = _PE32(); e.dwSize = ctypes.sizeof(e)
        ok = k.Process32FirstW(snap, byref(e))
        while ok:
            low = e.szExeFile.lower()
            if low in want:
                found.add(low)
                if len(found) == len(want):
                    break
            ok = k.Process32NextW(snap, byref(e))
    finally:
        k.CloseHandle(snap)
    return found


class ProcSource(threading.Thread):
    """Read one process tree and tag packets so a route change cannot play stale audio."""

    def __init__(self, q, log, stop_ev, foreground_game):
        super().__init__(daemon=True)
        self.q, self.log, self.stop_ev, self.foreground_game = q, log, stop_ev, foreground_game
        self.request = ('mix', None)
        self.key = None
        self.name = ''
        self.reattach = 0
        self.max_gap = 0.0
        self.last_sound = 0.0

    def sounding(self):
        return self.key is not None and self.key[0] == self.request and time.monotonic() - self.last_sound < 1.0

    def select(self, wanted, current, attached):
        mode, target = wanted
        if mode == 'game':
            foreground = self.foreground_game(target)
            if foreground:
                if (current and current[0] != foreground[0]
                        and current[1].lower() == foreground[1].lower() and _pid_alive(current[0])):
                    cur_ws = _process_ws_bytes(current[0]) or 0
                    fg_ws = _process_ws_bytes(foreground[0]) or 0
                    if cur_ws > fg_ws * 2:
                        return current
                return foreground
            if current and _pid_alive(current[0]):
                return current
            return find_pid(current[1]) if current else None
        if mode == 'pinned':
            if not target:
                return None
            if current and current[1].lower() == target.lower() and _pid_alive(current[0]):
                # Periodically ensure a launcher stub didn't hand off to a larger child process.
                now = time.monotonic()
                if now - getattr(self, '_pin_rescan', 0.0) < 5.0:
                    return current
                self._pin_rescan = now
            return find_pid(target, prefer_pid=current[0] if current else None)
        if mode == 'auto':
            if current and attached and time.monotonic() - self.last_sound < 5.0:
                return current
            try:
                import audio_sessions
                return next(((pid, exe) for peak, pid, exe in audio_sessions.playing() if peak > 0.003), None)
            except Exception:
                return None
        return None

    def run(self):
        import queue as _q
        initialized = False
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            initialized = True
        except OSError:
            pass
        stream = None; current = None; request = self.request; retry_at = 0.0; check_at = 0.0; last_packet = time.monotonic()
        failed = set()
        try:
            while not self.stop_ev.is_set():
                now = time.monotonic()
                wanted = self.request
                if wanted != request or now >= check_at:
                    found = self.select(wanted, current if wanted == request else None, stream is not None)
                    check_at = now + (0.5 if wanted[0] == 'game' and found is None else 1.0)
                    if found != current or wanted != request:
                        self.key = None; self.last_sound = 0.0; self.name = ''
                        if stream is not None:
                            stream.close(); stream = None
                        current = found; request = wanted; retry_at = 0.0
                if stream is None and current is not None and now >= retry_at:
                    pid, exe = current
                    retry_at = now + 1.5
                    try:
                        stream = ProcessLoopback(pid)
                        last_packet = time.monotonic()
                        self.name = exe
                        self.key = (request, pid)
                        if self.key != getattr(self, 'logged', None):     # re-attaches after alt-tab stay quiet
                            self.logged = self.key
                            self.log(f'按进程截音：{exe}（杜比全景声 / 空间音效开着也能截到）')
                        failed.discard(current)
                    except Exception as exc:           # retried every 1.5 s; logged once per target
                        if current not in failed:
                            detail = '按进程截音失败，将保持静音并重试：' if request[0] in ('game', 'pinned') else '按进程截音失败，暂用系统混音，会自动重试：'
                            self.log(detail + str(exc))
                            failed.add(current)
                if stream is None:
                    self.stop_ev.wait(0.15); continue
                try:
                    samples = stream.read(20)
                except Exception as exc:                       # game exited / session gone
                    self.log('游戏进程音频中断：' + str(exc))
                    self.key = None; self.last_sound = 0.0; self.name = ''
                    stream.close(); stream = None; current = None
                    continue
                now = time.monotonic()
                if len(samples):
                    self.max_gap = max(self.max_gap, now - last_packet); last_packet = now
                    if float(np.abs(samples).max()) > 1e-4:
                        self.last_sound = now
                    if self.request == request and (request[0] != 'auto' or self.sounding()):
                        try: self.q.put_nowait((self.key, self.name.lower(), samples))
                        except _q.Full: pass
                elif now - last_packet > 0.8:
                    # Wait longer when the game menu is genuinely silent, but reattach promptly if the game IS playing sound
                    # (e.g. stream recreated after alt-tab or mid-combat Wwise reset) without dropping self.key/route.
                    has_audio = _pid_has_audio(current[0]) if current else None
                    if has_audio is False and now - last_packet < 6.0:
                        continue
                    old_stream, stream = stream, None
                    old_stream.close()
                    self.reattach += 1
                    try:
                        stream = ProcessLoopback(current[0])
                        last_packet = time.monotonic()
                    except Exception as exc:
                        self.key = None; self.last_sound = 0.0; self.name = ''
                        if current not in failed:
                            detail = '按进程截音失败，将保持静音并重试：' if request[0] in ('game', 'pinned') else '按进程截音失败，暂用系统混音，会自动重试：'
                            self.log(detail + str(exc))
                            failed.add(current)
                        current = current if (current and _pid_alive(current[0])) else None
                        retry_at = time.monotonic() + 1.5
                        check_at = 0.0
        finally:
            self.key = None
            if stream is not None:
                stream.close()
            if initialized:
                comtypes.CoUninitialize()


if __name__ == '__main__':
    import sys
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else find_pid('Client-Win64-Shipping.exe')[0]
    pl = ProcessLoopback(pid); t0 = time.time(); xs = []
    while time.time() - t0 < float(sys.argv[2] if len(sys.argv) > 2 else 3):
        xs.append(pl.read())
    x = np.concatenate(xs); pl.close()
    print('pid', pid, 'samples', len(x), 'peak %.4f' % (float(np.abs(x).max()) if len(x) else 0))
