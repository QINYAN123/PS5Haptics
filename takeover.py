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


class XUSBReport(C.Structure):
    _fields_ = [('wButtons', C.c_ushort), ('bLeftTrigger', C.c_ubyte), ('bRightTrigger', C.c_ubyte),
                ('sThumbLX', C.c_short), ('sThumbLY', C.c_short), ('sThumbRX', C.c_short), ('sThumbRY', C.c_short)]


X360_CB = C.WINFUNCTYPE(None, C.c_void_p, C.c_void_p, C.c_ubyte, C.c_ubyte, C.c_ubyte, C.c_void_p)
_HAT = {0: 0x1, 1: 0x9, 2: 0x8, 3: 0xA, 4: 0x2, 5: 0x6, 6: 0x4, 7: 0x5}   # DualSense hat -> XInput up 1 down 2 left 4 right 8


def _axis(v, invert=False):
    a = (int(v) - 128) * 32767 // 127
    return max(-32768, min(32767, -a if invert else a))


def x360_report(r):
    """DualSense input report (id first) -> XInput state. Cross = A, circle = B, square = X, triangle = Y,
    create or touchpad click = Back, options = Start, PS = Guide."""
    b = _HAT.get(r[8] & 0x0F, 0)
    face, shoulder, misc = r[8], r[9], r[10]
    for bit, x in ((0x10, 0x4000), (0x20, 0x1000), (0x40, 0x2000), (0x80, 0x8000)):    # square cross circle triangle
        if face & bit: b |= x
    for bit, x in ((0x01, 0x0100), (0x02, 0x0200), (0x10, 0x0020), (0x20, 0x0010), (0x40, 0x0040), (0x80, 0x0080)):   # L1 R1 create options L3 R3
        if shoulder & bit: b |= x
    if misc & 0x01: b |= 0x0400                 # PS -> Guide
    if misc & 0x02: b |= 0x0020                 # touchpad click -> Back
    return XUSBReport(b, r[5], r[6], _axis(r[1]), _axis(r[2], True), _axis(r[3]), _axis(r[4], True))


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


class VirtualX360(VirtualDS4):
    """For games that only accept Xbox controllers: same plumbing as the DS4, XInput reports, no light bar."""

    def __init__(self):
        super().__init__()
        d = self.dll
        d.vigem_target_x360_alloc.argtypes = []; d.vigem_target_x360_alloc.restype = C.c_void_p
        d.vigem_target_x360_update.argtypes = [C.c_void_p, C.c_void_p, XUSBReport]; d.vigem_target_x360_update.restype = C.c_uint
        d.vigem_target_x360_register_notification.argtypes = [C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p]
        d.vigem_target_x360_register_notification.restype = C.c_uint
        d.vigem_target_x360_unregister_notification.argtypes = [C.c_void_p]; d.vigem_target_x360_unregister_notification.restype = None
        self.cb = X360_CB(self._feedback_x360)

    def open(self):
        try:
            self.client = self.dll.vigem_alloc()
            if not self.client:
                raise RuntimeError('ViGEm 初始化失败')
            self._check(self.dll.vigem_connect(self.client), '连接驱动')
            self.target = self.dll.vigem_target_x360_alloc()
            self._check(self.dll.vigem_target_add(self.client, self.target), '创建')
            self.attached = True
            self._check(self.dll.vigem_target_x360_register_notification(self.client, self.target, C.cast(self.cb, C.c_void_p), None), '震动回调')
            self.registered = True
        except Exception:
            self.close(); raise

    def _feedback_x360(self, client, target, large, small, led, user):
        with self.lock:
            self.large, self.small = int(large), int(small); self.feedbacks += 1

    def forward(self, report):
        self.dll.vigem_target_x360_update(self.client, self.target, x360_report(report))
        self.updates += 1

    def close(self):
        if self.registered:
            self.dll.vigem_target_x360_unregister_notification(self.target); self.registered = False
        super().close()


DEV_PATTERN = r'^(HID|USB)\\VID_054C&PID_(0CE6|0DF2)&MI_03\\'


def _decode_cli(raw):
    """HidHideCLI prints UTF-8 or the system code page depending on the machine; never fail on it."""
    for enc in ('utf-8', 'mbcs'):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            pass
    return raw.decode('utf-8', 'replace')


def gaming_pad_paths(text):
    """PS5 controller instance paths from `HidHideCLI --dev-gaming`.
    On some PCs the output stops halfway through a device description (seen as JSON error "Unterminated
    string ... line 34 column 17"), probably at a character the CLI can't print. Keep what did arrive."""
    try:
        groups = json.loads(text or '[]')
        found = [d.get('deviceInstancePath', '') for g in groups for d in g.get('devices', []) if d.get('present')]
    except ValueError:
        found = []
        for block in re.split(r'"present"\s*:', text)[1:]:
            m = re.search(r'"deviceInstancePath"\s*:\s*"((?:[^"\\]|\\.)*)"', block)
            if m and block.lstrip().startswith('true'):
                found.append(m.group(1).replace('\\\\', '\\'))
    return [p for p in found if re.match(DEV_PATTERN, p, re.I)]


def instance_path(hid_path):
    r"""hidapi path \\?\hid#vid_054c&pid_0ce6&mi_03#7&3a227958&0&0000#{guid} -> HID\VID_054C&PID_0CE6&MI_03\7&3a227958&0&0000"""
    if isinstance(hid_path, bytes):
        hid_path = hid_path.decode('ascii', 'ignore')
    m = re.match(r'^\\\\\?\\(hid)#(vid_054c&pid_[0-9a-f]{4}(?:&mi_[0-9a-f]{2})?)#([^#]+)#', hid_path or '', re.I)
    return f'{m.group(1).upper()}\\{m.group(2).upper()}\\{m.group(3).lower()}' if m else None


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
                r = subprocess.run([str(self.cli), *args], capture_output=True, creationflags=NOWIN, timeout=6, stdin=subprocess.DEVNULL)
                break
            except subprocess.TimeoutExpired:
                if attempt == 3:
                    raise RuntimeError('HidHide 没有响应（重试 4 次）')
        out = _decode_cli(r.stdout); err = _decode_cli(r.stderr)
        if r.returncode:
            raise RuntimeError('HidHide 命令失败：' + (err or out).strip()[:200])
        return out

    def open(self, pad_path=None):
        """pad_path: hidapi path of the real pad we already opened, so a damaged HidHide list can't stop us."""
        if self.path.exists():
            self.log('发现上次未恢复的隐藏记录，先恢复…')
            try:
                self.restore_receipt(json.loads(self.path.read_text('utf8')))
            except ValueError:                                       # half-written receipt: nothing usable in it
                self.log('上次的隐藏记录已损坏，已忽略')
                self.path.unlink(missing_ok=True)
        if not self.cli.exists():
            raise RuntimeError('没有安装 HidHide，无法接管')
        before = self.hh('--inv-state', '--cloak-state', '--dev-list', '--app-list')
        if '--inv-on' in before:
            raise RuntimeError('HidHide 处于反向模式，不支持；未做任何修改')
        devs = gaming_pad_paths(self.hh('--dev-gaming'))             # HidHide's own device list: fast
        own_path = instance_path(pad_path)
        if own_path and own_path.lower() not in (d.lower() for d in devs):
            devs.append(own_path)
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


def repair_pad(log=print):
    """「修复手柄」: after a crash the real DualSense can stay hidden from every program (HidHide) or a
    leftover virtual pad can confuse games. Undo all of it. Only touches PS5 controllers; other tools'
    HidHide entries stay as they are. Returns a short summary."""
    done = []
    iso = Isolation(log)
    rp = iso.path
    if rp.exists():                                         # 1) exactly what we changed last time
        try:
            iso.restore_receipt(json.loads(rp.read_text('utf8'))); done.append('已撤销上次的隐藏记录')
        except Exception as exc:
            log('撤销隐藏记录失败：' + str(exc))
    if iso.cli.exists():                                    # 2) any PS5 controller still hidden (record lost)
        out = iso.hh('--dev-list')
        hidden = [m.group(1) for m in re.finditer(r'--dev-hide "([^"]+)"', out)]
        pads = [d for d in hidden if re.search(r'VID_054C&PID_(0CE6|0DF2)', d, re.I)]     # DualSense / DualSense Edge
        for d in pads:
            iso.hh('--dev-unhide', d)
        if pads:
            done.append(f'取消隐藏 {len(pads)} 个手柄接口')
        if not [d for d in hidden if d not in pads]:           # 3) cloak off only if nothing else is hidden
            iso.hh('--cloak-off')
    r = subprocess.run(['taskkill', '/F', '/IM', 'VIIPERHost.exe'], capture_output=True, text=True, creationflags=NOWIN)
    if r.returncode == 0:                                   # 4) leftover virtual-pad host from a crash
        done.append('关闭了残留的虚拟手柄')
    return '、'.join(done) if done else '没有发现需要修复的地方'


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


FUSION_ALIAS = {'融合': '叠加', '仅声音触感': '替换', '仅游戏震动': '纯用游戏'}    # names used before 2.1.2


def fuse(audio_l, audio_r, rum_l, rum_r, level, mode, duck_depth=0.5):
    """mode: 叠加 / 替换 (ours only) / 纯用游戏. When layering, the game rumble leads and audio
    haptics are ducked while it plays (native first)."""
    mode = FUSION_ALIAS.get(mode, mode)
    if mode == '纯用游戏':
        l, r = rum_l, rum_r
    elif mode == '替换':
        l, r = audio_l, audio_r
    else:
        duck = 1.0 - duck_depth * min(1.0, level * 2)
        l, r = audio_l * duck + rum_l, audio_r * duck + rum_r
    return np.tanh(l).astype(np.float32), np.tanh(r).astype(np.float32)
