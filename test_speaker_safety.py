import unittest
import numpy as np
from speaker_safety import SpeakerSafety
from haptic_dsp import RawSpeaker, HOP


def stream(x, block=240, headset=False):
    guard = SpeakerSafety(headset=headset)
    return np.concatenate([guard.process(x[i:i+block]) for i in range(0, len(x), block)])


class SpeakerTests(unittest.TestCase):
    def test_overload_impulse_and_corrupt_samples(self):
        rng = np.random.default_rng(83)
        x = np.concatenate((np.zeros(4800), rng.normal(0, 5, 24000),
                            [np.nan, np.inf, -np.inf], np.zeros(48000)))
        for headset, ceiling in ((False, .7), (True, .95)):
            y = stream(x, headset=headset)
            self.assertTrue(np.isfinite(y).all())
            self.assertLessEqual(np.max(np.abs(y)), ceiling + 1e-6)
            self.assertLess(np.max(np.abs(y[-2400:])), 1e-8)

    def test_block_boundaries_do_not_change_sound(self):
        x = np.random.default_rng(4).normal(0, 2, 30000)
        np.testing.assert_allclose(stream(x, 137), stream(x, 960), atol=1e-7)

    def test_headset_preserves_quiet_waveform_and_has_fixed_delay(self):
        x = np.random.default_rng(2).uniform(-.1, .1, 10000)
        y = stream(x, headset=True)
        np.testing.assert_allclose(y[240:], x[:-240], atol=1e-7)
        self.assertEqual(np.max(np.abs(y[:240])), 0)

    def test_speaker_rolloff_retains_midrange(self):
        def rms(hz):
            t = np.arange(24000) / 48000
            return np.sqrt(np.mean(stream(.1*np.sin(2*np.pi*hz*t))[-12000:]**2))
        mid = rms(1000)
        self.assertGreater(mid, .065)
        self.assertLess(rms(50), mid * .1)
        self.assertLess(rms(12000), mid * .25)

    def test_all_eqs_respect_zero_volume_and_remain_bounded(self):
        r = RawSpeaker()
        x = np.random.default_rng(8).normal(0, .8, HOP).astype(np.float32)
        r.analyse(x, x)
        for eq in r.PRESETS:
            self.assertEqual(np.max(np.abs(r.render(eq, 0))), 0.)
            y = stream(np.tile(r.render(eq, 1), 20))
            self.assertLessEqual(np.max(np.abs(y)), .700001)

    def test_output_channels_keep_grip_pcm(self):
        from ds_audio import HapticOut
        out = HapticOut.__new__(HapticOut)
        out.speaker_safety = SpeakerSafety()
        class Sink:
            def write(self, b, **kw): self.buf = np.frombuffer(b, np.float32).reshape(-1, 4)
        out.stream = Sink()
        l = np.linspace(-1, 1, 240, dtype=np.float32); r = -l
        out.feed(l, r, np.ones(240)*4)
        np.testing.assert_array_equal(out.stream.buf[:, 2], l)
        np.testing.assert_array_equal(out.stream.buf[:, 3], r)
        np.testing.assert_array_equal(out.stream.buf[:, 0], out.stream.buf[:, 1])


if __name__ == '__main__':
    unittest.main(verbosity=2)
