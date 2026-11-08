"""Takeover mode: the game talks to a virtual DS4 (ViGEm), the real DualSense is hidden from
it (HidHide) and driven only by us. Game rumble from the virtual pad -> voice-coil waveform,
fused with audio haptics; game lightbar colour is forwarded. No game injection.

DualSense USB input report 0x01 (offsets incl. report id): 1 LX 2 LY 3 RX 4 RY 5 L2 6 R2,
8 hat|face, 9 L1 R1 L2 R2 create options L3 R3, 10 PS touch mute, 16..21 gyro, 22..27 accel,
28..31 timestamp (1/3 us), 33..40 two touch points. DS4 button bits are laid out the same way,
gyro/accel resolution matches (1024 per deg/s, 8192 per g).
"""
import ctypes as C
import json
import re
import math
import pathlib
import subprocess
import sys
import threading
import numpy as np
from haptic_dsp import RATE, HOP

ROOT = pathlib.Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else pathlib.Path(__file__).resolve().parent
OK = 0x20000000
NOWIN = 0x08000000


class Lightbar(C.Structure):
    _fields_ = [('r', C.c_ubyte), ('g', C.c_ubyte), ('b', C.c_ubyte)]


class DS4ReportEx(C.Structure):
    _fields_ = [('buf', C.c_ubyte * 63)]


DS4_CB = C.WINFUNCTYPE(None, C.c_void_p, C.c_void_p, C.c_ubyte, C.c_ubyte, Lightbar, C.c_void_p)


def ds4_report(r, counter):
    """DualSense input report (64 bytes, id first) -> DS4_REPORT_EX buffer (63 bytes)."""
    out = DS4ReportEx(); b = out.buf
    b[0], b[1], b[2], b[3] = r[1], r[2], r[3], r[4]
    b[4] = r[8]; b[5] = r[9]                 # hat + face, shoulders/sticks/create/options
    b[6] = (r[10] & 0x03) | ((counter & 0x3F) << 2)
    b[7], b[8] = r[5], r[6]
    ts = int.from_bytes(r[28:32], 'little') // 16 & 0xFFFF   # 1/3 us -> 5.33 us units
    b[9], b[10] = ts & 0xFF, ts >> 8
    b[11] = 0xFF
    for i in range(12):                      # gyro xyz, accel xyz (int16 LE, same resolution)
        b[12 + i] = r[16 + i]
    b[29] = 0x1B                             # cable + full
    b[32] = 1                                # one touch packet
    b[33] = counter & 0xFF
    for k in range(2):
        p = r[33 + 4 * k: 37 + 4 * k]
        x = p[1] | ((p[2] & 0x0F) << 8); y = (p[2] >> 4) | (p[3] << 4)
        y = min(942, y)
        o = 34 + 4 * k
        b[o] = p[0]; b[o + 1] = x & 0xFF; b[o + 2] = (x >> 8) | ((y & 0x0F) << 4); b[o + 3] = y >> 4
    return out


class VirtualDS4:
    def __init__(self):
        dll_path = ROOT / 'bin' / 'ViGEmClient.dll'
        self.dll = C.CDLL(str(dll_path))
        specs = [('vigem_alloc', [], C.c_void_p), ('vigem_free', [C.c_void_p], None),
                 ('vigem_connect', [C.c_void_p], C.c_uint), ('vigem_disconnect', [C.c_void_p], None),
                 ('vigem_target_ds4_alloc', [], C.c_void_p), ('vigem_target_free', [C.c_void_p], None),
                 ('vigem_target_add', [C.c_void_p, C.c_void_p], C.c_uint),
                 ('vigem_target_remove', [C.c_void_p, C.c_void_p], C.c_uint),
                 ('vigem_target_ds4_update_ex_ptr', [C.c_void_p, C.c_void_p, C.POINTER(DS4ReportEx)], C.c_uint),
                 ('vigem_target_ds4_register_notification', [C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p], C.c_uint),
                 ('vigem_target_ds4_unregister_notification', [C.c_void_p], None)]
        for name, args, ret in specs:
            fn = getattr(self.dll, name); fn.argtypes = args; fn.restype = ret
        self.client = self.target = None; self.attached = self.registered = False
        self.cb = DS4_CB(self._feedback)
        self.lock = threading.Lock()
        self.large = self.small = 0; self.color = None; self.updates = 0; self.feedbacks = 0
        self.counter = 0

    def _check(self, st, what):
        if st != OK:
            raise RuntimeError(f'虚拟手柄 {what} 失败（ViGEm 0x{st:08x}）')

    def open(self):
        try:
            self.client = self.dll.vigem_alloc()
            if not self.client:
                raise RuntimeError('ViGEm 初始化失败')
            self._check(self.dll.vigem_connect(self.client), '连接驱动')
            self.target = self.dll.vigem_target_ds4_alloc()
            self._check(self.dll.vigem_target_add(self.client, self.target), '创建')
            self.attached = True
            self._check(self.dll.vigem_target_ds4_register_notification(self.client, self.target, C.cast(self.cb, C.c_void_p), None), '震动回调')
            self.registered = True
        except Exception:
            self.close(); raise

    def _feedback(self, client, target, large, small, color, user):
        with self.lock:
            self.large, self.small = int(large), int(small); self.feedbacks += 1
            self.color = (int(color.r), int(color.g), int(color.b))

    def rumble(self):
        with self.lock:
            return self.large / 255, self.small / 255, self.color

    def forward(self, report):
        self.counter = (self.counter + 1) & 0xFF
        rep = ds4_report(report, self.counter)
        self.dll.vigem_target_ds4_update_ex_ptr(self.client, self.target, C.byref(rep))
        self.updates += 1

    def close(self):
        if self.registered:
            self.dll.vigem_target_ds4_unregister_notification(self.target); self.registered = False
        if self.attached:
            self.dll.vigem_target_remove(self.client, self.target); self.attached = False
        if self.target:
            self.dll.vigem_target_free(self.target); self.target = None
        if self.client:
            self.dll.vigem_disconnect(self.client); self.dll.vigem_free(self.client); self.client = None


DEV_PATTERN = r'^(HID|USB)\\VID_054C&PID_(0CE6|0DF2)&MI_03\\'


class Isolation:
    """Hide the real DualSense HID interface from every app except this process (HidHide).
    A receipt file records exactly what we changed; Restore-Visibility.ps1 undoes it after a crash."""

    def __init__(self, log):
        self.log = log; self.receipt = None; self.path = ROOT / 'isolation-restore.json'
        from deps import hidhide_cli
        self.cli = hidhide_cli()

    def hh(self, *args):
        # HidHideCLI occasionally stalls while Windows re-enumerates the pad: short timeout, retry.
        # Every command we send is idempotent (hide/unhide/reg/unreg/cloak), so a retry is safe.
        for attempt in range(4):
            try:
                r = subprocess.run([str(self.cli), *args], capture_output=True, text=True, creationflags=NOWIN, timeout=6, stdin=subprocess.DEVNULL)
                break
            except subprocess.TimeoutExpired:
                if attempt == 3:
                    raise RuntimeError('HidHide 没有响应（重试 4 次）')
        if r.returncode:
            raise RuntimeError('HidHide 命令失败：' + (r.stderr or r.stdout).strip()[:200])
        return r.stdout

    def open(self):
        if self.path.exists():
            self.log('发现上次未恢复的隐藏记录，先恢复…'); self.restore_receipt(json.loads(self.path.read_text('utf8')))
        if not self.cli.exists():
            raise RuntimeError('没有安装 HidHide，无法接管')
        before = self.hh('--inv-state', '--cloak-state', '--dev-list', '--app-list')
        if '--inv-on' in before:
            raise RuntimeError('HidHide 处于反向模式，不支持；未做任何修改')
        devs = []
        for grp in json.loads(self.hh('--dev-gaming') or '[]'):      # HidHide's own device list: fast
            for d in grp.get('devices', []):
                path = d.get('deviceInstancePath', '')
                if d.get('present') and re.match(DEV_PATTERN, path, re.I):
                    devs.append(path)
        if not any(d.upper().startswith('HID\\') for d in devs):
            raise RuntimeError('没找到 USB 连接的 PS5 手柄 HID 设备')
        apps = []
        for exe in {sys.executable, getattr(sys, '_base_executable', sys.executable)}:
            p = str(pathlib.Path(exe).resolve())
            if p.lower() not in before.lower():
                apps.append(p)
        own = [d for d in devs if d.lower() not in before.lower()]
        self.receipt = {'cli': str(self.cli), 'devices': own, 'apps': apps, 'restore_cloak_off': '--cloak-off' in before}
        self.path.write_text(json.dumps(self.receipt, ensure_ascii=False, indent=2), 'utf8')
        args = []
        for a in apps:
            args += ['--app-reg', a]
        for d in own:
            args += ['--dev-hide', d]
        self.hh(*args, '--cloak-on')
        self.log(f'已对游戏隐藏真手柄（{len(own)} 个接口）')

    def restore_receipt(self, rc):
        args = []
        for d in rc.get('devices', []):
            args += ['--dev-unhide', d]
        for a in rc.get('apps', []):
            args += ['--app-unreg', a]
        if rc.get('restore_cloak_off'):
            args += ['--cloak-off']
        if args:
            self.hh(*args)
        self.path.unlink(missing_ok=True)

    def close(self):
        if self.receipt:
            self.restore_receipt(self.receipt); self.receipt = None
            self.log('已恢复真手柄的可见性')


class RumbleCoil:
    """Xbox/DS4-style two-motor rumble -> voice-coil waveforms.
    Large (left) motor -> 55 Hz body, small (right) motor -> 160 Hz buzz; each grip gets
    mostly its own motor plus a bit of the other, like a real pad's shell coupling."""

    def __init__(self):
        self.f = np.array([55.0, 160.0]); self.phase = np.zeros(2)
        self.prev_l = np.zeros(2); self.prev_r = np.zeros(2)

    def step(self, large, small, gain=1.0):
        al = np.array([large, 0.35 * small]) * gain
        ar = np.array([0.35 * large, small]) * gain
        n = np.arange(HOP)
        ph = self.phase[:, None] + 2 * np.pi * self.f[:, None] * n[None, :] / RATE
        s = np.sin(ph)
        t = (n / HOP)[None, :]
        wl = np.sum((self.prev_l[:, None] + (al - self.prev_l)[:, None] * t) * s, axis=0)
        wr = np.sum((self.prev_r[:, None] + (ar - self.prev_r)[:, None] * t) * s, axis=0)
        self.phase = (self.phase + 2 * np.pi * self.f * HOP / RATE) % (2 * math.pi)
        self.prev_l, self.prev_r = al, ar
        return wl.astype(np.float32), wr.astype(np.float32)


def fuse(audio_l, audio_r, rum_l, rum_r, level, mode, duck_depth=0.5):
    """mode: 融合 / 仅游戏震动 / 仅声音触感. In fusion the game rumble leads and audio
    haptics are ducked while it plays (native first)."""
    if mode == '仅游戏震动':
        l, r = rum_l, rum_r
    elif mode == '仅声音触感':
        l, r = audio_l, audio_r
    else:
        duck = 1.0 - duck_depth * min(1.0, level * 2)
        l, r = audio_l * duck + rum_l, audio_r * duck + rum_r
    return np.tanh(l).astype(np.float32), np.tanh(r).astype(np.float32)
