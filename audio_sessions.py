"""Which programs are playing sound right now (Windows audio sessions on the default output).

Used by the 自动 audio source: per-process capture works with Dolby Atmos / spatial sound, but it
needs a process id. For films and music the sound comes from a player / browser / music app, so we
pick the process whose session is currently the loudest.
"""
import ctypes
from ctypes import POINTER, byref, c_float, c_int, c_void_p, c_wchar_p
from ctypes import wintypes as W
import comtypes
from comtypes import GUID, IUnknown, COMMETHOD, HRESULT

CLSID_MMDeviceEnumerator = GUID('{BCDE0395-E52F-467C-8E3D-C4579291692E}')


class IAudioMeterInformation(IUnknown):
    _iid_ = GUID('{C02216F6-8C67-4B5B-9D00-D008E73E0064}')
    _methods_ = [COMMETHOD([], HRESULT, 'GetPeakValue', (['out'], POINTER(c_float)))]


class IAudioSessionControl2(IUnknown):
    _iid_ = GUID('{bfb7ff88-7239-4fc9-8fa2-07c950be9c6d}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'GetState', (['out'], POINTER(c_int))),
        COMMETHOD([], HRESULT, 'GetDisplayName', (['out'], POINTER(c_wchar_p))),
        COMMETHOD([], HRESULT, 'SetDisplayName', (['in'], c_wchar_p), (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'GetIconPath', (['out'], POINTER(c_wchar_p))),
        COMMETHOD([], HRESULT, 'SetIconPath', (['in'], c_wchar_p), (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'GetGroupingParam', (['out'], POINTER(GUID))),
        COMMETHOD([], HRESULT, 'SetGroupingParam', (['in'], POINTER(GUID)), (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'RegisterAudioSessionNotification', (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'UnregisterAudioSessionNotification', (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'GetSessionIdentifier', (['out'], POINTER(c_wchar_p))),
        COMMETHOD([], HRESULT, 'GetSessionInstanceIdentifier', (['out'], POINTER(c_wchar_p))),
        COMMETHOD([], HRESULT, 'GetProcessId', (['out'], POINTER(W.DWORD))),
        COMMETHOD([], HRESULT, 'IsSystemSoundsSession'),
        COMMETHOD([], HRESULT, 'SetDuckingPreference', (['in'], W.BOOL)),
    ]


class IAudioSessionEnumerator(IUnknown):
    _iid_ = GUID('{E2F5BB11-0570-40CA-ACDD-3AA01277DEE8}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'GetCount', (['out'], POINTER(c_int))),
        COMMETHOD([], HRESULT, 'GetSession', (['in'], c_int), (['out'], POINTER(POINTER(IUnknown)))),
    ]


class IAudioSessionManager2(IUnknown):
    _iid_ = GUID('{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'GetAudioSessionControl', (['in'], c_void_p), (['in'], W.DWORD), (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'GetSimpleAudioVolume', (['in'], c_void_p), (['in'], W.DWORD), (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'GetSessionEnumerator', (['out'], POINTER(POINTER(IAudioSessionEnumerator)))),
    ]


class IMMDevice(IUnknown):
    _iid_ = GUID('{D666063F-1587-4E43-81F1-B948E807363F}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'Activate', (['in'], POINTER(GUID)), (['in'], W.DWORD), (['in'], c_void_p),
                  (['out'], POINTER(POINTER(IUnknown)))),
    ]


class IMMDeviceEnumerator(IUnknown):
    _iid_ = GUID('{A95664D2-9614-4F35-A746-DE8DB63617E6}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'EnumAudioEndpoints', (['in'], c_int), (['in'], W.DWORD), (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'GetDefaultAudioEndpoint', (['in'], c_int), (['in'], c_int),
                  (['out'], POINTER(POINTER(IMMDevice)))),
    ]


def _proc_names():
    from proc_loopback import _PE32
    k = ctypes.windll.kernel32; k.CreateToolhelp32Snapshot.restype = W.HANDLE
    snap = k.CreateToolhelp32Snapshot(0x2, 0); names = {}
    try:
        e = _PE32(); e.dwSize = ctypes.sizeof(e); ok = k.Process32FirstW(snap, byref(e))
        while ok:
            names[e.th32ProcessID] = e.szExeFile; ok = k.Process32NextW(snap, byref(e))
    finally:
        k.CloseHandle(snap)
    return names


SKIP = {'ps5haptics.exe', 'viiperhost.exe', 'python.exe', 'pythonw.exe', 'audiodg.exe'}


def playing():
    """[(peak 0..1, pid, exe)] of programs with an audio session on the default output, loudest first.
    Call from a thread with COM initialised."""
    en = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_ALL)
    dev = en.GetDefaultAudioEndpoint(0, 1)
    unk = dev.Activate(byref(IAudioSessionManager2._iid_), comtypes.CLSCTX_ALL, None)
    mgr = unk.QueryInterface(IAudioSessionManager2)
    sessions = mgr.GetSessionEnumerator()
    names = _proc_names(); out = []
    for i in range(sessions.GetCount()):
        try:
            unk = sessions.GetSession(i)
            ctl = unk.QueryInterface(IAudioSessionControl2)
            pid = ctl.GetProcessId()
            if not pid:
                continue                                      # system sounds
            peak = unk.QueryInterface(IAudioMeterInformation).GetPeakValue()
            exe = names.get(pid, '?')
            if exe.lower() in SKIP:
                continue
            out.append((float(peak), int(pid), exe))
        except Exception:
            continue
    out.sort(reverse=True)
    return out


if __name__ == '__main__':
    import threading
    def run():
        comtypes.CoInitialize()
        for p in playing():
            print('%.3f  %6d  %s' % p)
    t = threading.Thread(target=run); t.start(); t.join()
