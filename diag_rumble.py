"""HID rumble emulation test: valid_flag0 bit0 (compatible vibration) + bit1 (haptics select),
byte3 = right (high-freq) motor, byte4 = left (low-freq) motor. Drives the grip actuators
without the audio path."""
import time
from ds_hid import DualSenseHID

h = DualSenseHID()
def rumble(left, right):
    b = bytearray(48); b[0] = 0x02; b[1] = 0x03; b[3] = right; b[4] = left
    h.dev.write(bytes(b))
for _ in range(3):
    rumble(255, 0); time.sleep(0.35); rumble(0, 0); time.sleep(0.25)
for _ in range(3):
    rumble(0, 255); time.sleep(0.35); rumble(0, 0); time.sleep(0.25)
rumble(0, 0); h.close(); print('rumble test done')
