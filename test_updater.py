"""Release metadata, interrupted downloads and local Windows update application."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import updater


class Response(io.BytesIO):
    def __init__(self, data, length=0):
        super().__init__(data)
        self.headers = {'Content-Length': str(length)} if length else {}


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dst = self.root / 'download.zip'
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr("app/PS5Haptics.exe", b'new binary')
            z.writestr("app/_internal/example.dll", b'new library')
            z.writestr("app/config.json", b'package default')
        self.data = buf.getvalue()
        self.digest = 'sha256:' + hashlib.sha256(self.data).hexdigest()

    def test_metadata_and_verified_download(self):
        release = {'tag_name': 'v3.4.5', 'assets': [
            {'name': 'PS5Haptics-update-v3.4.5.zip', 'size': len(self.data),
             'browser_download_url': 'asset', 'digest': self.digest}]}
        with patch.object(updater, '_get', side_effect=[
                Response(json.dumps(release).encode()), Response(self.data)]):
            info = updater.check(local='2.3.17')
            updater.download(info['url'], self.dst, expected_size=info['size'], digest=info['digest'])
        self.assertEqual(self.dst.read_bytes(), self.data)

    def test_download_errors_preserve_previous_file_and_allow_retry(self):
        self.dst.write_bytes(b'previous file')
        for body, length, expected, digest in [
                (self.data, 0, 0, 'sha256:' + '0' * 64),
                (self.data, 0, len(self.data) + 1, None),
                (self.data, len(self.data) + 2, 0, None),
                (b'incomplete', 0, 0, None)]:
            with self.subTest(length=length, expected=expected, digest=digest):
                with patch.object(updater, '_get', return_value=Response(body, length)):
                    with self.assertRaises(RuntimeError):
                        updater.download('asset', self.dst, expected_size=expected, digest=digest)
                self.assertEqual(self.dst.read_bytes(), b'previous file')
                self.assertEqual(list(self.root.iterdir()), [self.dst])
        with patch.object(updater, '_get', return_value=Response(self.data)):
            updater.download('asset', self.dst)
        self.assertEqual(self.dst.read_bytes(), self.data)

    def test_bad_zip_and_failed_launch_clean_the_stage(self):
        with patch.object(tempfile, 'tempdir', str(self.root)):
            self.dst.write_bytes(b'broken')
            with self.assertRaises(zipfile.BadZipFile):
                updater.prepare(self.dst)
            self.assertEqual(list(self.root.iterdir()), [self.dst])
            self.dst.write_bytes(self.data)
            with patch.object(updater, 'run_script', side_effect=OSError('helper failed')):
                with self.assertRaises(OSError):
                    updater.start_update(self.dst)
            self.assertEqual(list(self.root.iterdir()), [self.dst])

    @unittest.skipUnless(os.name == 'nt', 'uses Windows PowerShell and robocopy')
    def test_apply_keeps_settings_and_waits_for_running_app(self):
        target = self.root / "test app's folder"
        target.mkdir()
        (target / 'config.json').write_bytes(b'my settings')
        exe = target / 'PS5Haptics.exe'
        exe.write_bytes(b'old')
        for still_running in (True, False):
            self.dst.write_bytes(self.data)
            # Use the current Python process for the timeout case. Stub the wait
            # to finish instantly. The successful case uses an already exited PID.
            with subprocess.Popen(['cmd', '/c', 'exit', '0']) as process:
                process.wait()
                pid = os.getpid() if still_running else process.pid
            script = updater.prepare(self.dst, target=target, pid=pid)
            runner = self.root / 'run.ps1'
            runner.write_text(
                'function Wait-Process { process {} }\n'
                'function Start-Process { param($FilePath,$WorkingDirectory) }\n'
                + "& '" + str(script).replace("'", "''") + "'\n", 'utf-8-sig')
            subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(runner)],
                           capture_output=True, check=True, timeout=15)
            self.assertEqual(exe.read_bytes(), b'old' if still_running else b'new binary')
            self.assertEqual((target / 'config.json').read_bytes(), b'my settings')
            self.assertEqual((target / '_internal/example.dll').exists(), not still_running)
            self.assertFalse(script.parent.exists())
            self.assertFalse(self.dst.exists())


if __name__ == '__main__':
    unittest.main()
