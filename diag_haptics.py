"""Three-step haptics diagnosis: HID trigger, shared WASAPI grip, exclusive WASAPI grip, WDM-KS grip."""
import sys, time
import numpy as np
import sounddevice as sd
from ds_hid import DualSenseHID, effect_vibration, effect_off

RATE = 48000


def grip_signal(sec=2.0):
    t = np.arange(int(sec * RATE)) / RATE
    env = (np.sin(2 * np.pi * 2 * t) > 0).astype(np.float32)           # 2 Hz on/off pulses
    x = (0.95 * np.sin(2 * np.pi * 120 * t) * env).astype(np.float32)
    buf = np.zeros((len(t), 4), np.float32); buf[:, 2] = x; buf[:, 3] = x
    return buf


def find(api_name, contains='DualSense', ch=4):
    for i, d in enumerate(sd.query_devices()):
        if contains in d['name'] and d['max_output_channels'] >= ch and api_name in sd.query_hostapis(d['hostapi'])['name']:
            return i
    return None


def step(n):
    if n == 1:
        h = DualSenseHID(); h.set_triggers(effect_vibration(8, 40), effect_vibration(8, 40), force=True); time.sleep(2); h.close(); return 'trigger'
    if n == 2:
        sd.play(grip_signal(), RATE, device=find('WASAPI'), blocking=True); return 'wasapi-shared'
    if n == 3:
        sd.play(grip_signal(), RATE, device=find('WASAPI'), blocking=True, extra_settings=sd.WasapiSettings(exclusive=True)); return 'wasapi-exclusive'
    if n == 4:
        sd.play(grip_signal(), RATE, device=find('WDM-KS'), blocking=True); return 'wdm-ks'


if __name__ == '__main__':
    for n in map(int, sys.argv[1:]):
        try:
            print(n, step(n), 'ok', flush=True)
        except Exception as e:
            print(n, 'error', e, flush=True)
        time.sleep(1.5)
