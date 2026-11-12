"""HidHide output damage, hidapi path mapping, DualSense -> XInput mapping. Run: python test_compat.py [--live]
--live also creates a real virtual Xbox pad for a moment and checks it through XInput (needs ViGEmBus)."""
import pathlib
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


def test_game_caps():
    import os, tempfile
    from game_caps import GameCaps
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, 'caps.json')
        c = GameCaps(path)
        assert not c.has('game.exe', 'speaker')
        assert c.note('game.exe', 'speaker') is True and c.note('game.exe', 'speaker') is False
        assert c.note('', 'speaker') is False and c.note('explorer.exe', 'trigger') is False and c.note('ps5haptics.exe', 'trigger') is False
        c.note('game.exe', 'trigger')
        again = GameCaps(path)                                           # survives a restart
        assert again.has('game.exe', 'speaker') and again.has('game.exe', 'trigger') and not again.has('other.exe', 'trigger')
        again.reset()
        assert not again.has('game.exe', 'speaker') and not os.path.exists(path)
        with open(path, 'w') as f:
            f.write('{broken')
        assert GameCaps(path).data == {}                                 # damaged file: learn again


def test_edge_is_found(monkey=None):
    import ds_hid
    keep = ds_hid.hid.enumerate
    try:
        ds_hid.hid.enumerate = lambda v, p: [{'product_id': 0x0DF2, 'interface_number': 0, 'path': b'audio'}, {'product_id': 0x0DF2, 'interface_number': 3, 'path': b'edge'}]
        assert ds_hid.find_path() == (b'edge', 0x0DF2)
        ds_hid.hid.enumerate = lambda v, p: [{'product_id': 0x09CC, 'interface_number': 3, 'path': b'ds4'}]
        assert ds_hid.find_path() == (None, None)                        # PS4 controllers are not driven by this path
    finally:
        ds_hid.hid.enumerate = keep
    assert gaming_pad_paths(listing([[device(r'HID\VID_054C&PID_0DF2&MI_03\7&1&0&0000')]])) == [r'HID\VID_054C&PID_0DF2&MI_03\7&1&0&0000']
    assert instance_path(r'\\?\hid#vid_054c&pid_0df2&mi_03#7&1&0&0000#{g}') == r'HID\VID_054C&PID_0DF2&MI_03\7&1&0&0000'


def test_tap_buffer():
    from pad_speaker import PadSpeakerTap
    t = PadSpeakerTap('x', print, threading.Event())
    tone = np.full(480, 0.5, np.float32)
    def push():
        with t.lock:
            t.buf.append(tone.copy()); t.n += 480
    push(); assert not np.any(t.pull(480))                               # below the cushion: wait
    push(); push(); out = t.pull(480)
    assert out[0] == 0.0 and out[-1] == 0.5 and 0 < out[48] < 0.5        # starts with a short fade, no click
    assert np.all(t.pull(480) == 0.5) and np.all(t.pull(480) == 0.5)
    assert not np.any(t.pull(480)) and t.underruns == 1 and not t.primed  # ran dry: rebuild the cushion
    for _ in range(300):
        push()
    assert t.n >= 480 * 300; t.primed = True; t.pull(480)
    assert t.n <= 48000 * 0.04                                           # backlog trimmed, latency stays low


def test_fuse_modes():
    from takeover import fuse
    a = np.full(8, 0.2, np.float32); g = np.full(8, 0.4, np.float32)
    assert np.allclose(fuse(a, a, g, g, 0.0, '纯用游戏')[0], np.tanh(0.4))
    assert np.allclose(fuse(a, a, g, g, 0.0, '替换')[0], np.tanh(0.2))
    assert np.allclose(fuse(a, a, g, g, 0.0, '叠加')[0], np.tanh(0.6))
    for old, new in (('仅游戏震动', '纯用游戏'), ('仅声音触感', '替换'), ('融合', '叠加')):         # saved settings keep working
        assert np.allclose(fuse(a, a, g, g, 0.0, old)[0], fuse(a, a, g, g, 0.0, new)[0])


def test_presets():
    import tkinter as tk
    from ui_scifi import PRESETS, preset_of, bind_presets
    for name, vals in PRESETS.items():
        assert preset_of(**vals) == name
    assert preset_of('叠加', '替换', '特殊事件') == '自定义'
    root = tk.Tk(); root.withdraw()
    try:
        v = lambda x: tk.StringVar(root, x)
        sv = {'mode': v('普通'), 'fusion': v('叠加'), 'trigger_mode': v('自动'), 'speaker_mode': v('特殊事件'),
              'custom_fusion': v('叠加'), 'custom_trigger_mode': v('叠加'), 'custom_speaker_mode': v('全部游戏声')}
        p = tk.StringVar(root); bind_presets(p, sv)
        now = lambda: (sv['fusion'].get(), sv['trigger_mode'].get(), sv['speaker_mode'].get())
        assert p.get() == '全感'
        p.set('声感'); assert now() == ('替换', '替换', '全部游戏声') and sv['mode'].get() == '原生透传'     # 普通 can't drop game effects
        sv['trigger_mode'].set('纯用游戏'); assert p.get() == '自定义'                                       # changed by hand
        assert sv['custom_trigger_mode'].get() == '纯用游戏' and sv['custom_fusion'].get() == '替换'
        p.set('全感'); assert now() == ('叠加', '自动', '特殊事件')
        assert sv['custom_trigger_mode'].get() == '纯用游戏'                                                 # a preset doesn't overwrite it
        p.set('自定义'); assert now() == ('替换', '纯用游戏', '全部游戏声') and p.get() == '自定义'             # restored
        sv['trigger_mode'].set('替换'); assert p.get() == '声感'                                             # happens to match again
        sv2 = {k: v(x.get()) for k, x in sv.items()}; sv2.update(fusion=v('替换'), trigger_mode=v('叠加'), speaker_mode=v('特殊事件'))
        p2 = tk.StringVar(root); bind_presets(p2, sv2)                                                      # saved non-preset setup
        assert p2.get() == '自定义' and sv2['custom_trigger_mode'].get() == '叠加' and sv2['custom_fusion'].get() == '替换'
    finally:
        root.destroy()


def test_next_step():
    import hid
    from ui_scifi import next_step, pad_status
    assert next_step(True, None, ['x']) is None
    assert next_step(False, 'usb', ['HidHide'])[1] == 'deps' and 'HidHide' in next_step(False, None, ['HidHide'])[0]
    assert next_step(False, None, [])[1] == 'repair'
    assert next_step(False, 'bluetooth', [])[1] is None
    assert next_step(False, 'usb', [])[1] == 'start' and next_step(False, 'edge', [])[1] == 'start'
    keep = hid.enumerate
    try:
        usb = {'product_id': 0x0CE6, 'interface_number': 3, 'path': b'\\\\?\\hid#vid_054c&pid_0ce6&mi_03#7&1#{g}'}
        edge = dict(usb, product_id=0x0DF2)
        bt = {'product_id': 0x0CE6, 'interface_number': -1, 'path': b'\\\\?\\hid#{00001124-0000-1000-8000-00805f9b34fb}_vid&0002054c_pid&0ce6#8#{g}'}
        for rows, want in (([], None), ([usb], 'usb'), ([edge], 'edge'), ([bt], 'bluetooth'), ([bt, usb], 'usb'),
                           ([{'product_id': 0x09CC, 'interface_number': 3, 'path': b'ds4'}], None)):
            hid.enumerate = lambda v, p, r=rows: r
            assert pad_status() == want, (rows, want)
    finally:
        hid.enumerate = keep


class ScriptedDetector:
    def __init__(self): self.p = 0.0
    def feed(self, mono): return self.p


def test_voice_gate():
    from voice_gate import VoiceGate, VoiceDetector
    hop = 480
    quiet = np.full(hop, 0.01, np.float32)
    t = np.arange(hop) / 48000
    voice = (0.05 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
    d = ScriptedDetector(); g = VoiceGate(d)
    assert all(g.gain(voice, voice) == 1.0 for _ in range(20))            # no voice: untouched
    d.p = 0.95
    for _ in range(40):
        v = g.gain(voice, voice)
    assert g.closed and v < 0.15                                          # pure voice: down
    loud = (0.2 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)        # a much louder sound joins in
    v = g.gain(loud, loud); assert not g.closed
    for _ in range(5):
        v = g.gain(voice, voice)
    assert v > 0.95 and not g.closed                                      # stays open for a moment
    for _ in range(80):
        v = g.gain(voice, voice)
    assert g.closed                                                       # voice alone again
    low = (0.05 * np.sin(2 * np.pi * 60 * t)).astype(np.float32)
    g.gain(voice + low, voice + low); assert not g.closed                 # a low thump the voice does not have
    d = ScriptedDetector(); g = VoiceGate(d); d.p = 0.95
    for _ in range(40):
        g.gain(voice, voice)
    hi = (0.04 * np.sin(2 * np.pi * 8000 * t)).astype(np.float32)
    g.gain(voice + hi, voice + hi); assert not g.closed                   # metal on metal
    d = ScriptedDetector(); g = VoiceGate(d); d.p = 0.95
    g.gain(voice, voice); assert not g.closed                             # one stray window is not speech
    d.p = 0.0; g.gain(voice, voice); d.p = 0.95; g.gain(voice, voice); assert not g.closed
    det = VoiceDetector()                                                 # the shipped model loads and runs in real time blocks
    probs = [det.feed(np.zeros(hop)) for _ in range(20)]
    assert all(0 <= x <= 1 for x in probs) and max(probs) < 0.5


def test_fine_profile():
    import haptic_dsp as D
    assert len(D.GRIP_PROFILES['细腻']['bands']) == 6 and max(b[5] for b in D.GRIP_PROFILES['细腻']['bands']) <= 200
    t = np.arange(48000 * 6) / 48000
    notes = (0.15 * (np.sin(2 * np.pi * 220 * t) + 0.7 * np.sin(2 * np.pi * 277 * t) + 0.7 * np.sin(2 * np.pi * 330 * t))).astype(np.float32)
    rng = np.random.default_rng(1)
    hit = notes.copy()
    burst = (0.8 * rng.standard_normal(4800) * np.exp(-np.arange(4800) / 480)).astype(np.float32)
    hit[48000 * 4:48000 * 4 + 4800] += burst

    def run(x, prof):
        e = D.HapticEngine(prof, '均衡冲击', style='标准'); out = []
        for i in range(0, len(x) - D.HOP, D.HOP):
            b = x[i:i + D.HOP]; out.append(e.process(b, b)[0])
        return np.concatenate(out)
    y = run(notes, '细腻'); assert np.sqrt(np.mean(y[48000 * 2:] ** 2)) < 0.01                  # steady music stays out
    y = run(hit, '细腻'); peak = np.max(np.abs(y[48000 * 4:48000 * 4 + 12000])); assert 0.2 < peak <= 1.0  # a hit comes through, within range
    assert np.max(np.abs(y)) <= 1.0
    S = np.abs(np.fft.rfft(y[48000 * 4:48000 * 4 + 12000])) ** 2; f = np.fft.rfftfreq(12000, 1 / 48000)
    fine = S[f >= 90].sum() / S.sum()
    y0 = run(hit, '均衡'); S0 = np.abs(np.fft.rfft(y0[48000 * 4:48000 * 4 + 12000])) ** 2
    assert fine > S0[f >= 90].sum() / S0.sum()                                                  # more of it above the body than 均衡


def test_hidhide_inverse():
    import json, tempfile, pathlib
    from takeover import Isolation
    pad = r'HID\VID_054C&PID_0CE6&MI_03\7&1&0&0000'
    for state, restored in (('--inv-on', True), ('--inv-off', False)):
        iso = Isolation.__new__(Isolation); logs = []; calls = []
        iso.log = logs.append; iso.receipt = None
        iso.path = pathlib.Path(tempfile.mkdtemp()) / 'r.json'
        iso.cli = pathlib.Path(__file__)                       # "installed"

        def hh(*args, st=state):
            calls.append(args)
            if '--inv-state' in args:
                return st + '\n--cloak-off\n'
            if '--dev-gaming' in args:
                return json.dumps([{'devices': [{'present': True, 'deviceInstancePath': pad}]}])
            return ''
        iso.hh = hh
        iso.open()
        change = calls[-1]
        assert ('--inv-off' in change) == restored and '--cloak-on' in change and '--dev-hide' in change
        assert json.loads(iso.path.read_text('utf8'))['restore_inv_on'] is restored      # survives a crash
        iso.close()
        assert ('--inv-on' in calls[-1]) == restored and '--dev-unhide' in calls[-1] and not iso.path.exists()
        assert any('反向' in m for m in logs) == restored


def test_receiver_paths():
    import ds_hid, re as _re
    from takeover import collection_paths, DEV_PATTERN
    guid = '{4d1e55b2-f16f-11cf-88cb-001111000030}'
    col1 = rf'\\?\hid#vid_054c&pid_0ce6&mi_03&col01#8&2f3a&0&0000#{guid}'
    col2 = rf'\\?\hid#vid_054c&pid_0ce6&mi_03&col02#8&2f3a&0&0001#{guid}'
    kbd = rf'\\?\hid#vid_054c&pid_0ce6&mi_05#8&aa&0&0000#{guid}'
    bt = rf'\\?\hid#{{00001124-0000-1000-8000-00805f9b34fb}}_vid&0002054c_pid&0ce6#8&1&0&0000#{guid}'
    plain = rf'\\?\hid#vid_054c&pid_0ce6#7&1&0&0000#{guid}'
    assert instance_path(col1) == r'HID\VID_054C&PID_0CE6&MI_03&COL01\8&2f3a&0&0000'
    assert instance_path(plain) == r'HID\VID_054C&PID_0CE6\7&1&0&0000' and instance_path(bt) is None
    rows = [dict(product_id=0x0CE6, interface_number=3, usage_page=0xFF00, usage=1, path=col2.encode()),
            dict(product_id=0x0CE6, interface_number=5, usage_page=1, usage=6, path=kbd.encode()),
            dict(product_id=0x0CE6, interface_number=-1, usage_page=1, usage=5, path=bt.encode()),
            dict(product_id=0x0CE6, interface_number=3, usage_page=1, usage=5, path=col1.encode())]
    keep = ds_hid.hid.enumerate
    try:
        ds_hid.hid.enumerate = lambda v, p: rows
        assert ds_hid.find_path() == (col1.encode(), 0x0CE6)                # gamepad, not the vendor channel, Bluetooth or keyboard
        ds_hid.hid.enumerate = lambda v, p: [rows[2], rows[0]]
        assert ds_hid.find_path()[0] == col2.encode()                       # a receiver beats Bluetooth
        ds_hid.hid.enumerate = lambda v, p: [rows[2]]
        assert ds_hid.is_bluetooth(ds_hid.find_path()[0])
        ds_hid.hid.enumerate = lambda v, p: [dict(product_id=0x0CE6, interface_number=2, usage_page=1, usage=5, path=b'other')]
        assert ds_hid.find_path()[0] == b'other'                            # another interface number, still a gamepad
        ds_hid.hid.enumerate = lambda v, p: [dict(product_id=0x0CE6, interface_number=0, path=b'audio')]
        assert ds_hid.find_path() == (None, None)
    finally:
        ds_hid.hid.enumerate = keep
    both = collection_paths(col1.encode(), rows)
    assert sorted(both) == sorted([instance_path(col1), instance_path(col2)])     # both collections, no keyboard
    assert collection_paths(bt.encode(), rows) == []
    for good in (instance_path(col1), instance_path(plain), PAD, r'USB\VID_054C&PID_0CE6&MI_03\7&1'):
        assert _re.match(DEV_PATTERN, good, _re.I), good
    for bad in (r'USB\VID_054C&PID_0CE6\7&1', r'HID\VID_054C&PID_09CC&MI_03\7&1', r'HID\VID_054C&PID_0CE6&MI_05\7&1'):
        assert not _re.match(DEV_PATTERN, bad, _re.I), bad


def test_trigger_presets():
    import trigger_presets as TP
    from ds_hid import effect_zones
    cfg = TP.scene_defaults()
    assert TP.effects(cfg) == (None, None)                                   # default: follow the sound
    for name in TP.NAMES[1:]:
        c = dict(cfg); c['trig_preset_L'] = name; c.update({TP.key(k, 'L'): v for k, v in TP.values_for(name).items()})
        e = TP.effect(c, 'L')
        assert e is not None and len(e) == 11 and e[0] in (0x21, 0x25, 0x26), name
        assert TP.effect(c, 'R') is None
        assert set(TP.uses(name)) <= set(TP.PARAMS)
    c = dict(cfg, trig_preset_R='重阻力', trig_start_R=50, trig_strength_R=7)
    e = TP.effect(c, 'R'); mask = int.from_bytes(e[1:3], 'little')
    assert e[0] == 0x21 and mask == sum(1 << z for z in range(5, 10))           # resistance from the middle on
    c = dict(cfg, trig_preset_L='枪械扳机', trig_start_L=30, trig_end_L=60, trig_strength_L=6)
    e = TP.effect(c, 'L'); assert e[0] == 0x25 and int.from_bytes(e[1:3], 'little') == (1 << 3) | (1 << 6) and e[3] == 5
    c = dict(cfg, trig_preset_L='枪械扳机', trig_start_L=80, trig_end_L=20)    # nonsense from sliders still valid
    e = TP.effect(c, 'L'); m = int.from_bytes(e[1:3], 'little'); assert bin(m).count('1') == 2
    c = dict(cfg, trig_preset_L='连发震动', trig_freq_L=999, trig_strength_L=0)
    e = TP.effect(c, 'L'); assert e[0] == 0x26 and e[9] == 60
    c = dict(cfg, trig_preset_L='渐重', trig_start_L=0, trig_strength_L=8)
    e = TP.effect(c, 'L'); packed = int.from_bytes(e[3:7], 'little')
    lv = [(packed >> (3 * z)) & 7 for z in range(10)]
    assert lv == sorted(lv) and lv[-1] == 7 and lv[0] == 0                    # light to heavy
    assert effect_zones([0] * 10) == bytes([5] + [0] * 10)
    assert TP.effect_name(None) == '-' and TP.effect_name(e) == '阻力'


def test_selfcheck_report():
    import selfcheck, getpass, os, urllib.parse
    from unittest.mock import patch
    home = os.path.expanduser('~'); user = getpass.getuser()
    text = selfcheck.sanitize(f'log at {home}\\traces and user {user} done')
    assert home not in text and (len(user) <= 2 or user not in text)
    t0 = 1000.0
    tl = [(t0 + i / 10, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, '', '-', '-') for i in range(50)]
    tl[20] = (t0 + 2.0, 0.1, 0.9, 1.0, 0.0, 0.9, 0.8, '破军·蓄力命中', '-', '震动')
    out = []; selfcheck._timeline(out, tl)
    rows = [r for r in out[2:]]
    assert len(rows) == 1 and '破军·蓄力命中' in rows[0] and '0.90' in rows[0]   # only rows with vibration
    url = selfcheck.issue_url(['缺少驱动：HidHide。到「系统」页点「一键安装依赖」。'])
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert url.startswith('https://github.com/QINYAN123/PS5Haptics/issues/new?') and '缺少驱动' in q['title'][0] and 'Ctrl+V' in q['body'][0]
    assert len(url) < 2000
    with patch.object(selfcheck, '_try', return_value=True):
        problems, _ = selfcheck.collect(stats={'xruns': 0, 'late': 1239})
        assert any('积压丢块' in p for p in problems)
        assert all('输出欠载' not in p for p in problems)


def test_game_profiles():
    import ps5_haptics_app as A
    cfg = dict(A.DEFAULTS); cfg['scene'] = '游戏'; cfg['grip_gain'] = 1.0
    cfg['user_games'] = [dict(id='a', name='某游戏', exe=r'D:\Games\Foo\Foo.exe', own=True, settings={'grip_gain': 0.4, 'trig_preset_R': '枪械扳机'}),
                         dict(id='b', name='另一个', exe=r'D:\Games\Bar\Bar.exe', own=False, settings={'grip_gain': 0.1})]
    r = A.Runner(cfg, lambda m: None)
    assert r.profile_for('foo.exe')['name'] == '某游戏' and r.profile_for('bar.exe') is None and r.profile_for('') is None
    eff = r.effective('foo.exe')
    assert eff['grip_gain'] == 0.4 and eff['trig_preset_R'] == '枪械扳机' and cfg['grip_gain'] == 1.0     # base untouched
    assert r.effective('bar.exe') is cfg and r.effective('other.exe') is cfg
    assert sorted(r.user_exes()) == ['bar.exe', 'foo.exe']
    cfg['scene'] = '影院'
    assert r.profile_for('foo.exe') is None                                   # films / music keep their own scene


def test_steam_app_for():
    import covers, tempfile
    d = pathlib.Path(tempfile.mkdtemp()) / 'steamapps'
    (d / 'common' / 'Some Game' / 'bin').mkdir(parents=True)
    (d / 'appmanifest_123.acf').write_text('"AppState"\n{\n\t"appid"\t\t"123"\n\t"name"\t\t"Some Game: Deluxe"\n\t"installdir"\t\t"Some Game"\n}\n', 'utf8')
    assert covers.steam_app_for(d / 'common' / 'Some Game' / 'bin' / 'game.exe') == (123, 'Some Game: Deluxe')
    assert covers.steam_app_for(r'D:\Games\Foo\Foo.exe') == (None, None)


def test_notes_url():
    import updater
    page = 'https://github.com/QINYAN123/PS5Haptics/releases/tag/v9.9.9'
    assert updater.notes_url(page, 'zh') == page
    u = updater.notes_url(page, 'ja')
    assert u.startswith('https://github-com.translate.goog/QINYAN123/PS5Haptics/releases/tag/v9.9.9?') and '_x_tr_tl=ja' in u
    assert updater.notes_url(None, 'zh').startswith('https://github.com/QINYAN123/PS5Haptics/releases')
    assert updater.notes_url('https://example.com/x', 'en') == 'https://example.com/x'


def test_deps_checks():
    import deps, tempfile
    real_svc, real_exe = deps.service_exists, deps.usbip_exe
    try:
        deps.service_exists = lambda name: True
        deps.usbip_exe = lambda: None
        assert deps.installed('usbip') is False and deps.installed('hidhide') is True   # driver left behind, no usbip.exe
        deps.usbip_exe = lambda: pathlib.Path(r'C:\Program Files\USBip\usbip.exe')
        assert deps.installed('usbip') is True
    finally:
        deps.service_exists, deps.usbip_exe = real_svc, real_exe
    assert set(deps.OFFICIAL) == {d[0] for d in deps.DEPS}
    for key, (url, sha) in deps.OFFICIAL.items():
        assert url.startswith('https://github.com/') and len(sha) == 64
        pat = next(d[3] for d in deps.DEPS if d[0] == key)
        assert pathlib.PurePath(url.rsplit('/', 1)[1]).match(pat), (url, pat)
    import xds
    assert 'usbip' in xds.USBIP_HELP and '重启' in xds.USBIP_HELP


def test_stuck_guard():
    import ps5_haptics_app as A
    import numpy as np
    logs = []; g = A.StuckGuard(logs.append)
    vib = bytes([0x26, 0xFF, 3] + [0] * 8); bow = bytes([0x22, 2, 2, 0x3C] + [0] * 7)
    assert g.trigger('R', vib, 0.0) == vib and g.trigger('R', vib, 2.0) == vib
    assert g.trigger('R', vib, 3.5) is None and logs                  # trigger vibration left on: released
    assert g.trigger('R', bow, 3.6) == bow and g.trigger('R', bow, 60.0) == bow   # resistance may stay forever
    assert g.trigger('R', vib, 61.0) == vib                             # a new vibration works again
    assert g.motors(80, 40, 0.0) == (80, 40) and g.motors(80, 40, 6.0) == (0, 0)
    assert g.motors(90, 40, 6.1) == (90, 40)                            # values change: the game is driving it
    step = A.HOP / A.RATE
    t = 100.0
    phase = 2 * np.pi * 97 * np.arange(A.HOP) / A.RATE

    def render(seconds, amplitudes, events=False):
        nonlocal t
        states = []
        start = t
        for i in range(round(seconds / step)):
            t = start + i * step
            left, right = amplitudes(i * step)
            carrier = np.sin(phase + 2 * np.pi * 97 * i * step)
            event = start + int(i * step) if events else 0.0
            states.append(g.wave(left * carrier, right * carrier, event, t))
        t = start + seconds
        return states

    plateau = lambda s: (0.60 + 0.012 * np.sin(13 * s), 0.32)
    assert not any(render(12, plateau, events=True))   # recent events keep regular feedback
    g.reset_wave()
    assert not any(render(12, lambda s: (0.4 + 0.17 * np.sin(3 * s), 0.32)))
    g.reset_wave()
    assert not any(render(5.9, plateau))               # no event mod, issue #2 pattern
    assert render(1, plateau)[-1]
    assert all(render(1, plateau))
    assert not render(0.2, lambda s: (0.60, 0.48))[-1]  # new effect rearms, even if the strongest side stays the same
    assert render(6.5, plateau)[-1]
    assert not render(0.2, lambda s: (0.0, 0.0))[-1]    # silence rearms the guard
    assert not any(render(0.5, plateau))


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
    test_hidhide_output(); test_instance_path(); test_mapping(); test_battery(); test_controls(); test_input_filter(); test_pad_commands(); test_volume(); test_game_caps(); test_edge_is_found(); test_tap_buffer(); test_fuse_modes(); test_presets(); test_next_step(); test_voice_gate(); test_fine_profile(); test_hidhide_inverse(); test_receiver_paths(); test_trigger_presets(); test_selfcheck_report(); test_game_profiles(); test_steam_app_for(); test_notes_url(); test_deps_checks(); test_stuck_guard()
    print('offline tests ok')
    if '--live' in sys.argv:
        test_live()
        print('live test ok')
