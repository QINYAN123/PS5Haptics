import unittest
import numpy as np
from speaker_safety import SpeakerSafety
from haptic_dsp import RawSpeaker, HOP


def stream(x, block=240, headset=False):
    guard = SpeakerSafety(headset=headset)
    return np.concatenate([guard.process(x[i:i+block]) for i in range(0, len(x), block)])


class SpeakerTests(unittest.TestCase):
    def test_quiet_audio_keeps_nominal_makeup_gain(self):
        renderer = RawSpeaker()
        guard = SpeakerSafety()
        output = []
        for block in range(600):
            t = (np.arange(HOP) + block * HOP) / 48000
            tone = (.01 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
            renderer.analyse(tone, tone)
            output.extend(guard.process(renderer.render('原味', .7)))
        rms = np.sqrt(np.mean(np.asarray(output[-4800:]) ** 2))
        self.assertGreater(rms, .027)
        self.assertLess(rms, .031)
        self.assertEqual(guard.limited_samples, 0)

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

    def test_haptic_out_ring_buffer_prebuffer_wrap_underrun_and_overrun(self):
        import threading
        import time
        from ds_audio import HapticOut

        out = HapticOut.__new__(HapticOut)
        out.lock = threading.Lock()
        out.xruns = 0
        out.overruns = 0
        out.failure = None
        out.stream = None
        out.speaker_safety = SpeakerSafety(headset=True)
        out._use_ring = True
        out._buf = np.zeros((HapticOut.RING_CAPACITY, 4), np.float32)
        out._rpos = 0
        out._wpos = 0
        out._count = 0
        out._primed = False
        out._last_spk = 0.0
        out._started_at = out._last_cb = time.monotonic()

        tone = lambda start, n: (0.4 * np.sin(2 * np.pi * 1000 * (np.arange(n) + start) / 48000)).astype(np.float32)
        z480 = np.zeros(480, np.float32)

        # Below PRIME_FRAMES (1440): stays silent and unprimed without counting underruns.
        out.feed(z480, z480, tone(0, 480), headset=True)
        out.feed(z480, z480, tone(480, 480), headset=True)
        self.assertEqual(float(np.max(np.abs(out._pull_frames(480)[:, 0]))), 0.0)
        self.assertFalse(out._primed)
        self.assertEqual(out.xruns, 0)

        # Reaching PRIME_FRAMES primes playback with a 3 ms fade-in from 0.
        out.feed(z480, z480, tone(960, 480), headset=True)
        first = out._pull_frames(480)[:, 0]
        self.assertTrue(out._primed)
        self.assertEqual(float(first[0]), 0.0)
        self.assertGreater(float(np.max(np.abs(first))), 0.25)

        # Push and pull past RING_CAPACITY to verify wrap-around continuity.
        idx = 1440
        played = [first]
        for _ in range(25):
            out.feed(z480, z480, tone(idx, 480), headset=True)
            idx += 480
            played.append(out._pull_frames(480)[:, 0])
        tail = np.concatenate(played[-6:])
        expected_step = 0.4 * 2 * np.pi * 1000 / 48000
        self.assertLess(float(np.max(np.abs(np.diff(tail)))), expected_step * 1.15)
        self.assertEqual(out.xruns, 0)

        # Drain down to a partial block to verify underrun fade-out and re-prime gate.
        while out.buffered_frames() >= 480:
            out._pull_frames(480)
        out.feed(z480[:100], z480[:100], np.full(100, 0.3, np.float32), headset=True)
        self.assertGreater(out.buffered_frames(), 0)
        if out.buffered_frames() >= 480:
            out._pull_frames(480)
        if out.buffered_frames() == 0:
            out.feed(z480[:100], z480[:100], np.full(100, 0.3, np.float32), headset=True)
        rem = out.buffered_frames()
        self.assertGreater(rem, 0)
        self.assertLess(rem, 480)
        last_pull = out._pull_frames(480)[:, 0]
        self.assertEqual(out.xruns, 1)
        self.assertFalse(out._primed)
        self.assertLess(abs(float(last_pull[rem - 1])), 0.02)
        self.assertEqual(float(np.max(np.abs(last_pull[rem:]))), 0.0)

        # A single 480-frame chunk must wait until 1440 frames accumulate again.
        out.feed(z480, z480, tone(0, 480), headset=True)
        self.assertEqual(float(np.max(np.abs(out._pull_frames(480)[:, 0]))), 0.0)
        self.assertFalse(out._primed)

        # Overrun trims safely without exceeding MAX_FRAMES.
        for _ in range(15):
            out.feed(z480, z480, tone(0, 480), headset=True)
        self.assertGreaterEqual(out.overruns, 1)
        self.assertLessEqual(out.buffered_frames(), HapticOut.MAX_FRAMES)


if __name__ == '__main__':
    unittest.main(verbosity=2)
