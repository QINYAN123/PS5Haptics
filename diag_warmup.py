"""Warm-up hypothesis: keep one continuous stream open, send silence first, then the grip signal."""
import sys, time
import numpy as np
import sounddevice as sd
from diag_haptics import grip_signal, find, RATE

warm = float(sys.argv[1]) if len(sys.argv) > 1 else 1.5
sig = grip_signal(2.0)
buf = np.concatenate([np.zeros((int(warm * RATE), 4), np.float32), sig, np.zeros((int(0.3 * RATE), 4), np.float32)])
sd.play(buf, RATE, device=find('WASAPI'), blocking=True)
print('warmup', warm, 'done')
