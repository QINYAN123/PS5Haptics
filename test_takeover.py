import time, numpy as np
from takeover import ds4_report, RumbleCoil, fuse
from haptic_dsp import SpeakerShaper, HOP, RATE, HapticEngine

def ds_input(lx=10, ly=20, rx=30, ry=40, l2=50, r2=60, b8=0x28, b9=0x21, b10=0x03, gyro=(100, -200, 300), acc=(1, 2, 8192)):
    r = bytearray(64); r[0] = 1
    r[1:7] = bytes([lx, ly, rx, ry, l2, r2]); r[8], r[9], r[10] = b8, b9, b10
    for i, v in enumerate(gyro + acc):
        r[16 + 2 * i:18 + 2 * i] = int(v).to_bytes(2, 'little', signed=True)
    r[28:32] = (48000).to_bytes(4, 'little')
    # touch point 0: touching id 5 at x=1000 y=500 ; point 1: not touching
    x, y = 1000, 500
    r[33:37] = bytes([5, x & 0xFF, (x >> 8) | ((y & 0xF) << 4), y >> 4]); r[37] = 0x80
    return bytes(r)

def test_mapping():
    b = bytes(ds4_report(ds_input(), 7).buf)
    assert b[0:4] == bytes([10, 20, 30, 40])
    assert b[4] == 0x28 and b[5] == 0x21          # hat=E(2)+cross, L1+options
    assert b[6] & 3 == 3 and b[6] >> 2 == 7        # PS + touch click, counter
    assert (b[7], b[8]) == (50, 60)
    g = [int.from_bytes(b[12 + 2 * i:14 + 2 * i], 'little', signed=True) for i in range(6)]
    assert g == [100, -200, 300, 1, 2, 8192]
    assert int.from_bytes(b[9:11], 'little') == 3000
    assert b[34] == 5 and (b[35] | ((b[36] & 0xF) << 8)) == 1000 and ((b[36] >> 4) | (b[37] << 4)) == 500 and b[38] == 0x80

def test_rumble():
    rc = RumbleCoil(); rc.step(1, 0); wl, wr = rc.step(1, 0)
    assert np.max(np.abs(wl)) > 0.8 and np.max(np.abs(wr)) < 0.45     # large motor -> left grip mostly
    rc = RumbleCoil(); rc.step(0, 0); wl, wr = rc.step(0, 0)
    assert np.max(np.abs(wl)) == 0
    a = np.full(HOP, .5, np.float32)
    l, _ = fuse(a, a, np.zeros(HOP, np.float32), np.zeros(HOP, np.float32), 1.0, '融合')
    assert abs(l[0] - np.tanh(.25)) < 1e-5                            # audio ducked 50% while rumble plays
    l, _ = fuse(a, a, a * 0, a * 0, 0.0, '仅游戏震动'); assert np.all(l == 0)

def test_speaker():
    sp = SpeakerShaper('冲击声', 1.0)
    noise = np.random.default_rng(1).standard_normal(HOP).astype(np.float32) * .3
    assert np.max(np.abs(sp.process(noise, noise, 0.0))) < 1e-6        # no impact -> silent
    for _ in range(3): y = sp.process(noise, noise, 0.6)
    assert np.max(np.abs(y)) > 0.1
    assert SpeakerShaper('关闭').process(noise, noise, 1) is None
    sp = SpeakerShaper('全部游戏声音', .5); dc = np.ones(HOP * 20, np.float32)
    assert abs(sp.process(dc, dc, 0)[-1]) < 0.05                      # high-pass removes DC/bass
    t0 = time.perf_counter()
    for _ in range(200): sp.process(noise, noise, .5)
    assert (time.perf_counter() - t0) / 200 < 0.0005

if __name__ == '__main__':
    for f in (test_mapping, test_rumble, test_speaker): f(); print('ok', f.__name__)
