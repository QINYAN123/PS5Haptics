"""Id of the current default render endpoint, straight from Windows Core Audio (IMMDeviceEnumerator).

PortAudio caches the device list (and the default) until every instance is terminated, so it never
notices the user switching the default output (laptop speakers -> TV). Polling this id lets the
runner reopen the loopback on the new device.
"""
import ctypes
from ctypes import POINTER, Structure, byref, c_void_p, c_wchar_p, c_int, c_ushort
from ctypes import wintypes as W
import comtypes
from comtypes import GUID, IUnknown, COMMETHOD, HRESULT

CLSID_MMDeviceEnumerator = GUID('{BCDE0395-E52F-467C-8E3D-C4579291692E}')
CLSID_PolicyConfigClient = GUID('{870af99c-171d-4f9e-af0d-e63df40c2bc9}')
PKEY_FRIENDLY = (GUID('{a45c254e-df1c-4efd-8020-67d146a850e0}'), 14)


class _PROPERTYKEY(Structure):
    _fields_ = [('fmtid', GUID), ('pid', W.DWORD)]


class _PROPVARIANT(Structure):
    _fields_ = [('vt', c_ushort), ('r1', c_ushort), ('r2', c_ushort), ('r3', c_ushort), ('val', c_void_p), ('pad', c_void_p)]


class _IPropertyStore(IUnknown):
    _iid_ = GUID('{886d8eeb-8cf2-4446-8d02-cdba1dbdcf99}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'GetCount', (['out'], POINTER(W.DWORD))),
        COMMETHOD([], HRESULT, 'GetAt', (['in'], W.DWORD), (['out'], POINTER(_PROPERTYKEY))),
        COMMETHOD([], HRESULT, 'GetValue', (['in'], POINTER(_PROPERTYKEY)), (['out'], POINTER(_PROPVARIANT))),
    ]


class IPolicyConfig(IUnknown):
    _iid_ = GUID('{f8679f50-850a-41cf-9c72-430f290290c8}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'GetMixFormat', (['in'], c_wchar_p), (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'GetDeviceFormat', (['in'], c_wchar_p), (['in'], c_int), (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'ResetDeviceFormat', (['in'], c_wchar_p)),
        COMMETHOD([], HRESULT, 'SetDeviceFormat', (['in'], c_wchar_p), (['in'], c_void_p), (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'GetProcessingPeriod', (['in'], c_wchar_p), (['in'], c_int), (['in'], c_void_p), (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'SetProcessingPeriod', (['in'], c_wchar_p), (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'GetShareMode', (['in'], c_wchar_p), (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'SetShareMode', (['in'], c_wchar_p), (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'GetPropertyValue', (['in'], c_wchar_p), (['in'], c_void_p), (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'SetPropertyValue', (['in'], c_wchar_p), (['in'], c_void_p), (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'SetDefaultEndpoint', (['in'], c_wchar_p), (['in'], c_int)),
        COMMETHOD([], HRESULT, 'SetEndpointVisibility', (['in'], c_wchar_p), (['in'], c_int)),
    ]


class IMMDevice(IUnknown):
    _iid_ = GUID('{D666063F-1587-4E43-81F1-B948E807363F}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'Activate', (['in'], c_void_p), (['in'], W.DWORD), (['in'], c_void_p), (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'OpenPropertyStore', (['in'], W.DWORD), (['out'], POINTER(POINTER(_IPropertyStore)))),
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


def _friendly_name(dev):
    try:
        ps = dev.OpenPropertyStore(0)
        key = _PROPERTYKEY(*PKEY_FRIENDLY)
        pv = ps.GetValue(byref(key))
        name = ctypes.wstring_at(pv.val) if pv.vt == 31 and pv.val else ''
        ctypes.windll.ole32.PropVariantClear(byref(pv))
        return name
    except Exception:
        return ''


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

    def current_info(self):
        """Return (device_id, friendly_name) of current default render endpoint, or (None, '')."""
        try:
            dev = self.en.GetDefaultAudioEndpoint(0, 1)
            return dev.GetId(), _friendly_name(dev)
        except Exception:
            return None, ''

    def set_default(self, device_id):
        """Restore Windows default render endpoint (eConsole, eMultimedia, eCommunications)."""
        if not device_id:
            return False
        try:
            policy = comtypes.CoCreateInstance(CLSID_PolicyConfigClient, IPolicyConfig, comtypes.CLSCTX_ALL)
            for role in (0, 1, 2):
                policy.SetDefaultEndpoint(device_id, role)
            return True
        except Exception:
            return False

    def close(self):
        self.en = None
        try: comtypes.CoUninitialize()
        except Exception: pass


if __name__ == '__main__':
    print(DefaultOutputWatcher().current())
