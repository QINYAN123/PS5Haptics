"""Motorbike report, HUD expiry and boost audio regression tests."""
import unittest
from unittest.mock import patch

import numpy as np

from ds_hid import effect_off
from ww_bike import BikeHud, BikeInput, BoostResistance, R1_BIT, R2_BIT, RATE
from ww_vision import WWVision
from xds import VirtualDualSense


def report(bits=0, r2=0):
    frame = bytearray(64)
    frame[0] = 1
    frame[5:7] = bytes([131, r2])
    frame[9] = 0x04 | bits
    return bytes(frame)


class MotorbikeTests(unittest.TestCase):
    def test_input_bits_and_analog_value_swap(self):
        remap = BikeInput()
        remap.apply(report(), True)
        shoulder = remap.apply(report(R1_BIT), True)
        self.assertEqual((shoulder[5], shoulder[6], shoulder[9]), (131, 255, 0x04 | R2_BIT))
        trigger = remap.apply(report(R2_BIT, 100), True)
        self.assertEqual((trigger[5], trigger[6], trigger[9]), (131, 0, 0x04 | R1_BIT))
        both = remap.apply(report(R1_BIT | R2_BIT, 100), True)
        self.assertEqual((both[6], both[9]), (255, 0x04 | R1_BIT | R2_BIT))

    def test_swap_transitions_require_neutral(self):
        remap = BikeInput()
        held = report(R1_BIT | R2_BIT, 255)
        self.assertEqual(remap.apply(held, True), report())
        remap.apply(report(), True)
        self.assertNotEqual(remap.apply(report(R2_BIT, 255), True), report())
        self.assertEqual(remap.apply(held, False), report())
        remap.apply(report(), False)
        self.assertEqual(remap.apply(held, False), held)

    def test_native_host_packets_follow_context_and_bypass_combat_gate(self):
        vpad = VirtualDualSense()
        class Wire:
            def sendall(self, packet):
                self.packet = packet
        vpad.sock = wire = Wire()
        mode = 'air'
        vpad.bike_context = lambda: mode
        vpad.gate['R'] = 170
        vpad.forward(report())
        vpad.forward(report(0, 44))
        self.assertEqual(wire.packet[3 + 5], 0)
        self.assertTrue(int.from_bytes(wire.packet[3 + 7:3 + 9], 'little') & (R1_BIT << 8))
        mode = 'unknown'
        vpad.forward(report(0, 64))
        self.assertEqual(wire.packet[3 + 5], 0)
        self.assertFalse(int.from_bytes(wire.packet[3 + 7:3 + 9], 'little') & (R1_BIT << 8))
        vpad.forward(report())
        mode = 'other'
        vpad.forward(report(R2_BIT, 220))
        self.assertEqual(wire.packet[3 + 5], 255)
        self.assertTrue(int.from_bytes(wire.packet[3 + 7:3 + 9], 'little') & (R2_BIT << 8))

    def test_hud_expiry_and_combat_state_clearing(self):
        hud = BikeHud()
        hud.state = ('boost', 10.3)
        self.assertEqual(hud.current(10.2)[0], 'boost')
        self.assertEqual(hud.current(10.3)[0], 'unknown')
        class Camera:
            width, height = 3840, 2160
        vision = WWVision(lambda: (0.0, 1.0), camera=Camera(), ocr=False, bike=True)
        vision._setup()
        vision.final['echo'].state = True
        vision.events.append(('ww', 'echo', 'ready'))

        def update(_frame, _pixel, now):
            vision.bike.state = ('boost', now + 0.3)
        with patch.object(vision.bike, 'update', side_effect=update):
            vision.step(None, 10.0)
        self.assertIsNone(vision.final['echo'].state)
        self.assertFalse(vision.poll())
        self.assertFalse(vision.pre_press)

    def test_low_band_changes_resistance_even_with_opposite_stereo_phase(self):
        t = np.arange(RATE // 2) / RATE
        base = BoostResistance().effect(True, 1.0, 1.0)
        for frequency, changes_effect in ((80, True), (1500, False)):
            boost = BoostResistance()
            signal = 0.16 * np.sin(t * 2 * np.pi * frequency)
            boost.feed(signal, -signal, 1.0)
            self.assertEqual(boost.effect(True, 1.05, 1.0) != base, changes_effect)
            self.assertEqual(boost.effect(False, 1.05, 1.0), effect_off())
            self.assertEqual(boost.effect(True, 1.25, 1.0), base)
            self.assertEqual(boost.effect(True, 1.05, 0.0), effect_off())


if __name__ == '__main__':
    unittest.main()
