import time, unittest
import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi
from haptic_dsp import FastSOS, HapticEngine, RawSpeaker, HOP, RATE

rng = np.random.default_rng(1)


def run(engine, sig_l, sig_r=None, press=(0, 0)):
    sig_r = sig_l if sig_r is None else sig_r
    outs = []; trig = []; worst = 0
    for i in range(0, len(sig_l) - HOP, HOP):
        t0 = time.perf_counter()
        wl, wr, tg = engine.process(sig_l[i:i + HOP], sig_r[i:i + HOP], press)
        worst = max(worst, time.perf_counter() - t0)
        outs.append((float(np.sqrt(np.mean(wl ** 2))), float(np.sqrt(np.mean(wr ** 2))))); trig.append(tg)
    return np.array(outs), np.array(trig), worst


def music(sec):
    t = np.arange(int(sec * RATE)) / RATE
    x = 0.2 * (np.sin(2 * np.pi * 440 * t) + 0.5 * np.sin(2 * np.pi * 660 * t) + 0.3 * np.sin(2 * np.pi * 1320 * t))
    return (x + 0.01 * rng.standard_normal(len(t))).astype(np.float32)


def impacts(sec, every=0.5, amp=0.8):
    x = music(sec) * 0.5
    for k in range(int(sec / every)):
        s = int((0.2 + k * every) * RATE); n = int(0.03 * RATE)
        x[s:s + n] += (amp * rng.standard_normal(n) * np.exp(-np.arange(n) / (0.006 * RATE))).astype(np.float32)
    return x


class DSPTests(unittest.TestCase):
    def test_sustained_music_stays_quiet(self):
        o, t, _ = run(HapticEngine(), music(12))
        self.assertLess(np.percentile(o[-800:, 0], 90), 0.05)

    def test_impacts_drive_grips_and_triggers(self):
        o, t, _ = run(HapticEngine(), impacts(12))
        late = o[-1200:, 0]
        self.assertGreater(late.max(), 0.15)
        self.assertGreater(np.percentile(late, 99) / (np.median(late) + 1e-3), 5)  # spiky, not a constant buzz
        self.assertGreater(t[-1200:, 1].max(), 0.2)

    def test_right_follows_right_channel(self):
        o, _, _ = run(HapticEngine(), music(10) * 0.5, impacts(10))
        self.assertGreater(o[-1000:, 1].max(), 3 * o[-1000:, 0].max() + 0.02)

    def test_trigger_press_ducks_grip(self):
        a, _, _ = run(HapticEngine(), impacts(8))
        b, _, _ = run(HapticEngine(), impacts(8), press=(0, 1.0))
        self.assertLess(b[-800:, 0].max(), 0.5 * a[-800:, 0].max())

    def test_realtime_budget(self):
        _, _, worst = run(HapticEngine(), impacts(6))
        self.assertLess(worst, 0.004)

    def test_silence_is_silent(self):
        o, t, _ = run(HapticEngine(), np.zeros(5 * RATE, np.float32))
        self.assertEqual(float(o.max()), 0.0); self.assertEqual(float(t.max()), 0.0)

    def test_fast_sos_matches_scipy_and_settles(self):
        sos = butter(2, (70, 320), 'bandpass', fs=RATE, output='sos')
        flt = FastSOS(sos, block_len=HOP)
        zi = sosfilt_zi(sos) * 0.0
        sig = np.concatenate([
            impacts(0.5),
            np.zeros(4 * RATE, np.float32),
            impacts(0.2)[:137],
        ])
        pos = 0
        sizes = [HOP] * ((len(sig) - 137) // HOP) + [137]
        for sz in sizes:
            blk = sig[pos:pos + sz]; pos += sz
            ref, zi = sosfilt(sos, blk.astype(np.float64), zi=zi)
            got = flt.step(blk)
            np.testing.assert_allclose(got, ref, atol=1e-11, rtol=1e-11)
        self.assertTrue(flt.active)

    def test_fine_engine_idle_and_wakeup(self):
        eng = HapticEngine('细腻', '均衡冲击', style='标准')
        spk = RawSpeaker()
        z = np.zeros(HOP, np.float32)
        for _ in range(30):
            wl, wr, tg = eng.process(z, z)
            spk.analyse(z, z)
        self.assertTrue(eng._idle)
        self.assertEqual(float(np.abs(wl).max()), 0.0)
        self.assertEqual(float(np.abs(spk.render('原味', 0.8)).max()), 0.0)
        hit = (0.8 * rng.standard_normal(HOP)).astype(np.float32)
        wl, wr, tg = eng.process(hit, hit)
        self.assertFalse(eng._idle)
        self.assertGreater(float(np.abs(wl).max()), 0.05)


if __name__ == '__main__':
    unittest.main(verbosity=2)
