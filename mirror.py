"""Audio mirror for other programs (RGB lighting "music mode" etc.).

With Dolby Atmos / spatial sound on, the default output's loopback is digital silence, so programs
that visualise "what is playing" (XsEngine / EchoRGB, many RGB suites) stop reacting. The mirror
captures the program that is currently playing (per-process capture works with Atmos) and plays a
copy into a silent virtual output - "Steam Streaming Speakers" by default - which the other program
is then pointed at. Runs on its own, independent of the haptics engine.
"""
import os
import threading
import time

import numpy as np

DEFAULT_TARGET = 'Steam Streaming Speakers'


def output_devices():
    """WASAPI render devices [(name, channels)] (for the UI)."""
    import pyaudiowpatch as pa
    from ds_audio import pa_new
    p = pa_new()
    try:
        w = p.get_host_api_info_by_type(pa.paWASAPI)['index']
        return [(d['name'], d['maxOutputChannels']) for d in (p.get_device_info_by_index(i) for i in range(p.get_device_count()))
                if d['hostApi'] == w and d['maxOutputChannels'] > 0 and not d.get('isLoopbackDevice')]
    finally:
        from ds_audio import pa_end
        pa_end(p)


class AudioMirror(threading.Thread):
    def __init__(self, log, target=DEFAULT_TARGET):
        super().__init__(daemon=True)
        self.log, self.target = log, target
        self.stop_ev = threading.Event(); self.source = ''; self.level = 0.0; self.error = None

    def stop(self):
        self.stop_ev.set()

    def run(self):
        import comtypes
        try: comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError: pass
        while not self.stop_ev.is_set():
            again = self._session()
            if not again:
                break
            self.stop_ev.wait(0.3)          # the haptics engine asked for a device re-scan: let it go first

    def _session(self):
        """One run on a PortAudio instance. True = released for a device re-scan, run again."""
        import pyaudiowpatch as pa
        import audio_sessions
        from proc_loopback import ProcessLoopback, RATE
        from ds_audio import pa_new, pa_end, pa_open, pa_gen
        gen = pa_gen(); again = False
        p = pa_new(); stream = None; pl = None; pid = None; check = 0.0; last_sound = 0.0
        try:
            w = p.get_host_api_info_by_type(pa.paWASAPI)['index']
            dev = next((d for d in (p.get_device_info_by_index(i) for i in range(p.get_device_count()))
                        if d['hostApi'] == w and not d.get('isLoopbackDevice') and d['maxOutputChannels'] > 0 and self.target in d['name']), None)
            if dev is None:
                raise RuntimeError(f'找不到输出设备「{self.target}」（装了 Steam 就有 Steam Streaming Speakers）')
            ch = dev['maxOutputChannels']
            stream = pa_open(p, format=pa.paFloat32, channels=ch, rate=RATE, output=True, output_device_index=dev['index'], frames_per_buffer=960)
            if not self.error and not getattr(self, '_said', False):
                self._said = True; self.log(f'音频镜像已开启：正在播放的声音会复制一份到「{dev["name"]}」')
            self.error = None
            me = os.getpid()
            while not self.stop_ev.is_set():
                if pa_gen() != gen:
                    again = True; break
                now = time.monotonic()
                if now - check > 2.0:
                    check = now
                    if pl is None or now - last_sound > 4.0:          # pick (or re-pick) the loudest playing program
                        try:
                            lst = [x for x in audio_sessions.playing() if x[0] > 0.003 and x[1] != me]
                        except Exception:
                            lst = []
                        new = lst[0][1] if lst else None
                        if new != pid:
                            if pl is not None:
                                pl.close(); pl = None
                            pid = new
                            if pid:
                                try:
                                    pl = ProcessLoopback(pid); self.source = lst[0][2]; last_sound = now
                                except Exception:
                                    pl = None; pid = None
                if pl is None:
                    stream.write(np.zeros(960 * ch, np.float32).tobytes()); continue
                try:
                    x = pl.read(20)
                except Exception:
                    pl.close(); pl = None; pid = None; continue
                if len(x):
                    if np.abs(x).max() > 1e-4:
                        last_sound = time.monotonic()
                    self.level = float(np.abs(x).max())
                    st = x.reshape(-1, 2)
                    out = st if ch == 2 else np.concatenate([st, np.zeros((len(st), ch - 2), np.float32)], 1)
                    stream.write(out.astype(np.float32).tobytes())
        except Exception as exc:
            self.error = str(exc); self.log('音频镜像出错：' + str(exc))
        finally:
            try:
                if pl is not None: pl.close()
                if stream is not None: stream.stop_stream(); stream.close()
            except Exception:
                pass
            pa_end(p)
        return again
