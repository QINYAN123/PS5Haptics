"""Id of the current default render endpoint, straight from Windows Core Audio (IMMDeviceEnumerator).

PortAudio caches the device list (and the default) until every instance is terminated, so it never
notices the user switching the default output (laptop speakers -> TV). Polling this id lets the
runner reopen the loopback on the new device.
"""
import ctypes
from ctypes import POINTER, c_void_p, c_wchar_p, c_int
from ctypes import wintypes as W
import comtypes
from comtypes import GUID, IUnknown, COMMETHOD, HRESULT

CLSID_MMDeviceEnumerator = GUID('{BCDE0395-E52F-467C-8E3D-C4579291692E}')


class IMMDevice(IUnknown):
    _iid_ = GUID('{D666063F-1587-4E43-81F1-B948E807363F}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'Activate', (['in'], c_void_p), (['in'], W.DWORD), (['in'], c_void_p), (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'OpenPropertyStore', (['in'], W.DWORD), (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'GetId', (['out'], POINTER(c_wchar_p), 'ppstrId')),
        COMMETHOD([], HRESULT, 'GetState', (['out'], POINTER(W.DWORD), 'pdwState')),
    ]


class IMMDeviceEnumerator(IUnknown):
    _iid_ = GUID('{A95664D2-9614-4F35-A746-DE8DB63617E6}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'EnumAudioEndpoints', (['in'], c_int), (['in'], W.DWORD), (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'GetDefaultAudioEndpoint', (['in'], c_int, 'dataFlow'), (['in'], c_int, 'role'),
                  (['out'], POINTER(POINTER(IMMDevice)), 'ppEndpoint')),
    ]


class DefaultOutputWatcher:
    """Call in the thread that uses it (initialises COM for that thread)."""

    def __init__(self):
        comtypes.CoInitialize()
        self.en = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_ALL)

    def current(self):
        try:
            dev = self.en.GetDefaultAudioEndpoint(0, 1)        # eRender, eMultimedia
            return dev.GetId()
        except Exception:
            return None

    def close(self):
        self.en = None
        try: comtypes.CoUninitialize()
        except Exception: pass


if __name__ == '__main__':
    print(DefaultOutputWatcher().current())
