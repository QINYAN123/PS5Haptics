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
        assert e is not None and len(e) == 11 and e[0] in ((0x05,) if name == TP.OFF else (0x21, 0x25, 0x26)), name
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


def test_native_motor_mode_switch():
    from xds import VirtualDualSense

    pad = VirtualDualSense()

    def report(flags, flags2=0, left=80, right=40):
        payload = bytes([flags, 0, right, left, flags2]) + bytes(25)
        pad._on_raw(bytes([1, len(payload), 0]) + payload)
        return pad.feedback()[:2]

    assert report(0x03) == ((80, 40), True)
    assert report(0x04) == ((80, 40), True)  # Trigger-only report preserves motor state.
    assert report(0x02) == ((0, 0), False)   # Audio haptics ignore the motor bytes.
    assert report(0x02, 0x04) == ((80, 40), True)
    assert report(0x02, 0x04, 0, 0) == ((0, 0), True)


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
    assert g.motors(90, 40, 6.1, A.PBZ_STUCK_MOTOR_S) == (90, 40)
    assert g.motors(90, 40, 8.7, A.PBZ_STUCK_MOTOR_S) == (0, 0)
    assert g.motors(0, 0, 9.0, A.PBZ_STUCK_MOTOR_S) == (0, 0)
    assert g.motors(90, 40, 9.1, A.PBZ_STUCK_MOTOR_S) == (90, 40)                            # values change: the game is driving it
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
    g.reset()
    assert not g.wave_muted and not g.seen
    assert g.trigger('R', vib, 62.0) == vib


def test_equal_peak_wave_changes():
    import numpy as np
    import ps5_haptics_app as app

    guard = app.StuckGuard(lambda _: None)
    samples = np.arange(app.HOP) / app.RATE
    block = 0

    def render(seconds, right_tone):
        nonlocal block
        states = []
        for _ in range(round(seconds * app.RATE / app.HOP)):
            now = block * app.HOP / app.RATE
            t = now + samples
            left = 0.6 * np.sin(2 * np.pi * 100 * t)
            right = 0.3 * right_tone(t)
            states.append(guard.wave(left, right, 0.0, now))
            block += 1
        return states

    # Equal peaks on both grips, only the quieter grip changes frequency.
    alternating = lambda t: np.sin(2 * np.pi * np.where(t.astype(int) % 2, 180, 80) * t)
    assert not any(render(10, alternating))
    steady = lambda t: np.sin(2 * np.pi * 100 * t)
    assert render(7, steady)[-1]
    assert not render(0.3, lambda t: np.sin(2 * np.pi * 180 * t))[-1]
    # Same frequency and peak, but changing pulse width.
    guard.reset()
    pulses = lambda t: ((t * 100) % 1 < np.where(t.astype(int) % 2, 0.2, 0.8)).astype(float)
    assert not any(render(10, pulses))


def test_silent_source_releases_audio_triggers(scene="游戏", suppress=False):
    from contextlib import ExitStack
    from tempfile import TemporaryDirectory
    from unittest.mock import Mock, patch
    import ps5_haptics_app as app

    logs = []
    cfg = dict(app.DEFAULTS, mode='普通', capture='系统', speaker_mode='关闭', source_hint='loop',
               pbz_fx=False, ww_fx=False, ww_vision=False, scene=scene, scenes={scene: {"suppress_background": suppress}})
    runner = app.Runner(cfg, logs.append)
    clock = [100.]
    first = True
    def get(timeout):
        nonlocal first
        clock[0] += timeout
        if first:
            first = False
            return np.ones(2 * app.HOP, np.float32)
        if clock[0] >= 101.:
            runner.stop_ev.set()
        raise app.queue.Empty

    hid, engine, output = Mock(), Mock(), Mock()
    hid.firmware_info.return_value = None
    hid.read_error = None
    hid.l2 = hid.r2 = 128
    hid.last = (None, None)
    hid.poll_input.return_value = (0.5, 0.5)
    hid.buttons.last_action = -100.
    hid.buttons.drain.return_value = ()
    engine.process.side_effect = lambda l, r, _: (l, r, (float(l.max()), float(r.max())))
    output.dev = {'name': 'test pad'}
    output.failure = None
    src = {'name': 'loop', 'defaultSampleRate': app.RATE, 'maxInputChannels': 2}
    with TemporaryDirectory() as tmp, ExitStack() as ctx:
        for name, value in [('rescan', Mock()), ('foreground_window', lambda: 1), ('foreground_exe', lambda *_: ''),
                            ('HapticOut', Mock(return_value=output)), ('DualSenseHID', Mock(return_value=hid)),
                            ('pa_end', Mock()), ('HapticEngine', Mock(return_value=engine)), ('ROOT', pathlib.Path(tmp))]:
            ctx.enter_context(patch.object(app, name, value))
        ctx.enter_context(patch.object(runner, '_open_loopback', return_value=(Mock(), src, Mock())))
        ctx.enter_context(patch.object(runner.q, 'get', side_effect=get))
        ctx.enter_context(patch.object(app.time, 'monotonic', side_effect=lambda: clock[0]))
        runner.run()
        assert not (pathlib.Path(tmp) / 'last-error.txt').exists(), logs
    effects = [args[0][0][0] for args in hid.set_triggers.call_args_list]
    if suppress:
        assert effects and all(e == app.effect_off()[0] for e in effects), (scene, effects)
    else:
        assert effects[0] == 0x26 and effects[-1] == app.effect_off()[0], effects


def test_audio_reset_and_failed_start_cleanup():
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from unittest.mock import Mock, patch
    import ds_audio
    import ps5_haptics_app as app

    backend = Mock()
    with patch.object(ds_audio, 'pa_new', return_value=backend), patch.object(ds_audio, 'pa_end') as end, \
            patch.object(ds_audio, 'find_dualsense_output', return_value={'index': 1}), \
            patch.object(ds_audio, 'pa_open', side_effect=OSError('endpoint reset')):
        for supplied in (None, backend):
            try:
                ds_audio.HapticOut(supplied)
            except OSError:
                pass
            else:
                raise AssertionError('simulated device loss must fail the open')
        end.assert_called_once_with(backend)

    runner = app.Runner(dict(app.DEFAULTS, capture='系统混音'), lambda _: None)
    stream, reopened, src = Mock(), Mock(), {'name': 'loop', 'defaultSampleRate': 48000, 'maxInputChannels': 2}
    stream.is_active.side_effect = stream.stop_stream.side_effect = OSError('disconnected')
    with patch.object(runner, '_open_stream', side_effect=[OSError('still unavailable'), reopened]):
        assert runner._refresh_stream(stream, backend, src) is None
        assert runner._refresh_stream(None, backend, src) is reopened
        assert runner._refresh_stream(reopened, backend, src) is reopened
    stream.close.assert_called_once()

    tracked = [Mock(), Mock()]
    recovered_out = Mock()
    with patch.object(runner, '_open_loopback', side_effect=[(backend, src, t) for t in tracked]), \
            patch.object(app, 'HapticOut', side_effect=[OSError(-9996, 'Invalid device'), recovered_out]), \
            patch.object(app, 'rescan') as rescan, patch.object(app, 'pa_end') as end, \
            patch.object(runner.stop_ev, 'wait'):
        assert runner._open_audio() == (backend, src, tracked[1], recovered_out)
        assert rescan.call_count == 2
        tracked[0].close.assert_called_once()
        tracked[1].close.assert_not_called()
        end.assert_called_once_with(backend)

    with TemporaryDirectory() as tmp, patch.object(app, 'ROOT', Path(tmp)), \
            patch.object(app, 'foreground_exe', return_value=''), patch.object(app, 'rescan'), \
            patch.object(runner, '_open_loopback', return_value=(backend, src, reopened)), \
            patch.object(app, 'HapticOut', side_effect=RuntimeError('output lost')), patch.object(app, 'pa_end') as end:
        runner.run()
        assert runner.stop_ev.is_set()
        reopened.close.assert_called_once()
        end.assert_called_once_with(backend)


def test_speaker_route_recovers_after_native_audio_goes_quiet():
    from ps5_haptics_app import speaker_route

    assert speaker_route('特殊事件', False, False, True, False) == '游戏原生'
    assert speaker_route('特殊事件', False, False, False, False) == '全部游戏声'
    assert speaker_route('特殊事件', False, False, False, True) == '游戏接管'
    assert speaker_route('特殊事件', True, False, False, False) == '静默'
    assert speaker_route('特殊事件', False, True, False, False) == '特殊事件'
    assert speaker_route('游戏原生', False, False, False, False) == '游戏原生'


def test_game_capture_never_falls_back_to_system_mix():
    from unittest.mock import Mock, patch
    import ps5_haptics_app as app

    runner = app.Runner({'scene': '游戏', 'capture': '自动'}, lambda _: None)
    assert runner.capture_locked and not runner._allow_system_mix()
    assert app.capture_request({'scene': '音乐', 'capture': '自动'}) == ('auto', None)
    assert app.capture_request(dict(capture='锁定进程', capture_exe='C:/Games/GAME.EXE')) == ('pinned', 'game.exe')
    with patch.object(app, 'rescan'), patch.object(app, 'pa_new', return_value='backend'), \
            patch.object(app, 'HapticOut', return_value='haptics'), patch.object(runner, '_open_loopback') as loopback:
        backend, source, stream, output = runner._open_audio()
        assert (backend, source['maxInputChannels'], stream, output) == ('backend', 2, None, 'haptics')
        loopback.assert_not_called()

    runner.capture = ('pinned', 'game.exe')
    runner.proc = Mock()
    runner.proc.sounding.return_value = False
    assert not runner._allow_system_mix()
    runner.capture = ('mix', None)
    assert runner._allow_system_mix()


def test_game_process_sticks_across_alt_tab_and_tracks_new_game():
    from unittest.mock import Mock, patch
    import queue, threading
    import proc_loopback as plb

    first = (100, 'game.exe')
    next_game = (200, 'next.exe')
    foreground = Mock(side_effect=[first, None, next_game, None])
    proc = plb.ProcSource(queue.Queue(), lambda _: None, threading.Event(), foreground)
    request = ('game', ('game.exe', 'next.exe'))
    with patch.object(plb, 'find_pid', side_effect=[first, None, next_game]) as find:
        assert proc.select(request, None, False) == first
        assert proc.select(request, first, True) == first
        assert proc.select(request, first, True) == next_game
        assert proc.select(request, next_game, True) is None
        assert proc.select(('pinned', 'next.exe'), None, False) == next_game
        assert proc.select(('pinned', ''), None, False) is None
        assert [args[0][0] for args in find.call_args_list] == ['game.exe', 'next.exe', 'next.exe']


def test_hid_reader_reconnects_same_path_and_releases_input():
    from unittest.mock import Mock, patch
    import ds_hid

    original = Mock()
    original.read.side_effect = OSError('read error')
    replacement = Mock()
    report = bytearray(ds_hid.NEUTRAL_INPUT)
    report[5] = 120
    replacement.read.side_effect = [[], report]
    received = []

    def forward(data):
        received.append(data)
        if data[5]:
            controller.reader_stop.set()

    with patch.object(ds_hid, 'find_path', return_value=(b'physical', 0x0DF2)), \
            patch.object(ds_hid.hid, 'device', side_effect=[original, replacement]):
        controller = ds_hid.DualSenseHID()
        controller.start_reader(forward)
        controller.reader.join(3)
        assert not controller.reader.is_alive()
        assert received == [ds_hid.NEUTRAL_INPUT, bytes(report)]
        assert controller.read_error is None and controller.l2 == 120
        replacement.open_path.assert_called_once_with(b'physical')
        original.close.assert_called_once()
        controller.close()


def test_hidhide_uses_opened_pad_before_broad_discovery():
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from unittest.mock import Mock, patch
    import ds_hid
    from takeover import Isolation, collection_paths

    prefix = r'\\?\hid#vid_054c&pid_0df2&mi_03&col'
    pad = prefix + r'01#8&aaaa&0&0000#{guid}'
    sibling = prefix + r'02#8&aaaa&0&0001#{guid}'
    other = prefix + r'01#8&bbbb&0&0000#{guid}'
    rows = [{'path': path.encode()} for path in (pad, sibling, other)]
    assert len(collection_paths(pad, rows)) == 2
    iso = Isolation.__new__(Isolation)
    iso.cli = Path(__file__)
    iso.log = Mock()
    iso.receipt = None
    iso.hh = Mock(side_effect=['--cloak-off', ''])
    with TemporaryDirectory() as tmp, patch.object(ds_hid.hid, 'enumerate', return_value=rows):
        iso.path = Path(tmp) / 'receipt.json'
        iso.open(pad)
        assert len(iso.receipt['devices']) == 2
        assert all('aaaa' in item for item in iso.receipt['devices'])
        assert all('--dev-gaming' not in call.args for call in iso.hh.call_args_list)


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


def test_game_music_gate():
    from voice_gate import GameSoundGate
    gate = GameSoundGate()
    rng = np.random.default_rng(403)
    drum = rng.normal(0, .045, 480) * np.exp(-np.arange(480) / 120)
    skill = rng.normal(0, .13, 480)
    outputs = []
    for hop in range(430):
        t = (np.arange(480) + hop * 480) / 48000
        chord = sum(.06 * np.sin(2*np.pi*f*t) for f in (110, 330, 550))
        music = chord + (drum if hop % 40 == 0 else 0)
        impact = skill if hop in (284, 309, 339) else 0.
        # The skill is panned out of phase; a mono detector would lose it.
        outputs.append(gate.gain(music + impact, music - impact, attack=280 <= hop < 350))
    assert max(outputs[80:280]) == 0, 'repeating music leaked'
    assert outputs[284] == outputs[309] == outputs[339] == 1., 'missed an impact over music'
    assert max(outputs[365:]) == 0, 'gate failed to close'


def test_scene_suppression():
    from voice_gate import suppression_enabled
    assert not suppression_enabled({'voice_gate': False})
    for scene in ('游戏', '影院', '音乐'):
        defaults = dict(scene=scene, suppress_background=True)
        assert suppression_enabled(defaults) == (scene == '游戏')
        legacy = dict(scene=scene, scenes={scene: {'voice_gate': False}}, suppress_background=True)
        assert not suppression_enabled(legacy)
        assert suppression_enabled(dict(legacy, scenes={scene: {'suppress_background': True}}))
        test_silent_source_releases_audio_triggers(scene, suppress=True)


def test_button_map():
    from itertools import combinations
    from button_haptics import BUTTONS
    from pad_input import ButtonMap, BUTTON_LABELS
    from takeover import ds4_report, x360_report
    from xds import asb_input
    from ww_bike import BikeInput

    neutral = bytearray(64)
    neutral[0], neutral[8], neutral[10] = 1, 8, 7
    neutral[1:5] = bytes([128]*4)
    neutral[12:] = bytes(range(52))
    def held(key):
        data = bytearray(neutral)
        if key in ('up', 'right', 'down', 'left'):
            data[8] = ('up', 'right', 'down', 'left').index(key)*2
        elif key in ('L2', 'R2'):
            data[5 if key == 'L2' else 6] = 255
            data[9] |= 4 if key == 'L2' else 8
        else:
            byte, bit = BUTTONS[key][:2]
            data[byte] |= bit
        return data
    assert ButtonMap({'R1': 'R2'}).apply(neutral) is neutral
    for a, b in combinations(BUTTON_LABELS, 2):
        mapper = ButtonMap(ButtonMap().swap(a, b))
        for physical, target in ((a,b),(b,a)):
            mapped = mapper.apply(held(physical))
            expect = held(target)
            assert mapped == expect, (physical,target)
            assert asb_input(mapped) == asb_input(expect)
            assert bytes(ds4_report(mapped, 0)) == bytes(ds4_report(expect, 0))
            assert bytes(x360_report(mapped)) == bytes(x360_report(expect))
            assert mapper.apply(neutral) == neutral
    triggers = ButtonMap({'L2':'R2','R2':'L2'})
    press = held('L2'); press[5] = 73
    assert triggers.apply(press)[5:7] == bytes([0,73])
    assert triggers.trigger_effects('left','right') == ('right','left')
    shoulder = ButtonMap({'R1':'R2','R2':'R1'})
    press = held('R2')
    for val, down in ((80,False),(128,True),(105,True),(95,False)):
        press[6] = val
        assert bool(shoulder.apply(press)[9] & 2) == down
    swap = BikeInput()
    assert shoulder.uses_right_shoulder and not ButtonMap().uses_right_shoulder
    assert swap.apply(shoulder.apply(held('R2')), not shoulder.uses_right_shoulder) == held('R1')
    assert shoulder.trigger_effects('left','right') == ('left',None)
    direction = ButtonMap({'cross':'up','up':'cross'})
    diagonal = held('right'); diagonal[8] |= 0x20
    assert direction.apply(diagonal)[8] == 1
    opposed = held('down'); opposed[8] |= 0x20
    assert direction.apply(opposed)[8] == 8


def test_proc_loopback_multi_pid_and_stickiness():
    import queue
    import threading
    from unittest.mock import patch
    import proc_loopback as plb
    import ps5_haptics_app as app

    # Launcher stub (29 MB parent) vs real UE5 game (13 GB child): must pick real game even if stub was prefer_pid.
    matches = [(21892, 'PBZ.exe', 100), (17036, 'PBZ.exe', 21892)]
    ws = {21892: 29 << 20, 17036: 13000 << 20}
    assert plb._select_best_pid(matches, mem_fn=ws.get) == (17036, 'PBZ.exe')
    assert plb._select_best_pid(matches, mem_fn=ws.get, prefer_pid=21892) == (17036, 'PBZ.exe')
    # Anti-cheat child where memory query is denied (None): still prefer child over unprotected stub.
    assert plb._select_best_pid(matches, mem_fn={21892: 29 << 20, 17036: None}.get) == (17036, 'PBZ.exe')
    # Equal-sized workers keep prefer_pid stable.
    workers = [(10, 'w.exe', 1), (20, 'w.exe', 1)]
    assert plb._select_best_pid(workers, mem_fn={10: 50 << 20, 20: 50 << 20}.get, prefer_pid=10) == (10, 'w.exe')

    # ProcSource.select must not ping-pong between 'pbz.exe' (foreground) and 'PBZ.exe' (stub) on focus loss.
    fg_seq = iter([(17036, 'pbz.exe'), None, (21892, 'PBZ.exe'), (17036, 'pbz.exe')])
    src = plb.ProcSource(queue.Queue(), lambda _m: None, threading.Event(), lambda _t: next(fg_seq))
    with patch.object(plb, '_pid_alive', side_effect=lambda p: p == 17036), \
         patch.object(plb, '_process_ws_bytes', side_effect=lambda p: ws.get(p, 0)), \
         patch.object(plb, 'find_pid', side_effect=AssertionError('must not rescan snapshot while current pid is alive')):
        cur = src.select(('game', ('pbz.exe',)), None, False)
        assert cur == (17036, 'pbz.exe')
        assert src.select(('game', ('pbz.exe',)), cur, True) == (17036, 'pbz.exe')  # transient None
        assert src.select(('game', ('pbz.exe',)), cur, True) == (17036, 'pbz.exe')  # small stub dialog focus
        assert src.select(('game', ('pbz.exe',)), cur, True) == (17036, 'pbz.exe')


def test_proc_hot_reattach_and_stick_coalescing():
    import numpy as np
    import queue
    import threading
    from types import SimpleNamespace
    from unittest.mock import patch
    import proc_loopback as plb
    from xds import VirtualDualSense

    # 1) Silent menu must not thrash ProcessLoopback or drop key; stalled audible stream must hot-reattach while keeping key.
    q = queue.Queue()
    stop = threading.Event()
    created = []
    closed = []
    keys_seen = []
    has_audio_flag = [False]
    now_t = [100.0]

    class FakePL:
        def __init__(self, pid):
            self.pid = pid
            created.append(pid)
            self.calls = 0

        def read(self, _ms=20):
            self.calls += 1
            keys_seen.append(src.key)
            if len(created) == 1:
                if self.calls == 1:
                    now_t[0] += 1.0  # > 0.8s while silent menu (has_audio=False): must NOT reattach or drop key
                    return np.zeros(0, np.float32)
                if self.calls == 2:
                    has_audio_flag[0] = True
                    now_t[0] += 1.0  # > 0.8s while game HAS audio: must hot-reattach keeping key
                    return np.zeros(0, np.float32)
            stop.set()
            return np.full(10, 0.1, np.float32)

        def close(self):
            closed.append(self.pid)

    src = plb.ProcSource(q, lambda _m: None, stop, lambda _t: (777, 'pbz.exe'))
    src.request = ('game', ('pbz.exe',))
    with patch.object(plb, 'ProcessLoopback', FakePL), \
         patch.object(plb, '_pid_has_audio', side_effect=lambda _p: has_audio_flag[0]), \
         patch.object(plb, '_pid_alive', return_value=True), \
         patch.object(plb.time, 'monotonic', side_effect=lambda: now_t[0]), \
         patch.object(plb.comtypes, 'CoInitializeEx'), patch.object(plb.comtypes, 'CoUninitialize'):
        src.run()

    assert len(created) == 2 and len(closed) == 2
    assert src.reattach == 1
    assert all(k == (('game', ('pbz.exe',)), 777) for k in keys_seen)
    assert not q.empty()

    # 2) VirtualDualSense: stick-only updates coalesce <2 ms, button/trigger changes flush at once.
    sent = []
    pad = VirtualDualSense()
    pad.sock = SimpleNamespace(sendall=lambda d: sent.append(d))
    rep = bytearray(64)
    rep[0], rep[8] = 1, 8
    rep[1] = 128
    with patch('xds.time.perf_counter', side_effect=[1.000, 1.0005, 1.0025, 1.0028]):
        pad.forward(bytes(rep))          # initial
        rep[1] = 140
        pad.forward(bytes(rep))          # stick delta at 0.5 ms -> coalesced
        rep[1] = 160
        pad.forward(bytes(rep))          # stick delta at 2.5 ms -> sent
        rep[8] |= 0x20                   # button press at +0.3 ms -> sent immediately
        pad.forward(bytes(rep))
    assert len(sent) == 3


def test_v2328_fixes():
    from types import SimpleNamespace
    from unittest.mock import patch
    import deps
    import ps5_haptics_app as app

    # 1) Trigger gear-chatter hysteresis: tiny amp=1 noise doesn't engage, amp>=2 engages and holds 150 ms.
    active, holds = [False, False], [0.0, 0.0]
    amp, active, holds = app.smooth_trigger_amp((1, 0), active, holds, 10.0)
    assert amp == (0, 0) and active == [False, False]
    amp, active, holds = app.smooth_trigger_amp((3, 0), active, holds, 10.05)
    assert amp == (3, 0) and active == [True, False]
    amp, active, holds = app.smooth_trigger_amp((0, 0), active, holds, 10.10)
    assert amp == (1, 0) and active == [True, False]
    amp, active, holds = app.smooth_trigger_amp((0, 0), active, holds, 10.25)
    assert amp == (0, 0) and active == [False, False]

    # 2) usbip OS build check rejects older Win10 (< 19041) cleanly without launching incompatible setup.
    with patch.object(deps.sys, 'getwindowsversion', return_value=SimpleNamespace(build=17763), create=True), \
         patch.object(deps.platform, 'machine', return_value='AMD64'), \
         patch.dict(deps.os.environ, {}, clear=False):
        ok, msg = deps.usbip_supported()
        assert not ok and '19041' in msg
    with patch.object(deps.sys, 'getwindowsversion', return_value=SimpleNamespace(build=26200), create=True), \
         patch.object(deps.platform, 'machine', return_value='AMD64'):
        ok, _ = deps.usbip_supported()
        assert ok


def test_v2329_trigger_fixes():
    import ps5_haptics_app as app
    from ds_hid import DualSenseHID, effect_off, effect_vibration
    import trigger_presets as TP

    # 1) Unpressed trigger with press gate enabled never engages audio vibration, and releasing trigger drops immediately.
    active, holds = [False, False], [0.0, 0.0]
    amp, active, holds = app.smooth_trigger_amp((6, 6), active, holds, 10.0, pressed=(False, False))
    assert amp == (0, 0) and active == [False, False]
    amp, active, holds = app.smooth_trigger_amp((6, 0), active, holds, 10.05, pressed=(True, False))
    assert amp == (6, 0) and active == [True, False]
    amp, active, holds = app.smooth_trigger_amp((6, 0), active, holds, 10.08, pressed=(False, False))
    assert amp == (0, 0) and active == [False, False]

    # 2) Native trigger stabilizer: stops 0x22 <-> 0x05 idle/combat gear thrashing while unpressed, releases at once when pressed.
    bow = bytes.fromhex('2205002400000000000000')
    vib = effect_vibration(6, 40)
    off = effect_off()
    phys = off
    phys = app.stabilize_native_trigger(bow, phys, pressed=False, press_gate=True)
    assert phys == bow
    # Game briefly sends 0x05 during sword swing / idle while finger is off trigger -> stays parked at bow (zero gear movement).
    phys = app.stabilize_native_trigger(off, phys, pressed=False, press_gate=True)
    assert phys == bow
    # Game sends 0x26 vibration while finger is off trigger -> ignored, stays parked at bow.
    phys = app.stabilize_native_trigger(vib, phys, pressed=False, press_gate=True)
    assert phys == bow
    # User presses trigger while game wants 0x05 -> releases immediately.
    phys = app.stabilize_native_trigger(off, phys, pressed=True, press_gate=True)
    assert phys == off
    # While at off, game sends 0x26 while unpressed -> stays off.
    assert app.stabilize_native_trigger(vib, phys, pressed=False, press_gate=True) == off
    assert app.stabilize_native_trigger(vib, phys, pressed=True, press_gate=True) == vib

    # 3) trigger_presets '关闭' preset outputs effect_off() with no sliders.
    assert TP.OFF == '关闭' and TP.uses(TP.OFF) == ()
    assert TP.effect({TP.preset_key('L'): TP.OFF}, 'L') == off

    # 4) DualSenseHID.set_triggers never re-sends identical trigger reports even after >1.0 s unless forced.
    h = DualSenseHID.__new__(DualSenseHID)
    h.dev = FakeDev(); h.lock = threading.Lock(); h.recovering = threading.Event()
    h.last = (None, None); h.last_write = 0.0; h.reader = None
    assert h.set_triggers(off, off) is True
    assert len(h.dev.sent) == 1
    h.last_write -= 5.0
    assert h.set_triggers(off, off) is False
    assert len(h.dev.sent) == 1
    assert h.set_triggers(off, off, force=True) is True
    assert len(h.dev.sent) == 2


def test_v2330_speaker_stability():
    from contextlib import ExitStack
    from tempfile import TemporaryDirectory
    from unittest.mock import Mock, patch
    import ps5_haptics_app as app
    from speaker_safety import SpeakerSafety

    # 1) Catmull-Rom Resampler keeps clean continuity across unity (1.0) and fractional drift ratios.
    rs = app.Resampler()
    t = np.arange(app.RATE * 2) / app.RATE
    sig = (0.4 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
    ratios = [1.0, 1.0, 0.992, 0.988, 1.0, 1.015, 1.02, 1.0]
    outs = []
    for k in range(80):
        blk = sig[k * app.HOP:(k + 1) * app.HOP]
        l, _ = rs.process(blk, blk, ratios[(k // 10) % len(ratios)])
        outs.append(l)
    joined = np.concatenate(outs)
    expected_step = 0.4 * 2 * np.pi * 1000 / app.RATE
    max_step = float(np.max(np.abs(np.diff(joined[10:]))))
    assert max_step < expected_step * 1.35, (max_step, expected_step)
    # Short single-sample block under 2x ratio must not raise or go out of bounds.
    rs_short = app.Resampler()
    s1, _ = rs_short.process(np.array([0.25], np.float32), np.array([0.25], np.float32), 2.0)
    assert len(s1) <= 1

    # 2) Bidirectional drift_speed controller pulls back < 20 ms, speeds up > 40 ms, and snaps cleanly in deadband.
    s = 1.0
    for _ in range(100):
        s = app.drift_speed(0.005, s, has_ring=True)
    assert 0.984 <= s <= 0.990, s
    assert app.drift_speed(0.005, 1.0, has_ring=False) == 1.0
    for _ in range(100):
        s = app.drift_speed(0.070, s, has_ring=True)
    assert 1.010 <= s <= 1.026, s
    s = 1.002
    for _ in range(60):
        s = app.drift_speed(0.030, s, has_ring=True)
    assert s == 1.0, s

    # 3) Runner in 原生透传 does NOT spam hid.enable_speaker every 5 s, but re-asserts on HID reopen.
    logs = []
    cfg = dict(app.DEFAULTS, mode='原生透传', capture='系统', speaker_mode='全部游戏声', source_hint='loop',
               pbz_fx=False, ww_fx=False, ww_vision=False, record_native=False)
    runner = app.Runner(cfg, logs.append)
    clock = [100.0]

    hid, engine, vpad, iso = Mock(), Mock(), Mock(), Mock()
    hid.firmware_info.return_value = None
    hid.read_error = None
    hid.l2 = hid.r2 = 0
    hid.last = (None, None)
    hid.reopen_count = 0
    hid.poll_input.return_value = (0.0, 0.0)
    hid.buttons.last_action = -100.0
    hid.buttons.drain.return_value = ()
    engine.process.side_effect = lambda l, r, _: (l, r, (0.0, 0.0))
    vpad.feedback.return_value = ((0, 0), False, None)
    vpad.wave.pull.return_value = (np.zeros(app.HOP, np.float32), np.zeros(app.HOP, np.float32))
    vpad.triggers.return_value = (None, None, 0)
    vpad.spk_peak = 0
    vpad.frames = {'in': 0, 1: 0, 3: 0}
    vpad.trigger_press_times = (None, None)

    class FakeOut:
        dev = {'name': 'test pad'}
        failure = None
        xruns = 0
        overruns = 0
        _use_ring = True
        speaker_safety = SpeakerSafety()
        def buffered_s(self): return 0.030
        def feed(self, *a, **kw): pass
        def close(self): pass

    def get(timeout):
        clock[0] += 0.2
        if clock[0] >= 107.0 and hid.reopen_count == 0:
            hid.reopen_count = 1
        if clock[0] >= 112.0:
            runner.stop_ev.set()
        return np.zeros(2 * app.HOP, np.float32)

    src = {'name': 'loop', 'defaultSampleRate': app.RATE, 'maxInputChannels': 2}
    with TemporaryDirectory() as tmp, ExitStack() as ctx:
        for name, value in [('rescan', Mock()), ('foreground_window', lambda: 1), ('foreground_exe', lambda *_: ''),
                            ('HapticOut', Mock(return_value=FakeOut())), ('DualSenseHID', Mock(return_value=hid)),
                            ('VirtualDualSense', Mock(return_value=vpad)), ('Isolation', Mock(return_value=iso)),
                            ('pa_end', Mock()), ('HapticEngine', Mock(return_value=engine)), ('ROOT', pathlib.Path(tmp))]:
            ctx.enter_context(patch.object(app, name, value))
        ctx.enter_context(patch.object(runner, '_open_loopback', return_value=(Mock(), src, Mock())))
        ctx.enter_context(patch.object(runner.q, 'get', side_effect=get))
        ctx.enter_context(patch.object(app.time, 'monotonic', side_effect=lambda: clock[0]))
        runner.run()
        assert not (pathlib.Path(tmp) / 'last-error.txt').exists(), logs
    # Expect: 1 on startup + 1 when HID reopens at 7 s (112s-100s=12s would be 3-4 times under old 5s loop).
    assert hid.enable_speaker.call_count == 2, hid.enable_speaker.call_count


if __name__ == '__main__':
    test_scene_suppression()
    test_proc_loopback_multi_pid_and_stickiness()
    test_proc_hot_reattach_and_stick_coalescing()
    test_v2328_fixes()
    test_v2329_trigger_fixes()
    test_v2330_speaker_stability()
    test_button_map()
    test_game_music_gate()
    test_silent_source_releases_audio_triggers()
    test_audio_reset_and_failed_start_cleanup()
    test_speaker_route_recovers_after_native_audio_goes_quiet()
    test_game_capture_never_falls_back_to_system_mix()
    test_game_process_sticks_across_alt_tab_and_tracks_new_game()
    test_hid_reader_reconnects_same_path_and_releases_input()
    test_hidhide_uses_opened_pad_before_broad_discovery()
    test_equal_peak_wave_changes()
    test_native_motor_mode_switch()
    test_hidhide_output(); test_instance_path(); test_mapping(); test_battery(); test_controls(); test_input_filter(); test_pad_commands(); test_volume(); test_game_caps(); test_edge_is_found(); test_tap_buffer(); test_fuse_modes(); test_presets(); test_next_step(); test_voice_gate(); test_fine_profile(); test_hidhide_inverse(); test_receiver_paths(); test_trigger_presets(); test_selfcheck_report(); test_game_profiles(); test_steam_app_for(); test_notes_url(); test_deps_checks(); test_stuck_guard()
    print('offline tests ok')
    if '--live' in sys.argv:
        test_live()
        print('live test ok')

