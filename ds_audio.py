"""DualSense voice-coil haptics over its USB audio endpoint + game-audio loopback capture.

DualSense (USB) exposes a 4-channel 48 kHz render endpoint: ch1/ch2 = speaker/headset,
ch3/ch4 = left/right grip voice-coil actuators. Writing waveforms to ch3/ch4 plays them
on the actuators (wired only).
"""
import threading
import time
import numpy as np
import pyaudiowpatch as pa
from speaker_safety import SpeakerSafety

RATE = 48000

# PortAudio's global init / terminate (and opening streams around them) is not thread-safe: the
# haptics engine and helper threads (default-device watcher, pad-speaker tap) share one process -> one lock for all.
PA_LOCK = threading.RLock()


# PortAudio scans the audio devices only when the FIRST instance initialises. While any instance is
# alive (any long-lived holder) new ones reuse that old list - a pad plugged in
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


def close_stream(stream):
    """An unplugged WASAPI stream may already refuse stop_stream()."""
    if stream is None:
        return
    try:
        stream.stop_stream()
    except Exception:
        pass
    try:
        stream.close()
    except Exception:
        pass


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
    """Push-model writer: feed(left, right) float32 arrays (-1..1) at 48 kHz.

    Feeds a lock-protected ring buffer drained by a WASAPI callback so scheduler jitter
    and 48 kHz clock drift never stall on the portaudio write boundary or clip without fades.
    """
    RING_CAPACITY = 9600
    PRIME_FRAMES = 1440
    TARGET_FRAMES = 1440
    MAX_FRAMES = 4800
    FADE_FRAMES = 144

    def __init__(self, p=None, name=None):
        # Own the PortAudio instance when none is passed, and always terminate it in close():
        # a leaked instance initialised on a finished thread breaks every later WASAPI open.
        self.own = p is None
        self.p = pa_new() if self.own else p
        try:
            self.dev = find_dualsense_output(self.p, name)
            if self.dev is None:
                raise LookupError('没有找到 DualSense 音频设备（需要 USB 线或 DS5Dongle 接收器，普通蓝牙不行）')
            self.lock = threading.Lock(); self.xruns = 0; self.overruns = 0; self.failure = None
            self.speaker_safety = SpeakerSafety()
            self._use_ring = True
            self._buf = np.zeros((self.RING_CAPACITY, 4), np.float32)
            self._rpos = 0
            self._wpos = 0
            self._count = 0
            self._primed = False
            self._last_spk = 0.0
            self._started_at = time.monotonic()
            self._last_cb = self._started_at
            self.stream = pa_open(self.p, format=pa.paFloat32, channels=4, rate=RATE, output=True,
                                  output_device_index=self.dev['index'], frames_per_buffer=480,
                                  stream_callback=self._callback)
        except Exception:
            if self.own:
                pa_end(self.p)
            raise

    def buffered_frames(self):
        if not getattr(self, '_use_ring', False):
            return 0
        with self.lock:
            return self._count

    def buffered_s(self):
        return self.buffered_frames() / RATE

    def _pull_frames(self, frame_count):
        out = np.zeros((frame_count, 4), np.float32)
        now = time.monotonic()
        with self.lock:
            self._last_cb = now
            fade_in = False
            if not self._primed:
                if self._count < max(frame_count, self.PRIME_FRAMES):
                    return out
                self._primed = True
                fade_in = True
            if self._count >= frame_count:
                first = min(frame_count, self.RING_CAPACITY - self._rpos)
                out[:first] = self._buf[self._rpos:self._rpos + first]
                if first < frame_count:
                    rem = frame_count - first
                    out[first:] = self._buf[:rem]
                    self._rpos = rem
                else:
                    self._rpos = (self._rpos + first) % self.RING_CAPACITY
                self._count -= frame_count
                if fade_in:
                    k = min(self.FADE_FRAMES, frame_count)
                    ramp = np.linspace(0.0, 1.0, k, dtype=np.float32)[:, None]
                    out[:k, :2] *= ramp
                self._last_spk = float(out[-1, 0])
                return out
            # True underrun: drain remaining samples with a smooth fade-out and require re-priming.
            self.xruns += 1
            self._primed = False
            avail = self._count
            if avail > 0:
                first = min(avail, self.RING_CAPACITY - self._rpos)
                out[:first] = self._buf[self._rpos:self._rpos + first]
                if first < avail:
                    out[first:avail] = self._buf[:avail - first]
                self._rpos = (self._rpos + avail) % self.RING_CAPACITY
                self._count = 0
                k = min(avail, self.FADE_FRAMES)
                ramp = np.linspace(1.0, 0.0, k, dtype=np.float32)[:, None]
                out[avail - k:avail, :2] *= ramp
            elif abs(self._last_spk) > 1e-5:
                k = min(64, frame_count)
                ramp = np.linspace(self._last_spk, 0.0, k, dtype=np.float32)
                out[:k, 0] = ramp
                out[:k, 1] = ramp
            self._last_spk = 0.0
            return out

    def _callback(self, in_data, frame_count, time_info, status):
        try:
            out = self._pull_frames(frame_count)
            return (out.tobytes(), pa.paContinue)
        except Exception as exc:
            self.failure = exc
            return (np.zeros((frame_count, 4), np.float32).tobytes(), pa.paContinue)

    def _push_frames(self, buf):
        n = len(buf)
        if not n:
            return
        with self.lock:
            if self._count + n > self.MAX_FRAMES:
                self.overruns += 1
                keep = max(0, 2400 - n)
                drop = max(0, self._count - keep)
                if drop:
                    self._rpos = (self._rpos + drop) % self.RING_CAPACITY
                    self._count -= drop
                    if self._count > 0:
                        k = min(self.FADE_FRAMES, self._count)
                        ramp = np.linspace(0.0, 1.0, k, dtype=np.float32)
                        idx = (self._rpos + np.arange(k)) % self.RING_CAPACITY
                        self._buf[idx, 0] *= ramp
                        self._buf[idx, 1] *= ramp
            if n >= self.MAX_FRAMES:
                buf = buf[-2400:]
                n = len(buf)
            first = min(n, self.RING_CAPACITY - self._wpos)
            self._buf[self._wpos:self._wpos + first] = buf[:first]
            if first < n:
                rem = n - first
                self._buf[:rem] = buf[first:]
                self._wpos = rem
            else:
                self._wpos = (self._wpos + first) % self.RING_CAPACITY
            self._count += n

    def drain(self, timeout=1.0):
        if not getattr(self, '_use_ring', False):
            return
        deadline = time.monotonic() + timeout
        while self.buffered_frames() > 0 and time.monotonic() < deadline and self.failure is None:
            time.sleep(0.005)

    def feed(self, left, right, speaker=None, headset=False, block=False):
        n = len(left)
        buf = np.zeros((n, 4), np.float32)
        if headset != self.speaker_safety.headset:
            self.speaker_safety = SpeakerSafety(headset=headset)
        # Feed zeros too, so filter/limiter tails drain when an event ends.
        clean = self.speaker_safety.process(np.zeros(n) if speaker is None else speaker)
        buf[:, 0] = clean; buf[:, 1] = clean
        buf[:, 2] = np.clip(left, -1, 1)
        buf[:, 3] = np.clip(right, -1, 1)
        if not getattr(self, '_use_ring', False):
            try:
                self.stream.write(buf.tobytes(), exception_on_underflow=True)
            except OSError as exc:
                if exc.errno == pa.paOutputUnderflowed:
                    self.xruns += 1
                else:
                    self.failure = exc
            return
        if self.stream is not None and self.failure is None:
            if time.monotonic() - self._last_cb > 1.0:
                try:
                    if not self.stream.is_active():
                        self.failure = OSError('DualSense audio stream stopped')
                except OSError as exc:
                    self.failure = exc
        if block:
            deadline = time.monotonic() + 1.0
            while self.buffered_frames() > self.TARGET_FRAMES and time.monotonic() < deadline and self.failure is None:
                time.sleep(0.005)
        self._push_frames(buf)

    def close(self):
        if self.stream is not None:
            try:
                if self.failure is None:
                    self.feed(np.zeros(1440, np.float32), np.zeros(1440, np.float32))
                    if getattr(self, '_use_ring', False):
                        self.drain(0.06)
            except Exception:
                pass
            close_stream(self.stream)
            self.stream = None
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
            out.feed(l[i:i + 2400], r[i:i + 2400], block=True)
    out.drain(1.5)
    out.close()
    return out.dev['name']


if __name__ == '__main__':
    print('played on', test_burst())

