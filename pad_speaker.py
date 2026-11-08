"""The game's OWN controller-speaker sound (原生手柄扬声器).

Games with native DualSense support (影之刃零, Sony ports...) send authored sounds - parry clangs,
bow strings, radio voices - to the pad's speaker channels (ch1/ch2 of its 4-channel USB audio
endpoint), exactly like on PS5. In 原生透传 the game talks to our VIRTUAL DualSense, so that sound
lands on the virtual pad's audio endpoint and would be lost. We loopback-capture that endpoint
(plain WASAPI shared loopback; read-only, nothing touches the game) and hand ch1/ch2 to the
runner, which plays them on the real pad's speaker.

Pure COM (comtypes) so it sees endpoints that appear after the app started - PortAudio only
enumerates devices once.
"""
import collections
import ctypes
import threading
import time
from ctypes import POINTER, Structure, byref, c_ushort, c_void_p, c_uint32, c_wchar_p
from ctypes import wintypes as W

import numpy as np
import comtypes
from comtypes import GUID, IUnknown, COMMETHOD, HRESULT

from proc_loopback import (IAudioClient, IAudioCaptureClient, WAVEFORMATEX,
                           AUDCLNT_STREAMFLAGS_LOOPBACK, AUDCLNT_BUFFERFLAGS_SILENT)

CLSID_MMDeviceEnumerator = GUID('{BCDE0395-E52F-467C-8E3D-C4579291692E}')
PKEY_FRIENDLY = (GUID('{a45c254e-df1c-4efd-8020-67d146a850e0}'), 14)
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
KSDATAFORMAT_SUBTYPE_IEEE_FLOAT = GUID('{00000003-0000-0010-8000-00aa00389b71}')


class PROPERTYKEY(Structure):
    _fields_ = [('fmtid', GUID), ('pid', W.DWORD)]


class PROPVARIANT(Structure):
    _fields_ = [('vt', c_ushort), ('r1', c_ushort), ('r2', c_ushort), ('r3', c_ushort), ('val', c_void_p), ('pad', c_void_p)]


class IPropertyStore(IUnknown):
    _iid_ = GUID('{886d8eeb-8cf2-4446-8d02-cdba1dbdcf99}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'GetCount', (['out'], POINTER(W.DWORD))),
        COMMETHOD([], HRESULT, 'GetAt', (['in'], W.DWORD), (['out'], POINTER(PROPERTYKEY))),
        COMMETHOD([], HRESULT, 'GetValue', (['in'], POINTER(PROPERTYKEY)), (['out'], POINTER(PROPVARIANT))),
    ]


class IMMDevice(IUnknown):
    _iid_ = GUID('{D666063F-1587-4E43-81F1-B948E807363F}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'Activate', (['in'], POINTER(GUID)), (['in'], W.DWORD), (['in'], c_void_p),
                  (['out'], POINTER(POINTER(IUnknown)))),
        COMMETHOD([], HRESULT, 'OpenPropertyStore', (['in'], W.DWORD), (['out'], POINTER(POINTER(IPropertyStore)))),
        COMMETHOD([], HRESULT, 'GetId', (['out'], POINTER(c_wchar_p))),
        COMMETHOD([], HRESULT, 'GetState', (['out'], POINTER(W.DWORD))),
    ]


class IMMDeviceCollection(IUnknown):
    _iid_ = GUID('{0BD7A1BE-7A1A-44DB-8397-CC5392387B5E}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'GetCount', (['out'], POINTER(c_uint32))),
        COMMETHOD([], HRESULT, 'Item', (['in'], c_uint32), (['out'], POINTER(POINTER(IMMDevice)))),
    ]


class IMMDeviceEnumerator(IUnknown):
    _iid_ = GUID('{A95664D2-9614-4F35-A746-DE8DB63617E6}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'EnumAudioEndpoints', (['in'], ctypes.c_int), (['in'], W.DWORD),
                  (['out'], POINTER(POINTER(IMMDeviceCollection)))),
    ]


def _friendly(dev):
    ps = dev.OpenPropertyStore(0)
    key = PROPERTYKEY(*PKEY_FRIENDLY)
    pv = ps.GetValue(byref(key))
    name = ctypes.wstring_at(pv.val) if pv.vt == 31 and pv.val else ''     # VT_LPWSTR
    ctypes.windll.ole32.PropVariantClear(byref(pv))
    return name


def render_endpoints():
    """[(friendly name, IMMDevice)] of active render endpoints (call with COM initialised)."""
    en = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_ALL)
    col = en.EnumAudioEndpoints(0, 1)                 # eRender, DEVICE_STATE_ACTIVE
    out = []
    for i in range(col.GetCount()):
        d = col.Item(i)
        try:
            out.append((_friendly(d), d))
        except Exception:
            pass
    return out


class EndpointLoopback:
    """Shared-mode loopback of one render endpoint, in its own mix format (float32)."""

    def __init__(self, dev):
        unk = dev.Activate(byref(IAudioClient._iid_), comtypes.CLSCTX_ALL, None)
        self.client = unk.QueryInterface(IAudioClient); del unk
        pfmt = self.client.GetMixFormat()                          # address of a CoTaskMem WAVEFORMAT(EXTENSIBLE)
        try:
            wf = WAVEFORMATEX.from_address(pfmt)
            self.ch, self.rate, self.bits = wf.nChannels, wf.nSamplesPerSec, wf.wBitsPerSample
            if wf.wFormatTag == WAVE_FORMAT_EXTENSIBLE:
                # packed layout: 18-byte WAVEFORMATEX, 2 + 4 bytes, then SubFormat at offset 24
                self.float = GUID.from_address(pfmt + 24) == KSDATAFORMAT_SUBTYPE_IEEE_FLOAT
            else:
                self.float = wf.wFormatTag == 3
            self.client.Initialize(0, AUDCLNT_STREAMFLAGS_LOOPBACK, 400000, 0, ctypes.cast(pfmt, POINTER(WAVEFORMATEX)), None)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(c_void_p(pfmt))
        self.capture = self.client.GetService(byref(IAudioCaptureClient._iid_)).QueryInterface(IAudioCaptureClient)
        self.client.Start()

    def read(self):
        """All frames available now -> float32 array (n, ch)."""
        out = []
        while self.capture.GetNextPacketSize():
            data, n, flags, _, _ = self.capture.GetBuffer()
            if n:
                if flags & AUDCLNT_BUFFERFLAGS_SILENT:
                    out.append(np.zeros((n, self.ch), np.float32))
                elif self.float and self.bits == 32:
                    out.append(np.ctypeslib.as_array(ctypes.cast(data, POINTER(ctypes.c_float)), shape=(n * self.ch,)).reshape(n, self.ch).copy())
                elif self.bits == 16:
                    a = np.ctypeslib.as_array(ctypes.cast(data, POINTER(ctypes.c_int16)), shape=(n * self.ch,))
                    out.append((a.astype(np.float32) / 32768.0).reshape(n, self.ch))
                else:
                    out.append(np.zeros((n, self.ch), np.float32))
            self.capture.ReleaseBuffer(n)
        return np.concatenate(out) if out else np.zeros((0, self.ch), np.float32)

    def close(self):
        try: self.client.Stop()
        except Exception: pass
        self.capture = None; self.client = None


class PadSpeakerTap(threading.Thread):
    """Follows the VIRTUAL DualSense's audio endpoint (any 'DualSense' render endpoint except the
    real pad's) and keeps its speaker channels (ch1/ch2 -> mono) in a small jitter buffer."""

    def __init__(self, real_name, log, stop_ev):
        super().__init__(daemon=True)
        self.real_name = real_name; self.log = log; self.stop_ev = stop_ev
        self.buf = collections.deque(); self.lock = threading.Lock(); self.n = 0
        self.source = ''; self.peak = 0.0; self.last_sound = 0.0; self.error = None

    def sounding(self, within=1.0):
        return time.monotonic() - self.last_sound < within

    def pull(self, n):
        """n samples at 48 kHz (zeros while nothing is queued)."""
        with self.lock:
            if self.n > 48000 * 0.12:                       # keep latency low: drop old audio
                while self.n > 48000 * 0.04 and self.buf:
                    self.n -= len(self.buf.popleft())
            if self.n < n:
                return np.zeros(n, np.float32)
            parts = []; need = n
            while need:
                b = self.buf[0]
                if len(b) <= need:
                    parts.append(b); self.buf.popleft(); need -= len(b)
                else:
                    parts.append(b[:need]); self.buf[0] = b[need:]; need = 0
            self.n -= n
        return np.concatenate(parts)

    def run(self):
        try: comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError: pass
        lb = None; check = 0.0; said = False
        while not self.stop_ev.is_set():
            if lb is None:
                if time.monotonic() - check < 2.0:
                    self.stop_ev.wait(0.2); continue
                check = time.monotonic()
                try:
                    for name, dev in render_endpoints():
                        if 'DualSense' in name and name != self.real_name:
                            lb = EndpointLoopback(dev); self.source = name
                            if lb.ch < 2 or lb.rate != 48000:
                                lb.close(); lb = None; continue
                            if not said:
                                said = True; self.log(f'已接上虚拟手柄的扬声器声道：游戏原生的手柄扬声器音效会转到真手柄（{name}）')
                            break
                except Exception as exc:
                    self.error = str(exc); lb = None
                continue
            try:
                x = lb.read()
            except Exception:                              # virtual pad went away
                lb.close(); lb = None; self.source = ''; continue
            if len(x):
                mono = (x[:, 0] + x[:, 1]) * 0.5
                pk = float(np.max(np.abs(mono)))
                self.peak = max(self.peak * 0.9, pk)
                if pk > 0.003:
                    self.last_sound = time.monotonic()
                with self.lock:
                    self.buf.append(mono.astype(np.float32)); self.n += len(mono)
            else:
                self.stop_ev.wait(0.005)
        if lb is not None:
            lb.close()
