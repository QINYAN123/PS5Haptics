"""Regression tests for the duplicate-interface error and repair policy."""
import unittest
from unittest.mock import patch

import usbip_vhci
import xds


class FakeVhci:
    def __init__(self, active, busy=(), fail_on=None):
        self.active = dict(active)
        self.busy = set(busy)
        self.fail_on = fail_on
        self.disabled = {}

    def interfaces(self):
        return set(self.active.values())

    def controllers(self, paths):
        return {device: {path} for device, path in self.active.items() if device != 'unrelated'}

    def idle(self, device):
        return device not in self.busy

    def disable(self, device):
        if device == self.fail_on:
            raise RuntimeError('disable failed')
        self.disabled[device] = self.active.pop(device)

    def enable(self, device):
        self.active[device] = self.disabled.pop(device)


class VhciTests(unittest.TestCase):
    def repair(self, vhci):
        usbip_vhci.repair_duplicates(vhci, log=lambda _: None)

    def test_exact_report_and_host_log_are_classified(self):
        err = ('Auto-attaching localhost client busID=1 deviceID=1: Failed to attach device '
               'error=exit status 1 port=3241 output=error: Multiple instances of VHCI device interface found.')
        self.assertEqual(xds.startup_error(err), xds.VHCI_HELP)
        self.assertEqual(xds.startup_error('host exited', [err]), xds.VHCI_HELP)
        self.assertIn(xds.USBIP_HELP, xds.startup_error('usbip-win2 驱动未就绪'))
        self.assertEqual(xds.startup_error('port 3241 in use'), 'port 3241 in use')

    def test_duplicate_preflight_does_not_spawn_host(self):
        vhci = FakeVhci({'first': 'a', 'second': 'b'})
        with patch.object(xds, 'Vhci', return_value=vhci), patch.object(xds.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(RuntimeError, 'VHCI'):
                xds.VirtualDualSense(log=lambda _: None).open()
            launch.assert_not_called()

    def test_keeps_active_controller_and_is_repeatable(self):
        vhci = FakeVhci({'first': 'a', 'second': 'b', 'third': 'c'}, busy=['second'])
        self.repair(vhci)
        self.repair(vhci)
        self.assertEqual(vhci.active, {'second': 'b'})
        self.assertEqual(vhci.disabled, {'first': 'a', 'third': 'c'})

    def test_keeps_first_idle_controller(self):
        vhci = FakeVhci({'second': 'b', 'first': 'a'})
        self.repair(vhci)
        self.assertEqual(vhci.active, {'first': 'a'})

    def test_multiple_busy_or_unknown_interface_are_left_alone(self):
        for vhci in (
            FakeVhci({'first': 'a', 'second': 'b'}, busy=['first', 'second']),
            FakeVhci({'first': 'a', 'unrelated': 'b'}),
            FakeVhci({}),
        ):
            with self.subTest(vhci=vhci), self.assertRaises(RuntimeError):
                self.repair(vhci)
            self.assertEqual(vhci.disabled, {})

    def test_disable_failure_rolls_back_prior_changes(self):
        active = {'first': 'a', 'second': 'b', 'third': 'c'}
        vhci = FakeVhci(active, fail_on='third')
        with self.assertRaisesRegex(RuntimeError, 'disable failed'):
            self.repair(vhci)
        self.assertEqual(vhci.active, active)
        self.assertEqual(vhci.disabled, {})


if __name__ == '__main__':
    unittest.main()
