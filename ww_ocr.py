"""Cooldown-number reader for Wuthering Waves skill icons - character independent.

Same idea as ok-ww (refresh_cd): keep only near-white pixels (the countdown text), turn them into
black-on-white, and run PP-OCRv5's recognition model (onnxocr's rec.onnx, Apache-2.0) on the
fixed icon crop - no text detection needed because the icon positions are known. A cooldown shows
'12.3'-style text (ok-ww's cd_regex \\d{1,2}\\.\\d). Only onnxruntime + numpy + Pillow are needed.

HDR: the white level of the captured UI may sit below SDR's 244; the threshold adapts to the
brightest pixels of a sufficiently contrasted crop, and the regex rejects anything that is not a
countdown, so bright icon art cannot be read as a cooldown.
"""
from collections import OrderedDict
import pathlib
import re
import sys
import numpy as np
from PIL import Image

CD_RE = re.compile(r'(\d{1,2})\.(\d)')


def model_dir():
    base = pathlib.Path(getattr(sys, '_MEIPASS', '')) / 'onnxocr' / 'models' / 'ppocrv5'
    if base.exists():
        return base
    import onnxocr
    return pathlib.Path(onnxocr.__file__).parent / 'models' / 'ppocrv5'


class CDReader:
    def __init__(self):
        import onnxruntime as ort
        d = model_dir()
        so = ort.SessionOptions(); so.intra_op_num_threads = 1; so.inter_op_num_threads = 1
        self.sess = ort.InferenceSession(str(d / 'rec' / 'rec.onnx'), so, providers=['CPUExecutionProvider'])
        chars = (d / 'ppocrv5_dict.txt').read_text('utf8').splitlines()
        self.cache = OrderedDict()
        self.chars = ['<blank>'] + chars + [' ']
        self.inp = self.sess.get_inputs()[0].name

    @staticmethod
    def white_mask(bgra, floor=200):
        mn = np.minimum(np.minimum(bgra[..., 0], bgra[..., 1]), bgra[..., 2])                                  # white = all three channels high
        top = np.percentile(mn, 99.7)
        if 120 <= top < floor and top - float(mn.mean()) >= 45:
            # A dim HUD can put the entire countdown below the SDR white floor.
            return mn >= top - 8
        thr = max(floor, min(244, top - 10))
        return mn >= thr

    def recognize(self, mask):
        """mask: bool HxW (True = text pixel) -> recognized string."""
        ys, xs = np.nonzero(mask)
        if len(ys) < 12:
            return ''
        y0, y1 = max(0, ys.min() - 4), min(mask.shape[0], ys.max() + 5)
        x0, x1 = max(0, xs.min() - 6), min(mask.shape[1], xs.max() + 7)
        img = np.where(mask[y0:y1, x0:x1], 0, 255).astype(np.uint8)        # black text on white
        return self.read_gray(img)

    def read_gray(self, img):
        """img: uint8 HxW grayscale, dark text on a light background -> recognized string."""
        h, w = img.shape
        nw = max(16, min(320, int(round(48 * w / max(1, h)))))
        im = Image.fromarray(img).resize((nw, 48), Image.BILINEAR)
        # Cache the exact grayscale network input, including empty recognitions.
        # A single changed digit must go through OCR again.
        key = im.tobytes()
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        im = im.convert('RGB')
        x = (np.asarray(im, np.float32) / 255.0 - 0.5) / 0.5
        x = x.transpose(2, 0, 1)[None]
        out = self.sess.run(None, {self.inp: x})[0][0]                     # T x classes
        idx = out.argmax(axis=1)
        text, prev = [], -1
        for i in idx:
            if i != prev and i != 0:
                text.append(self.chars[i] if i < len(self.chars) else '')
            prev = i
        result = ''.join(text)
        self.cache[key] = result
        if len(self.cache) > 48:
            self.cache.popitem(last=False)
        return result

    def cooldown(self, bgra):
        """Seconds of cooldown shown on the icon, or None if there is no countdown."""
        m = CD_RE.search(self.recognize(self.white_mask(bgra)))
        return float(f'{m.group(1)}.{m.group(2)}') if m else None
