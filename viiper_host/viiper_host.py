"""VIIPERHost - a tiny stand-alone process that owns libVIIPER (the virtual DualSense).

License: GPL-3.0 (this program loads libVIIPER, which is GPL-3.0). It is a separate program from
PS5声音触感: the two only exchange messages over a local TCP socket (127.0.0.1), so PS5声音触感
itself is not a derivative work of VIIPER and keeps its own license.

Usage:  VIIPERHost.exe <parent_pid>
  prints "PORT <n>" on stdout, accepts ONE connection on 127.0.0.1:<n>, exits when the connection
  closes or the parent process exits.

Protocol (both directions): [kind u8][len u16 LE][payload]
  client -> host   b'I'  33-byte ApexSenseBridge input state  (SetDualSenseASBInputState)
  host -> client   b'O'  ready (payload: patch version u32)
                   b'E'  error text (utf-8), then the host exits
                   b'R'  raw output frame exactly as libVIIPER delivers it
                   b'L'  log line (level u8 + utf-8 text)
"""
import ctypes as C
import pathlib
import socket
import struct
import sys
import threading

HERE = pathlib.Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else pathlib.Path(__file__).resolve().parent


class USBServerConfig(C.Structure):
    _fields_ = [('addr', C.c_char_p), ('connection_timeout_ms', C.c_uint64),
                ('device_handler_connect_timeout_ms', C.c_uint64), ('write_batch_flush_interval_ms', C.c_uint32)]


LOG_CB = C.CFUNCTYPE(None, C.c_int, C.c_char_p)
RAW_CB = C.CFUNCTYPE(None, C.c_size_t, C.POINTER(C.c_uint8), C.c_uint32)


def find_dll():
    for d in (HERE, HERE / 'bin', HERE.parent / 'bin', HERE.parent):
        if (d / 'libVIIPER.dll').exists():
            return d / 'libVIIPER.dll'
    raise FileNotFoundError('libVIIPER.dll not found next to VIIPERHost')


class Host:
    def __init__(self, conn):
        self.conn = conn; self.send_lock = threading.Lock(); self.logs = []
        self.dll = C.CDLL(str(find_dll()))
        B = C.c_bool
        specs = [('NewUSBServerASBLoopback', [C.POINTER(USBServerConfig), C.POINTER(C.c_size_t), LOG_CB], B),
                 ('CloseUSBServer', [C.c_size_t], B),
                 ('CreateUSBBus', [C.c_size_t, C.POINTER(C.c_uint32)], B),
                 ('RemoveUSBBus', [C.c_size_t, C.c_uint32], B),
                 ('CreateDualSenseDevice', [C.c_size_t, C.POINTER(C.c_size_t), C.c_uint32, B, C.c_uint16, C.c_uint16, C.c_void_p], B),
                 ('RemoveDualSenseDevice', [C.c_size_t], B),
                 ('SetDualSenseASBInputState', [C.c_size_t, C.POINTER(C.c_uint8), C.c_uint32], B),
                 ('SetDualSenseASBRawOutputCallback', [C.c_size_t, C.c_void_p, C.c_size_t], B),
                 ('GetVIIPERASBPatchVersion', [], C.c_uint32)]
        for name, args, ret in specs:
            fn = getattr(self.dll, name); fn.argtypes = args; fn.restype = ret
        self._log_cb = LOG_CB(self._on_log); self._raw_cb = RAW_CB(self._on_raw)
        self.server = C.c_size_t(0); self.bus = C.c_uint32(0); self.dev = C.c_size_t(0)

    def send(self, kind, payload):
        with self.send_lock:
            self.conn.sendall(kind + struct.pack('<H', len(payload)) + payload)

    def _on_log(self, level, msg):
        try:
            text = msg.decode('utf8', 'replace'); self.logs = (self.logs + [text])[-5:]
            self.send(b'L', bytes([level & 0xFF]) + text.encode('utf8')[:1000])
        except Exception:
            pass

    def _on_raw(self, ctx, frame, length):
        try:
            self.send(b'R', C.string_at(frame, length))
        except Exception:
            pass

    def open(self):
        cfg = USBServerConfig(b'', 30000, 5000, 1)
        if not self.dll.NewUSBServerASBLoopback(C.byref(cfg), C.byref(self.server), self._log_cb):
            raise RuntimeError('虚拟 PS5 手柄服务启动失败（端口 3241 被占用？请先退出飞智 XHaptics 等同类软件）')
        if not self.dll.CreateUSBBus(self.server, C.byref(self.bus)):
            raise RuntimeError('创建虚拟 USB 总线失败')
        if not self.dll.CreateDualSenseDevice(self.server, C.byref(self.dev), self.bus.value, True, 0, 0, None):
            raise RuntimeError('创建虚拟 PS5 手柄失败（usbip-win2 驱动未就绪？）' + ('：' + '; '.join(self.logs[-3:]) if self.logs else ''))
        if not self.dll.SetDualSenseASBRawOutputCallback(self.dev, C.cast(self._raw_cb, C.c_void_p), 0):
            raise RuntimeError('注册虚拟手柄反馈回调失败')

    def set_input(self, state):
        buf = (C.c_uint8 * 33).from_buffer_copy(state[:33].ljust(33, b'\0'))
        self.dll.SetDualSenseASBInputState(self.dev, buf, 33)

    def close(self):
        try:
            if self.dev.value:
                self.dll.SetDualSenseASBRawOutputCallback(self.dev, None, 0)
                self.dll.RemoveDualSenseDevice(self.dev); self.dev = C.c_size_t(0)
            if self.bus.value:
                self.dll.RemoveUSBBus(self.server, self.bus.value); self.bus = C.c_uint32(0)
        finally:
            if self.server.value:
                self.dll.CloseUSBServer(self.server); self.server = C.c_size_t(0)


def recv_exact(conn, n):
    b = b''
    while len(b) < n:
        chunk = conn.recv(n - len(b))
        if not chunk:
            raise ConnectionError
        b += chunk
    return b


def watch_parent(pid, conn):
    k = C.windll.kernel32
    h = k.OpenProcess(0x00100000, False, pid)              # SYNCHRONIZE
    if h:
        k.WaitForSingleObject(h, 0xFFFFFFFF)
    try: conn.shutdown(socket.SHUT_RDWR)
    except Exception: pass


def main():
    parent = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    srv = socket.socket(); srv.bind(('127.0.0.1', 0)); srv.listen(1)
    print('PORT', srv.getsockname()[1], flush=True)
    srv.settimeout(20)
    conn, _ = srv.accept(); srv.close()
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    if parent:
        threading.Thread(target=watch_parent, args=(parent, conn), daemon=True).start()
    host = None
    try:
        host = Host(conn); host.open()
        host.send(b'O', struct.pack('<I', host.dll.GetVIIPERASBPatchVersion()))
        while True:
            kind = recv_exact(conn, 1); n = struct.unpack('<H', recv_exact(conn, 2))[0]
            payload = recv_exact(conn, n) if n else b''
            if kind == b'I':
                host.set_input(payload)
    except ConnectionError:
        pass
    except Exception as exc:
        try:
            msg = str(exc).encode('utf8')[:1000]; conn.sendall(b'E' + struct.pack('<H', len(msg)) + msg)
        except Exception:
            pass
    finally:
        if host is not None:
            host.close()
        try: conn.close()
        except Exception: pass


if __name__ == '__main__':
    main()
