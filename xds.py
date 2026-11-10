"""Virtual DualSense (with USB audio) - client side.

The virtual pad itself is created by VIIPERHost (viiper_host/, GPL-3.0), a separate process that
owns libVIIPER + usbip-win2. This module never loads libVIIPER: it starts VIIPERHost and talks to
it over a local TCP socket (see viiper_host.py for the protocol), so PS5声音触感 stays an
independent program under its own license.

Feedback frames forwarded by the host: [type u8][len u16][payload]
  type 1 HID output (30 B): enable1, enable2, rumbleRight, rumbleLeft, enable3,
         rightTrigger[11], leftTrigger[11], lightbar r g b
  type 2 audio energy (16 B)   type 3 haptic waveform (64 B): seq u32, 8x(L i16, R i16) at 1 kHz
"""
import collections
import os
import pathlib
import socket
import struct
import subprocess
import sys
import threading
import numpy as np

ROOT = pathlib.Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else pathlib.Path(__file__).resolve().parent


HAT_TO_DPAD = {0: 1, 1: 1 | 8, 2: 8, 3: 2 | 8, 4: 2, 5: 2 | 4, 6: 4, 7: 1 | 4}


def asb_input(r, waveform=True):
    """Real DualSense USB input report (64 B, id first) -> ApexSenseBridge 33-byte state."""
    b = bytearray(33)
    b[0:6] = bytes(r[1:7])                       # LX LY RX RY (raw, 128 = centre), L2, R2
    b[6] = HAT_TO_DPAD.get(r[8] & 0x0F, 0)
    legacy = (r[8] & 0xF0) | (r[9] << 8)
    legacy |= (0x0001 if r[10] & 0x01 else 0) | (0x0002 if r[10] & 0x02 else 0) | (0x0004 if r[10] & 0x04 else 0)
    struct.pack_into('<H', b, 7, legacy)
    for k, (xo, ao) in enumerate(((9, 13), (14, 18))):
        p = r[33 + 4 * k: 37 + 4 * k]
        x = p[1] | ((p[2] & 0x0F) << 8); y = (p[2] >> 4) | (p[3] << 4)
        struct.pack_into('<HH', b, xo, x, y); b[ao] = 0 if p[0] & 0x80 else 1
    b[19:31] = bytes(r[16:28])                   # gyro xyz, accel xyz (int16 LE)
    b[32] = 0x80 if waveform else 0
    return bytes(b)


class WaveBuffer:
    """1 kHz haptic waveform frames -> 48 kHz blocks with a small jitter buffer."""

    def __init__(self, target=24, maxlen=200):
        self.q = collections.deque(maxlen=maxlen); self.lock = threading.Lock()
        self.target = target; self.primed = False; self.last = (0.0, 0.0); self.frac = 0.0

    def push(self, left, right):
        with self.lock:
            for l, r in zip(left, right):
                self.q.append((l / 32768.0, r / 32768.0))

    def pull(self, n48):
        """Return (left, right) float32 arrays of n48 samples."""
        need = n48 / 48.0
        with self.lock:
            if not self.primed:
                if len(self.q) < self.target // 2:
                    return np.zeros(n48, np.float32), np.zeros(n48, np.float32)
                self.primed = True
            if len(self.q) > self.target * 3:               # drift / burst: drop to target latency
                for _ in range(len(self.q) - self.target):
                    self.q.popleft()
            k = int(self.frac + need)
            pts = [self.last]
            for _ in range(k):
                if self.q:
                    pts.append(self.q.popleft())
                else:                                       # underrun: decay to zero
                    pts.append((pts[-1][0] * 0.5, pts[-1][1] * 0.5)); self.primed = len(self.q) > 0
            self.frac = self.frac + need - k
        self.last = pts[-1]
        arr = np.array(pts, np.float32)
        if len(arr) < 2:
            return np.full(n48, arr[0, 0], np.float32), np.full(n48, arr[0, 1], np.float32)
        xs = np.linspace(0, len(arr) - 1, n48)
        return (np.interp(xs, np.arange(len(arr)), arr[:, 0]).astype(np.float32),
                np.interp(xs, np.arange(len(arr)), arr[:, 1]).astype(np.float32))


USBIP_HELP = ('usbip-win2 驱动没就绪，虚拟 PS5 手柄建不起来：刚装完驱动的话请先重启电脑；还不行就到「系统」页点「一键安装依赖」重装。'
              '急着玩可以先把连接方式换成「接管」或「虚拟 Xbox」')


def host_command():
    """VIIPERHost.exe next to the app (bin/), or the script when running from source."""
    for d in (ROOT / 'bin', ROOT):
        if (d / 'VIIPERHost.exe').exists():
            return [str(d / 'VIIPERHost.exe')]
    return [sys.executable, str(pathlib.Path(__file__).resolve().parent / 'viiper_host' / 'viiper_host.py')]


class VirtualDualSense:
    def __init__(self, log=print):
        self.log = log
        self.proc = None; self.sock = None; self.send_lock = threading.Lock(); self.reader = None
        self.lock = threading.Lock()
        self.right_effect = None; self.left_effect = None; self.trigger_seq = 0
        self.rumble = (0, 0); self.compat_rumble = False; self.color = None
        self.wave = WaveBuffer(); self.frames = collections.Counter(); self.logs = collections.deque(maxlen=50)
        # the game's OWN controller-speaker channels (front L/R of the pad's audio): VIIPER measures their
        # peak before decimating the haptic channels. Games with native pad-speaker support (e.g. 影之刃零)
        # send their authored speaker sounds there.
        self.spk_peak = 0; self.spk_frames = 0
        self.gate = {'L': None, 'R': None}; self._gate_down = {'L': False, 'R': False}
        self.ready = threading.Event(); self.error = None

    def _on_raw(self, data):
        try:
            t = data[0]; n = data[1] | (data[2] << 8); p = data[3:3 + n]
            self.frames[t] += 1
            if t == 1 and len(p) >= 30:
                e1, e2 = p[0], p[1]
                with self.lock:
                    if e1 & 0x04: self.right_effect = bytes(p[5:16]); self.trigger_seq += 1
                    if e1 & 0x08: self.left_effect = bytes(p[16:27]); self.trigger_seq += 1
                    if e1 & 0x03:
                        self.rumble = (p[3], p[2]); self.compat_rumble = (e1 & 0x02) == 0 or (e1 & 0x01) != 0
                    if e2 & 0x04: self.color = (p[27], p[28], p[29])
            elif t == 3 and len(p) >= 36:
                vals = struct.unpack_from('<16h', p, 4)
                self.wave.push(vals[0::2], vals[1::2])
                if len(p) >= 40:
                    pk = max(struct.unpack_from('<2H', p, 36))
                    if pk > self.spk_peak: self.spk_peak = pk
                    if pk > 300: self.spk_frames += 1          # ~-40 dBFS: the game is really using the pad speaker
        except Exception:
            pass

    def _recv_exact(self, n):
        b = b''
        while len(b) < n:
            c = self.sock.recv(n - len(b))
            if not c:
                raise ConnectionError('VIIPERHost 已退出')
            b += c
        return b

    def _read_loop(self):
        try:
            while True:
                kind = self._recv_exact(1); n = struct.unpack('<H', self._recv_exact(2))[0]
                payload = self._recv_exact(n) if n else b''
                if kind == b'R':
                    self._on_raw(payload)
                elif kind == b'L':
                    self.logs.append((payload[0] if payload else 0, payload[1:].decode('utf8', 'replace')))
                elif kind == b'O':
                    self.ready.set()
                elif kind == b'E':
                    self.error = payload.decode('utf8', 'replace'); self.ready.set(); return
        except Exception as exc:
            if not self.ready.is_set():
                self.error = str(exc); self.ready.set()

    def open(self):
        env = dict(os.environ)
        try:                                       # usbip-win2 installed somewhere else: let VIIPER's fallback find usbip.exe
            import deps
            exe = deps.usbip_exe()
            if exe is not None:
                env['PATH'] = str(exe.parent) + os.pathsep + env.get('PATH', '')
        except Exception:
            pass
        self.proc = subprocess.Popen(host_command() + [str(os.getpid())], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     stdin=subprocess.DEVNULL, creationflags=0x08000000, cwd=str(ROOT), env=env)
        line = self.proc.stdout.readline().decode('ascii', 'replace').split()
        if len(line) != 2 or line[0] != 'PORT':
            self.close(); raise RuntimeError('虚拟 PS5 手柄宿主程序（VIIPERHost）没有启动')
        self.sock = socket.create_connection(('127.0.0.1', int(line[1])), timeout=10)
        self.sock.settimeout(None); self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.reader = threading.Thread(target=self._read_loop, daemon=True); self.reader.start()
        if not self.ready.wait(15) or self.error:
            err = self.error or '虚拟 PS5 手柄创建超时'
            if 'usbip' in err.lower():             # say what to do, keep the technical detail for bug reports
                err = (USBIP_HELP + '（详细：' + err[:240] + '）')
            self.close(); raise RuntimeError(err)

    # gate[side] = raw threshold (0..255) or None. While set, the game sees that trigger as fully
    # released until the physical trigger passes the threshold (the adaptive "click" point), then
    # fully pressed - so a ready skill fires exactly at the click, not at the game's own early point.
    gate = {'L': None, 'R': None}
    _gate_down = {'L': False, 'R': False}

    def _apply_gate(self, report):
        r = None
        for side, idx, bit in (('L', 5, 0x04), ('R', 6, 0x08)):
            thr = self.gate.get(side)
            if thr is None:
                self._gate_down[side] = False
                continue
            raw = report[idx]
            down = self._gate_down[side]
            down = raw >= thr if not down else raw >= thr - 40      # hysteresis on release
            self._gate_down[side] = down
            if r is None:
                r = bytearray(report)
            r[idx] = 255 if down else 0
            r[9] = (r[9] | bit) if down else (r[9] & ~bit & 0xFF)
        return bytes(r) if r is not None else report

    def forward(self, report):
        # the pad reports ~1000x/s, mostly because gyro / timestamps move. Forward at once when sticks,
        # triggers or buttons change (bytes 1..10), otherwise at most every 4 ms (game sees 250 Hz motion)
        import time as _t
        now = _t.perf_counter(); key = bytes(report[1:11])
        if key == getattr(self, '_last_key', None) and now - getattr(self, '_last_fw', 0.0) < 0.004:
            return
        self._last_key = key; self._last_fw = now
        report = self._apply_gate(report)
        state = asb_input(report)
        try:
            with self.send_lock:
                self.sock.sendall(b'I' + struct.pack('<H', len(state)) + state)
        except Exception:
            return
        self.frames['in'] += 1

    def triggers(self):
        with self.lock:
            return self.left_effect, self.right_effect, self.trigger_seq

    def feedback(self):
        with self.lock:
            return self.rumble, self.compat_rumble, self.color

    def close(self):
        try:
            if self.sock is not None:
                self.sock.close()                  # the host removes the device and exits
        except Exception:
            pass
        self.sock = None
        if self.proc is not None:
            try:
                self.proc.wait(5)
            except Exception:
                self.proc.kill()
            self.proc = None
