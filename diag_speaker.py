"""Speaker test. Phase A: tones on ch1/ch2 with no HID change (two short beeps).
Phase B: HID enables speaker path (audio_control path=3) + speaker volume, then three rising beeps."""
import sys, time
import numpy as np
import pyaudiowpatch as pa
import hid
from ds_audio import find_dualsense_output, RATE
from ds_hid import find_path


def beeps(freqs, dur=0.35, gap=0.2):
    t = np.arange(int(dur * RATE)) / RATE
    parts = []
    for f in freqs:
        x = 0.9 * np.sin(2 * np.pi * f * t) * np.minimum(1, np.minimum(t, dur - t) * 60)
        parts += [x, np.zeros(int(gap * RATE))]
    mono = np.concatenate(parts).astype(np.float32)
    buf = np.zeros((len(mono), 4), np.float32); buf[:, 0] = mono; buf[:, 1] = mono
    return buf


def play(p, dev, buf):
    s = p.open(format=pa.paFloat32, channels=4, rate=RATE, output=True, output_device_index=dev['index'], frames_per_buffer=480)
    s.write(np.zeros((4800, 4), np.float32).tobytes()); s.write(buf.tobytes()); s.write(np.zeros((4800, 4), np.float32).tobytes())
    s.stop_stream(); s.close()


def speaker_on(vol=0x64):
    path, _ = find_path(); d = hid.device(); d.open_path(path)
    r = bytearray(48); r[0] = 0x02
    r[1] = 0x20 | 0x80          # speaker volume + audio control
    r[6] = vol                  # speaker volume
    r[8] = 3 << 4               # output path: speaker
    d.write(bytes(r)); d.close()


p = pa.PyAudio(); dev = find_dualsense_output(p)
phase = sys.argv[1] if len(sys.argv) > 1 else 'AB'
if 'A' in phase:
    play(p, dev, beeps([700, 700])); print('A done')
if 'B' in phase:
    time.sleep(1.0); speaker_on(); time.sleep(0.2); play(p, dev, beeps([500, 700, 950])); print('B done')
p.terminate()
