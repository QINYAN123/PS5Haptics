"""DualSense (USB) HID: read L2/R2, write adaptive-trigger effects only.

Output report 0x02 (48 bytes): byte1 valid_flag0 -> bit2 right trigger, bit3 left trigger.
Right trigger effect at bytes 11..21, left at 22..32 (11-byte effect blocks).
Only trigger flags are set, so game/Steam rumble and our audio haptics are untouched.
Effect 0x26 = vibration: zone mask, 3-bit amplitude per zone, frequency byte 9.
Effect 0x21 = feedback (resistance): zone mask, 3-bit strength per zone.
Effect 0x05 = off.
"""
import threading
import time
import hid

VID = 0x054C
PIDS = (0x0CE6, 0x0DF2)   # DualSense, DualSense Edge


def find_path():
    for d in hid.enumerate(VID, 0):
        if d['product_id'] in PIDS and d.get('interface_number', 3) in (3, -1):
            return d['path'], d['product_id']
    return None, None


def effect_off():
    return bytes([0x05] + [0] * 10)


def effect_vibration(amplitude, frequency, start_zone=0):
    """amplitude 0..8 (0 = off), frequency 1..255, zones start_zone..9 vibrate."""
    if amplitude <= 0:
        return effect_off()
    amplitude = max(1, min(8, int(amplitude)))
    zones = list(range(max(0, min(9, start_zone)), 10))
    mask = 0; packed = 0
    for z in zones:
        mask |= 1 << z
        packed |= (amplitude - 1) << (3 * z)
    e = bytearray(11)
    e[0] = 0x26
    e[1:3] = mask.to_bytes(2, 'little')
    e[3:7] = (packed & 0xFFFFFFFF).to_bytes(4, 'little')
    e[9] = max(1, min(255, int(frequency)))
    return bytes(e)


def effect_feedback(strength, start_zone=0):
    """Continuous resistance from start_zone to the end; strength 0..8."""
    if strength <= 0:
        return effect_off()
    strength = max(1, min(8, int(strength)))
    mask = 0; packed = 0
    for z in range(max(0, min(9, start_zone)), 10):
        mask |= 1 << z; packed |= (strength - 1) << (3 * z)
    e = bytearray(11)
    e[0] = 0x21
    e[1:3] = mask.to_bytes(2, 'little')
    e[3:7] = (packed & 0xFFFFFFFF).to_bytes(4, 'little')
    return bytes(e)


def effect_weapon(start_zone=3, end_zone=6, strength=6):
    """0x25 'weapon': resistance from start_zone that snaps (gives way) at end_zone - a gun trigger
    feel. Zones 2..8 apart; strength 1..8."""
    start = max(2, min(7, int(start_zone))); end = max(start + 1, min(8, int(end_zone)))
    mask = (1 << start) | (1 << end)
    e = bytearray(11)
    e[0] = 0x25
    e[1:3] = mask.to_bytes(2, 'little')
    e[3] = max(0, min(7, int(strength) - 1))
    return bytes(e)


def output_report(right=None, left=None):
    b = bytearray(48)
    b[0] = 0x02
    flag = 0
    if right is not None:
        flag |= 0x04; b[11:22] = right
    if left is not None:
        flag |= 0x08; b[22:33] = left
    b[1] = flag
    return bytes(b)


class DualSenseHID:
    def __init__(self):
        path, pid = find_path()
        if path is None:
            raise RuntimeError('没有找到 USB 连接的 DualSense')
        self.dev = hid.device()
        self.dev.open_path(path)
        self.dev.set_nonblocking(True)
        self.pid = pid
        self.l2 = 0; self.r2 = 0
        self.last = (None, None); self.last_write = 0.0
        self.lock = threading.Lock()
        self.reader = None; self.reader_stop = threading.Event(); self.reports = 0; self.read_error = None
        self.light = None

    def set_lightbar(self, r=0, g=0, b=255, player=0x04):
        """Light bar + player LED (they stay dark on PC until a program sets them)."""
        rep = bytearray(48); rep[0] = 0x02
        rep[2] = 0x04 | 0x10; rep[39] = 0x02; rep[42] = 0x02; rep[44] = player
        rep[45], rep[46], rep[47] = r, g, b
        with self.lock:
            self.dev.write(bytes(rep))
        self.light = (r, g, b)

    def enable_speaker(self, volume=0x64, preamp=4):
        """Route ch1/ch2 to the built-in speaker at a set volume. A game that owned the pad before
        (e.g. via libScePad) may have left the speaker muted or routed to the headset.
        preamp: speaker pre-amplifier gain 0..7 (byte 38, valid flag1 bit 7 = audio control 2);
        the firmware default is the lowest step, which is easily drowned out by the TV."""
        rep = bytearray(48); rep[0] = 0x02
        rep[1] = 0x20 | 0x80                  # valid: speaker volume + audio control
        rep[2] = 0x80                         # valid: audio control 2 (preamp gain)
        rep[6] = max(0, min(0x7F, int(volume)))
        rep[8] = 3 << 4                       # output path: speaker
        rep[38] = max(0, min(7, int(preamp)))
        with self.lock:
            self.dev.write(bytes(rep))

    def start_reader(self, on_report):
        """Own all input reads in a thread; on_report(bytes) gets every 0x01 input report.
        poll_input() then just returns the cached trigger values."""
        self.dev.set_nonblocking(False)

        def loop():
            while not self.reader_stop.is_set():
                try:
                    r = self.dev.read(64, 20)
                except Exception as exc:          # unplugged
                    self.read_error = exc; break
                if r and r[0] == 0x01 and len(r) >= 41:
                    self.l2, self.r2 = r[5], r[6]; self.reports += 1
                    try:
                        on_report(bytes(r))
                    except Exception as exc:
                        self.read_error = exc; break
        self.reader = threading.Thread(target=loop, daemon=True); self.reader.start()

    def poll_input(self):
        """Drain pending input reports; keep latest L2/R2 (0..255)."""
        if self.reader is not None:
            return self.l2 / 255, self.r2 / 255
        for _ in range(16):
            r = self.dev.read(64)
            if not r:
                break
            if r[0] == 0x01 and len(r) >= 7:
                self.l2, self.r2 = r[5], r[6]
        return self.l2 / 255, self.r2 / 255

    def set_triggers(self, left_effect, right_effect, min_interval=0.016, force=False):
        now = time.monotonic()
        if not force and (left_effect, right_effect) == self.last and now - self.last_write < 1.0:
            return False
        if not force and now - self.last_write < min_interval:
            return False
        with self.lock:
            self.dev.write(output_report(right=right_effect, left=left_effect))
        self.last = (left_effect, right_effect); self.last_write = now
        return True

    def release(self):
        try:
            self.set_triggers(effect_off(), effect_off(), force=True)
        except Exception:
            pass

    def close(self):
        self.reader_stop.set()
        if self.reader is not None:
            self.reader.join(0.5)
        self.release()
        try:
            self.dev.close()
        except Exception:
            pass
