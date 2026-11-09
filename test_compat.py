"""HidHide output damage, hidapi path mapping, DualSense -> XInput mapping. Run: python test_compat.py [--live]
--live also creates a real virtual Xbox pad for a moment and checks it through XInput (needs ViGEmBus)."""
import ctypes as C
import json
import sys
import time

import struct
import threading

import numpy as np

from audio_volume import MAX_GAIN, gain_for
from button_haptics import ButtonClicks, ControlTracker, WAVES
from ds_hid import DualSenseHID, battery_status, parse_firmware_info
from pad_input import InputFilter, deadzone
from takeover import gaming_pad_paths, instance_path, x360_report, VirtualX360

PAD = r'HID\VID_054C&PID_0CE6&MI_03\7&3a227958&0&0000'


def device(path, desc='HID-compliant game controller', present=True):
    d = {'present': present, 'gamingDevice': True, 'symbolicLink': 'x', 'vendor': 'v', 'product': 'p', 'serialNumber': '',
         'usage': 'Gamepad', 'description': desc, 'deviceInstancePath': path, 'baseContainerDeviceInstancePath': 'y',
         'baseContainerClassGuid': 'z', 'baseContainerDeviceCount': 2}
    return '{ ' + ' ,\n'.join(f'"{k}" : {json.dumps(v)}' for k, v in d.items()) + ' }'


def listing(groups):
    return '[ ' + ' ,\n'.join('{ "friendlyName" : "x" , "devices" : [\n' + ' ,\n'.join(g) + ' ] }' for g in groups) + ' ]'


def test_hidhide_output():
    other = r'HID\VID_046D&PID_C52B\1&2&3'
    whole = listing([[device(other)], [device(PAD), device(other)]])
    assert gaming_pad_paths(whole) == [PAD]
    assert gaming_pad_paths('') == []
    assert gaming_pad_paths(listing([[device(PAD, present=False)]])) == []
    # the reported failure: output stops inside a description on line 34
    cut_at = [i for i in range(len(whole)) if whole.startswith('"description" : "', i)][2] + len('"description" : "') + 3
    damaged = whole[:cut_at]
    try:
        json.loads(damaged); raise AssertionError('expected a JSON error')
    except ValueError as exc:
        assert 'Unterminated string' in str(exc), exc
    assert gaming_pad_paths(damaged) == [PAD]                  # pad before the cut survives
    early = whole[:whole.index(json.dumps(PAD))]               # pad after the cut: empty, hidapi path fills in
    assert gaming_pad_paths(early) == []


def test_instance_path():
    want = PAD
    for raw in (r'\\?\hid#vid_054c&pid_0ce6&mi_03#7&3a227958&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}',
                r'\\?\HID#VID_054C&PID_0CE6&MI_03#7&3A227958&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}'):
        assert instance_path(raw) == want, instance_path(raw)
        assert instance_path(raw.encode()) == want
    assert instance_path(None) is None
    assert instance_path(r'\\?\hid#{00001124-0000-1000-8000-00805f9b34fb}_vid&0002054c_pid&0ce6#8&1&0&0000#{x}') is None


def report(lx=128, ly=128, rx=128, ry=128, l2=0, r2=0, face=8, shoulder=0, misc=0):
    r = bytearray(64); r[0] = 1; r[33] = r[37] = 0x80                      # 0x80: no finger on the touchpad
    r[1:7] = bytes([lx, ly, rx, ry, l2, r2]); r[8], r[9], r[10] = face, shoulder, misc
    return bytes(r)


def test_mapping():
    x = x360_report(report())
    assert (x.wButtons, x.sThumbLX, x.sThumbLY, x.bLeftTrigger) == (0, 0, 0, 0)
    x = x360_report(report(lx=0, ly=0, rx=255, ry=255, l2=10, r2=200))
    assert (x.sThumbLX, x.sThumbLY, x.sThumbRX, x.sThumbRY) == (-32768, 32767, 32767, -32767)
    assert (x.bLeftTrigger, x.bRightTrigger) == (10, 200)
    for face, want in ((0x28, 0x1000), (0x48, 0x2000), (0x18, 0x4000), (0x88, 0x8000), (0, 0x1), (1, 0x9), (2, 0x8), (3, 0xA), (4, 0x2), (5, 0x6), (6, 0x4), (7, 0x5)):
        assert x360_report(report(face=face)).wButtons == want, hex(face)
    for sh, want in ((1, 0x100), (2, 0x200), (0x10, 0x20), (0x20, 0x10), (0x40, 0x40), (0x80, 0x80)):
        assert x360_report(report(shoulder=sh)).wButtons == want, hex(sh)
    assert x360_report(report(misc=1)).wButtons == 0x400
    assert x360_report(report(misc=2)).wButtons == 0x20


def test_battery():
    r = bytearray(64); r[0] = 1
    for status, want in ((0x00, (5, 'discharging')), (0x07, (75, 'discharging')), (0x0A, (100, 'discharging')),
                         (0x14, (45, 'charging')), (0x20, (100, 'full')), (0x2A, (100, 'full')), (0xA0, None), (0xF0, None)):
        r[53] = status
        assert battery_status(r) == want, hex(status)
    assert battery_status(r[:41]) is None


def test_controls():
    t = ControlTracker()
    t.update(report(face=0x28, l2=255)); assert t.drain() == []          # held at start: no press
    t.update(report(face=0x08, l2=255)); assert t.drain() == [('button', 'R', 'face', False)]
    t.update(report(shoulder=0x01, l2=255)); assert t.drain() == [('button', 'L', 'shoulder', True)]
    t.update(report(shoulder=0x01, l2=255)); assert t.drain() == []      # still held: nothing new
    t.update(report(shoulder=0x04, l2=255)); assert t.drain() == [('button', 'L', 'shoulder', False)]   # L2 digital bit alone: no click
    t.update(report(face=0)); assert t.drain() == [('trigger', 'L', 'trigger', False), ('button', 'L', 'dpad', True)][::-1]
    t.update(report(face=2)); assert t.drain() == [('button', 'L', 'dpad', True)]    # up -> right is a new press
    t.update(report(face=8)); assert t.drain() == [('button', 'L', 'dpad', False)]
    t.update(report(misc=0x02)); assert t.drain() == [('button', 'both', 'face', True)]
    t.update(report()); t.drain()
    for v, want in ((120, []), (149, []), (150, [('trigger', 'R', 'trigger', True)]), (120, []), (101, []), (100, [('trigger', 'R', 'trigger', False)]), (149, [])):
        t.update(report(r2=v)); assert t.drain() == want, v              # hysteresis: no chatter around one value
    t.update(report()); t.drain()
    t.update(report(rx=255)); assert t.drain() == [('stick', 'R', 'stick', True)]
    t.update(report(rx=230)); assert t.drain() == []                     # still near the edge
    t.update(report()); assert t.drain() == []                           # return to centre is silent
    r = bytearray(report()); r[33] = 0x05
    t.update(r); assert t.drain() == [('touch', 'both', 'small', True)]
    t.update(r); assert t.drain() == []
    t.update(report()); assert t.drain() == []                           # lifting the finger is silent
    for _ in range(100):
        t.update(report(face=0x28)); t.update(report())
    assert len(t.drain()) == 64                                          # bounded

    c = ButtonClicks(); c.add('R', 'face', True)
    l, r = c.render(480)
    assert np.max(np.abs(r)) > 0.4 and np.max(np.abs(l)) < np.max(np.abs(r)) * 0.2 + 1e-6
    press_peak = np.max(np.abs(r))
    n = 0
    while c.voices:
        n += 1; l, r = c.render(480)
        assert n < 20, 'click never ends'
    assert np.max(np.abs(r)) < 0.02                                      # decays to nothing, no end click
    assert not np.any(c.render(480)[1])                                  # silent once finished
    c = ButtonClicks(); c.add('R', 'face', False)
    assert np.max(np.abs(c.render(480)[1])) < press_peak * 0.5           # release lighter
    c = ButtonClicks(); c.add('L', 'dpad', True)
    a = c.render(240, 2.0)[0]; c = ButtonClicks(); c.add('L', 'dpad', True)
    assert np.allclose(a, c.render(240, 1.0)[0] * 2, atol=1e-5)          # gain
    c = ButtonClicks()
    for _ in range(50):
        c.add('both', 'shoulder', True)
    assert len(c.voices) == 8
    for kind in WAVES:
        assert 0 < np.max(np.abs(WAVES[kind])) <= 1


def with_gyro(r, x, y, z):
    b = bytearray(r); struct.pack_into('<3h', b, 16, x, y, z); return bytes(b)


def test_input_filter():
    raw = report(lx=140, ly=128, rx=255, ry=128)
    assert InputFilter().apply(raw) is raw                               # nothing set: untouched
    f = InputFilter(0.2, 0.2)
    out = f.apply(raw)
    assert out[1] == 128 and out[2] == 128                               # inside the deadzone
    assert out[3] == 255 and out[4] == 128                               # edge still reaches full range
    just = f.apply(report(rx=128 + 40))
    assert 128 < just[3] < 128 + 40                                      # rescaled, starts from zero
    assert deadzone(128, 128, 0.3) == (128, 128) and deadzone(0, 128, 0.3)[0] == 1 or deadzone(0, 128, 0.3)[0] == 0
    f = InputFilter(gyro_bias=(10, -20, 30))
    assert struct.unpack_from('<3h', f.apply(with_gyro(raw, 15, -20, 0)), 16) == (5, 0, -30)
    assert struct.unpack_from('<3h', f.apply(with_gyro(raw, -32768, 32767, 0)), 16)[0] == -32768     # clamped
    assert f.apply(raw)[1:5] == raw[1:5]                                 # sticks untouched
    f = InputFilter(); f.calibrate(0.05)
    while f.cal_until:
        f.apply(with_gyro(raw, 12, -7, 3))
        time.sleep(0.0001)
    assert f.cal_result == ('ok', (12, -7, 3)) or f.cal_result == ('no_data', None), f.cal_result
    f = InputFilter(); f.cal_until = time.monotonic() + 3600
    for i in range(300):
        f.apply(with_gyro(raw, 12 + i % 3, -7, 3))
    f.cal_until = time.monotonic() - 1; f.apply(raw)
    assert f.cal_result == ('ok', (13, -7, 3)), f.cal_result
    f = InputFilter(); f.cal_until = time.monotonic() + 3600
    for i in range(300):
        f.apply(with_gyro(raw, i * 10, -7, 3))
    f.cal_until = time.monotonic() - 1; f.apply(raw)
    assert f.cal_result == ('moved', None)
    f = InputFilter(); f.cal_until = time.monotonic() - 1; f.apply(raw)
    assert f.cal_result == ('no_data', None)


class FakeDev:
    def __init__(self): self.sent = []
    def write(self, b): self.sent.append(bytes(b))
    def get_feature_report(self, rid, n):
        b = bytearray(64); b[0] = rid; b[1:12] = b'Jun  5 2023'; b[12:20] = b'12:34:56'
        b[24:28] = (0x01020304).to_bytes(4, 'little'); b[28:32] = (0x0A0B0C0D).to_bytes(4, 'little'); b[44:46] = (0x0123).to_bytes(2, 'little')
        return list(b)


def test_pad_commands():
    h = DualSenseHID.__new__(DualSenseHID); h.dev = FakeDev(); h.lock = threading.Lock()
    h.enable_speaker(); s = h.dev.sent[-1]
    assert s[0] == 2 and s[8] == 0x30 and s[1] & 0x20 and s[1] & 0x80 and len(s) == 48
    h.enable_speaker(headset=True); s = h.dev.sent[-1]
    assert s[8] == 0 and s[5] == 0x7C and s[1] & 0x10
    assert h.firmware_info() == {'date': 'Jun  5 2023', 'time': '12:34:56', 'hw': 0x01020304, 'fw': 0x0A0B0C0D, 'update': 0x0123}
    assert parse_firmware_info(b'') is None and parse_firmware_info(bytes(64)) is None
    h.dev.get_feature_report = lambda *a: (_ for _ in ()).throw(OSError('receiver said no'))
    assert h.firmware_info() is None


def test_volume():
    assert gain_for(0.0, 0) == 1.0
    assert abs(gain_for(-6.0206, 0) - 2.0) < 0.01
    assert gain_for(-60.0, 0) == MAX_GAIN
    assert gain_for(-20.0, 0x1) == 1.0                                   # hardware volume: capture is not attenuated
    assert abs(gain_for(-20.0, 0x2 | 0x4) - MAX_GAIN) < 1e-9
    assert gain_for(3.0, 0) == 1.0


class XGamepad(C.Structure):
    _fields_ = [('wButtons', C.c_ushort), ('bLeftTrigger', C.c_ubyte), ('bRightTrigger', C.c_ubyte),
                ('sThumbLX', C.c_short), ('sThumbLY', C.c_short), ('sThumbRX', C.c_short), ('sThumbRY', C.c_short)]


class XState(C.Structure):
    _fields_ = [('dwPacketNumber', C.c_uint), ('Gamepad', XGamepad)]


class XVibration(C.Structure):
    _fields_ = [('wLeft', C.c_ushort), ('wRight', C.c_ushort)]


def test_live():
    xi = C.WinDLL('xinput1_4')
    before = {i for i in range(4) if xi.XInputGetState(i, C.byref(XState())) == 0}
    v = VirtualX360(); v.open()
    try:
        slot = None; st = XState()
        for _ in range(40):
            time.sleep(0.1)
            v.forward(report(lx=255, l2=99, face=0x28))
            new = [i for i in range(4) if i not in before and xi.XInputGetState(i, C.byref(st)) == 0]
            if new:
                slot = new[0]
                if st.Gamepad.wButtons & 0x1000:
                    break
        assert slot is not None, 'virtual Xbox pad never appeared in XInput'
        xi.XInputGetState(slot, C.byref(st))
        assert st.Gamepad.wButtons & 0x1000 and st.Gamepad.bLeftTrigger == 99 and st.Gamepad.sThumbLX == 32767, (st.Gamepad.wButtons, st.Gamepad.bLeftTrigger, st.Gamepad.sThumbLX)
        assert xi.XInputSetState(slot, C.byref(XVibration(0xFFFF, 0x8000))) == 0
        for _ in range(20):
            time.sleep(0.05)
            if v.feedbacks:
                break
        large, small, color = v.rumble()
        assert large == 1.0 and 0.49 < small < 0.51 and color is None, (large, small, color)
        print('live: slot', slot, 'A/LT/LX ok, rumble', large, round(small, 3))
    finally:
        xi.XInputSetState(slot if slot is not None else 0, C.byref(XVibration(0, 0))) if slot is not None else None
        v.close()
    for i in range(100):
        if xi.XInputGetState(slot, C.byref(XState())) != 0:
            print(f'live: pad gone {i * 0.1:.1f} s after close')
            return
        time.sleep(0.1)
    raise AssertionError('virtual pad still there 10 s after close')


if __name__ == '__main__':
    test_hidhide_output(); test_instance_path(); test_mapping(); test_battery(); test_controls(); test_input_filter(); test_pad_commands(); test_volume()
    print('offline tests ok')
    if '--live' in sys.argv:
        test_live()
        print('live test ok')
