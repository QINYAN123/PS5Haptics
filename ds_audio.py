"""DualSense voice-coil haptics over its USB audio endpoint + game-audio loopback capture.

DualSense (USB) exposes a 4-channel 48 kHz render endpoint: ch1/ch2 = speaker/headset,
ch3/ch4 = left/right grip voice-coil actuators. Writing waveforms to ch3/ch4 plays them
on the actuators (wired only).
"""
import threading
import numpy as np
import pyaudiowpatch as pa

RATE = 48000

# PortAudio's global init / terminate (and opening streams around them) is not thread-safe: the
# haptics engine and the audio mirror run in different threads of one process -> one lock for all.
PA_LOCK = threading.RLock()


# PortAudio scans the audio devices only when the FIRST instance initialises. While any instance is
# alive (e.g. the audio mirror, open all the time) new ones reuse that old list - a pad plugged in
# later is never found. rescan() asks long-lived holders to let go (they watch GEN) so the next
# instance re-enumerates.
_STATE = {'live': 0, 'gen': 0}


def pa_new():
    with PA_LOCK:
        p = pa.PyAudio(); _STATE['live'] += 1
        return p


def pa_end(p):
    with PA_LOCK:
        p.terminate(); _STATE['live'] = max(0, _STATE['live'] - 1)


def pa_gen():
    return _STATE['gen']


def rescan(timeout=2.0):
    """Make the next pa_new() see the current devices (waits until every instance is released)."""
    import time
    _STATE['gen'] += 1
    t = time.monotonic() + timeout
    while _STATE['live'] and time.monotonic() < t:
        time.sleep(0.02)
    return _STATE['live'] == 0


def pa_open(p, **kw):
    with PA_LOCK:
        return p.open(**kw)


def _wasapi(p):
    return p.get_host_api_info_by_type(pa.paWASAPI)


def find_dualsense_output(p, name=None):
    """name: exact device name to pick. In 原生透传 the virtual pad adds a second 'DualSense' audio
    device; the real one must be remembered by name when the audio is reopened."""
    w = _wasapi(p)['index']; first = None
    for i in range(p.get_device_count()):
        d = p.get_device_info_by_index(i)
        if d['hostApi'] == w and not d.get('isLoopbackDevice') and d['maxOutputChannels'] >= 4 and 'DualSense' in d['name']:
            if name is None or d['name'] == name:
                return d
            first = first or d
    return None if name else first


def find_loopback(p, name_hint=None):
    """Loopback of the default output (or of a device whose name contains name_hint)."""
    w = _wasapi(p)
    target = p.get_device_info_by_index(w['defaultOutputDevice'])['name'] if not name_hint else name_hint
    for d in p.get_loopback_device_info_generator():
        if target in d['name']:
            return d
    return None


class HapticOut:
    """Push-model writer: feed(left, right) float32 arrays (-1..1) at 48 kHz."""

    def __init__(self, p=None, name=None):
        # Own the PortAudio instance when none is passed, and always terminate it in close():
        # a leaked instance initialised on a finished thread breaks every later WASAPI open.
        self.own = p is None
        self.p = p or pa_new()
        self.dev = find_dualsense_output(self.p, name)
        if self.dev is None:
            if self.own: pa_end(self.p)
            raise RuntimeError('没有找到有线连接的 DualSense 音频设备（需要 USB 连线）')
        self.stream = pa_open(self.p, format=pa.paFloat32, channels=4, rate=RATE, output=True,
                                  output_device_index=self.dev['index'], frames_per_buffer=960)   # 20 ms periods: rides out short stalls of the main loop (10-20 ms gaps = crackle)
        self.lock = threading.Lock(); self.xruns = 0

    def feed(self, left, right, speaker=None):
        n = len(left)
        buf = np.zeros((n, 4), np.float32)
        if speaker is not None:             # ch1/ch2 = controller speaker (or its headphone jack)
            buf[:, 0] = speaker; buf[:, 1] = speaker
        buf[:, 2] = np.clip(left, -1, 1)
        buf[:, 3] = np.clip(right, -1, 1)
        try:
            self.stream.write(buf.tobytes(), exception_on_underflow=True)
        except IOError:
            self.xruns += 1                     # output ran dry before this block: an audible gap

    def close(self):
        try:
            self.feed(np.zeros(2400, np.float32), np.zeros(2400, np.float32))
            self.stream.stop_stream(); self.stream.close()
        except Exception:
            pass
        if self.own:
            try: pa_end(self.p)
            except Exception: pass
            self.own = False


def test_burst():
    """Left low thump, right high tick, then a 'perfect parry' double click on both."""
    out = HapticOut()
    t = np.arange(RATE) / RATE
    def tone(hz, dur, decay):
        n = int(dur * RATE); x = np.sin(2 * np.pi * hz * t[:n]) * np.exp(-t[:n] / decay)
        return x.astype(np.float32)
    z = lambda s: np.zeros(int(s * RATE), np.float32)
    seq = [
        (tone(60, .35, .12) * .9, z(.35)),
        (z(.4), z(.4)),
        (z(.35), tone(170, .35, .06) * .9),
        (z(.4), z(.4)),
    ]
    click = np.concatenate([tone(150, .03, .008), z(.06), tone(150, .03, .008)]) * 1.0
    seq.append((click, click))
    seq.append((z(.3), z(.3)))
    for l, r in seq:
        for i in range(0, len(l), 2400):
            out.feed(l[i:i + 2400], r[i:i + 2400])
    out.close()
    return out.dev['name']


if __name__ == '__main__':
    print('played on', test_burst())
