"""Reads (never changes) the default output's volume, so lowering it need not weaken the haptics.

An endpoint with software volume attenuates what loopback captures; one with hardware volume does not,
so its signal is left alone. Per-process capture does not go through this path at all.
"""
from ctypes import POINTER, byref, c_float, c_void_p
from ctypes import wintypes as W

import comtypes
from comtypes import GUID, COMMETHOD, HRESULT, IUnknown

from audio_sessions import CLSID_MMDeviceEnumerator, IMMDeviceEnumerator

MAX_GAIN = 8.0
HARDWARE_VOLUME = 0x1             # ENDPOINT_HARDWARE_SUPPORT_VOLUME


class IAudioEndpointVolume(IUnknown):
    _iid_ = GUID('{5CDF2C82-841E-4546-9722-0CF74078229A}')
    _methods_ = [           # full vtable order; only the two reads at the marked slots are used
        COMMETHOD([], HRESULT, 'RegisterControlChangeNotify', (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'UnregisterControlChangeNotify', (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'GetChannelCount', (['out'], POINTER(W.UINT))),
        COMMETHOD([], HRESULT, 'SetMasterVolumeLevel', (['in'], c_float), (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'SetMasterVolumeLevelScalar', (['in'], c_float), (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'GetMasterVolumeLevel', (['out'], POINTER(c_float))),      # used
        COMMETHOD([], HRESULT, 'GetMasterVolumeLevelScalar', (['out'], POINTER(c_float))),
        COMMETHOD([], HRESULT, 'SetChannelVolumeLevel', (['in'], W.UINT), (['in'], c_float), (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'SetChannelVolumeLevelScalar', (['in'], W.UINT), (['in'], c_float), (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'GetChannelVolumeLevel', (['in'], W.UINT), (['out'], POINTER(c_float))),
        COMMETHOD([], HRESULT, 'GetChannelVolumeLevelScalar', (['in'], W.UINT), (['out'], POINTER(c_float))),
        COMMETHOD([], HRESULT, 'SetMute', (['in'], W.BOOL), (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'GetMute', (['out'], POINTER(W.BOOL))),
        COMMETHOD([], HRESULT, 'GetVolumeStepInfo', (['out'], POINTER(W.UINT)), (['out'], POINTER(W.UINT))),
        COMMETHOD([], HRESULT, 'VolumeStepUp', (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'VolumeStepDown', (['in'], c_void_p)),
        COMMETHOD([], HRESULT, 'QueryHardwareSupport', (['out'], POINTER(W.DWORD))),      # used
    ]


def gain_for(level_db, support_mask):
    """Make-up gain for captured audio at this volume. Hardware volume leaves the capture untouched."""
    if support_mask & HARDWARE_VOLUME:
        return 1.0
    return min(MAX_GAIN, 10 ** (-min(level_db, 0.0) / 20))


class VolumeMonitor:
    """Call from one thread. gain() re-reads the current default device, so switching outputs just works."""

    def __init__(self):
        comtypes.CoInitialize()
        self.en = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_ALL)

    def read(self):
        dev = self.en.GetDefaultAudioEndpoint(0, 1)          # eRender, eMultimedia
        unk = dev.Activate(byref(IAudioEndpointVolume._iid_), comtypes.CLSCTX_ALL, None)
        vol = unk.QueryInterface(IAudioEndpointVolume)
        return vol.GetMasterVolumeLevel(), vol.QueryHardwareSupport()

    def gain(self):
        try:
            return gain_for(*self.read())
        except Exception:
            return 1.0

    def close(self):
        self.en = None
        try: comtypes.CoUninitialize()
        except Exception: pass


if __name__ == '__main__':
    m = VolumeMonitor(); print(m.read(), m.gain()); m.close()
