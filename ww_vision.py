"""Wuthering Waves (鸣潮) skill-state reader from the screen - read-only, no injection, no input.

Skill-bar icon positions come from ok-ww's annotations (3840x2160 reference, scaled to the real
screen). Instead of ok-ww's fixed "white pixel" RGB threshold (which HDR / RenoDX tone mapping
breaks), every icon is judged RELATIVE to its own recent history: a ready icon is a fully lit
picture, an icon on cooldown is dimmed with a white countdown number.

v2 (after the first in-game session): ready-glow flashes pushed a min/max range far up and made
ready icons read as "cooldown". The split is now an Otsu threshold over the last ~30 s of values
(robust to short flashes), with hysteresis. A cast is also accepted when the icon simply drops
hard right after the matching trigger press, even if "ready" was missed.

Events (kind 'ww'):
  liberation|echo  ready / cast   (cast needs R2 / L2 pressed within 0.6 s)
  forte            release        (resonance circuit bar full -> empty quickly: special attack,
                                   e.g. Jinhsi; ok-ww's forte_full box, white fraction)
"""
import collections
import threading
import time
import numpy as np

REF_W, REF_H = 3840, 2160
ROIS = {                     # x, y, w, h at 3840x2160 (ok-ww box_resonance / box_echo / box_liberation)
    'resonance': (3185, 1877, 135, 113),
    'echo': (3371, 1877, 135, 117),
    'liberation': (3572, 1877, 126, 113),
}
FORTE_ROI = (2221, 1993, 120, 23)   # ok-ww forte_full box (2251..2311 x 1993..2016), a little wider
# Gamepad HUD (in combat with a controller): skill buttons are a column of round icons at the right,
# each with a button badge ("L2" echo, "R2" liberation, "R1" ...). The column shifts by one slot with
# the character, so the badges are read with OCR every 0.5 s to find where L2 / R2 currently are.
PAD_X = 3550                           # icon centre x
PAD_SLOTS = (1320, 1530, 1740, 1950)   # possible icon centre y
PAD_BADGE_X = 3672                     # badge centre x
PAD_HALF = 80                          # icon box half size
PAD_RING = (74, 96)                    # liberation energy ring radius range
PAD_REGION = (PAD_X - 100, PAD_SLOTS[0] - 100, PAD_BADGE_X + 50, PAD_SLOTS[-1] + 100)
CAST_WINDOW = 0.6
SIDE_OF = {'liberation': 'R', 'echo': 'L'}


def badge_layout(labels):
    """labels: OCR text per slot (top -> bottom). Returns {'echo': slot, 'liberation': slot} or None.
    OCR often drops the 'L' of L2, so R2 is the anchor: L2 is the slot right above it."""
    norm = [t.upper().replace(' ', '') for t in labels]
    r2 = next((i for i, t in enumerate(norm) if 'R' in t and t.endswith('2')), None)
    if r2 is None:
        r1 = next((i for i, t in enumerate(norm) if 'R' in t and t.endswith('1')), None)
        if r1 is not None and r1 >= 1 and norm[r1 - 1].endswith('2'):
            r2 = r1 - 1
    if r2 is None or r2 == 0:
        return None
    if not norm[r2 - 1].endswith('2'):          # the slot above must be the L2 badge
        return None
    return {'echo': r2 - 1, 'liberation': r2}


def ring_fill(bgra, cx, cy, r0, r1):
    """Fraction of the circle around (cx, cy) covered by a bright / coloured ring (liberation energy)."""
    h, w = bgra.shape[:2]
    a = np.linspace(0, 2 * np.pi, 72, endpoint=False)[:, None]; rad = np.arange(r0, r1, 3)[None, :]
    xs = np.clip((cx + rad * np.cos(a)).astype(int), 0, w - 1); ys = np.clip((cy + rad * np.sin(a)).astype(int), 0, h - 1)
    q = bgra[ys, xs, :3].astype(np.int32)                       # 72 angles x radii x 3, one numpy gather
    score = q.max(-1) - q.min(-1) + q.max(-1) // 3
    return float((score.max(1) > 150).mean())


def otsu(values):
    """Best two-class split of a 1-D sample (maximises between-class variance)."""
    v = np.sort(np.asarray(values, np.float32))
    n = len(v)
    if n < 20:
        return None
    c = np.cumsum(v); total = c[-1]
    k = np.arange(1, n)
    w0 = k / n; w1 = 1 - w0
    m0 = c[:-1] / k; m1 = (total - c[:-1]) / (n - k)
    i = int(np.argmax(w0 * w1 * (m0 - m1) ** 2))
    return float((v[i] + v[i + 1]) / 2), float(m0[i]), float(m1[i])


class IconState:
    def __init__(self):
        self.hist = collections.deque(maxlen=600)       # ~30 s at 20 fps
        self.ready = None; self.value = 0.0; self.thr = None; self.lo = None; self.hi = None
        self.n = 0; self.pending = 0; self.pending_state = None

    def update(self, v):
        self.value = v; self.hist.append(v); self.n += 1
        if self.n % 10 == 0 or self.thr is None:
            r = otsu(self.hist)
            if r is not None and r[2] - r[1] >= 25:      # both states seen and clearly apart
                self.thr, self.lo, self.hi = r
        if self.thr is None:
            return None
        band = 0.12 * (self.hi - self.lo)
        want = True if v >= self.thr + band else (False if v <= self.thr - band else self.ready)
        # debounce: a new state must hold for 3 frames (150 ms) - the first live session flipped
        # 1309 times in 18 min as values hovered around the split
        if want != self.ready:
            self.pending = self.pending + 1 if getattr(self, 'pending_state', None) == want else 1
            self.pending_state = want
            if self.pending >= 3 or self.ready is None:
                prev = self.ready
                self.ready = want; self.pending = 0
                if prev is not None:
                    return want
        else:
            self.pending = 0
        return None

    def span(self):
        return (self.hi - self.lo) if self.thr is not None else None


class ForteState:
    """Resonance-circuit bar: fraction of near-white pixels; full >= 6 %."""

    def __init__(self):
        self.full = False; self.value = 0.0; self.full_since = None; self.last_full = -9.0

    def update(self, frac, now):
        self.value = frac
        if frac >= 0.06:
            if not self.full:
                self.full_since = now
            self.full = True; self.last_full = now
            return None
        was = self.full
        self.full = False
        # released: was full (for a moment, not a flicker) and emptied within 0.5 s
        if was and frac <= 0.02 and self.full_since is not None and self.last_full - self.full_since >= 0.15:
            return 'release'
        return None


def shrink(bgra, f=5):
    """Block-average downscale (BGRA -> RGB uint8), e.g. 135x113 -> 27x22: small training crops."""
    h, w = (bgra.shape[0] // f) * f, (bgra.shape[1] // f) * f
    x = bgra[:h, :w, 2::-1].astype(np.float32).reshape(h // f, f, w // f, f, 3).mean(axis=(1, 3))
    return x.astype(np.uint8)


class CropRecorder:
    """Saves shrunken skill-icon crops + inputs at ~10 Hz into npz parts, to train a classifier on
    the player's own HDR screen (labels come later from press -> icon-drop events)."""

    def __init__(self, directory, every=2, part=1200):
        import pathlib
        self.dir = pathlib.Path(directory); self.dir.mkdir(parents=True, exist_ok=True)
        self.every = every; self.part = part; self.n = 0; self.parts = 0
        self.stamp = time.strftime('%Y%m%d-%H%M%S'); self.buf = collections.defaultdict(list)

    def add(self, frame, boxes, forte_box, l2, r2):
        self.n += 1
        if self.n % self.every:
            return
        self.buf['t'].append(time.time()); self.buf['l2'].append(l2); self.buf['r2'].append(r2)
        for k, (x, y, w, h) in boxes.items():
            self.buf[k].append(shrink(frame[y:y + h, x:x + w]))
        x, y, w, h = forte_box
        self.buf['forte'].append(shrink(frame[y:y + h, x:x + w], 3))
        if len(self.buf['t']) >= self.part:
            self.flush()

    def flush(self):
        if not self.buf['t']:
            return
        self.parts += 1
        np.savez_compressed(self.dir / f'ww-crops-{self.stamp}-{self.parts:03d}.npz',
                            **{k: np.asarray(v) for k, v in self.buf.items()})
        self.buf = collections.defaultdict(list)


class Debounced:
    """A boolean/None state that must hold for n frames before it is accepted."""

    def __init__(self, n=2):
        self.n = n; self.state = None; self.cand = None; self.count = 0

    def update(self, want):
        if want == self.state:
            self.count = 0
            return None
        if want == self.cand:
            self.count += 1
        else:
            self.cand = want; self.count = 1
        if self.count >= self.n:
            prev = self.state; self.state = want; self.count = 0
            if prev is not None and want is not None:
                return want
        return None


def luminance(bgra):
    rgb = bgra[..., :3].astype(np.float32)
    return 0.0722 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.2126 * rgb[..., 2]


def body_brightness(bgra):
    """Mean luminance without the brightest 10 % (countdown digits / glints)."""
    flat = np.sort(luminance(bgra).ravel())
    return float(flat[: int(len(flat) * 0.9)].mean())


class WWVision:
    def __init__(self, inputs, fps=20, camera=None, trace_path=None, mem=None, crops_dir=None, shots_dir=None, ocr=None):
        """inputs(): returns (l2, r2) pressed 0..1 from the real pad. mem(): MB, for diagnostics."""
        self.inputs = inputs; self.period = 1.0 / fps; self.mem = mem
        self.cam = camera; self.icons = {k: IconState() for k in ROIS}; self.forte = ForteState()
        self.events = collections.deque(maxlen=64)
        self.last_press = {'L': -9.0, 'R': -9.0}; self.prev_in = {'L': 0.0, 'R': 0.0}
        self.pre_press = {}; self.last_cast = {k: -9.0 for k in ROIS}
        self.stop_ev = threading.Event(); self.thread = None; self.frames = 0; self.error = None
        self.region = None; self.boxes = {}; self.forte_box = None
        self.trace = open(trace_path, 'a', encoding='utf8') if trace_path else None
        self.recorder = CropRecorder(crops_dir) if crops_dir else None
        # cooldown digits via OCR decide readiness (character independent); brightness only tells a
        # charged liberation from a grey, uncharged one. ocr=False disables (tests / no model).
        self.ocr = ocr; self.cd = {k: None for k in ROIS}; self.cd_seen = {k: False for k in ROIS}; self.final = {k: Debounced(2) for k in ROIS}
        self.shots_dir = shots_dir; self.shot_due = []; self.shots = 0
        self.pad = None; self.pad_time = -9.0; self.layout = '-'
        self.cd_last = {}; self.ring_on = {}; self.ready_since = {}

    def _setup(self):
        if self.ocr is None:
            try:
                from ww_ocr import CDReader
                self.ocr = CDReader()
            except Exception as exc:                      # no model: fall back to brightness only
                self.ocr = False; self.error = exc
        if self.cam is None:
            import dxcam
            self.cam = dxcam.create(output_color='BGRA')
        h, w = self.cam.height, self.cam.width
        sx, sy = w / REF_W, h / REF_H
        sc = lambda b: (int(b[0] * sx), int(b[1] * sy), max(4, int(b[2] * sx)), max(4, int(b[3] * sy)))
        boxes = {k: sc(b) for k, b in ROIS.items()}
        fb = sc(FORTE_ROI)
        pr = PAD_REGION; padb = (int(pr[0] * sx), int(pr[1] * sy), int((pr[2] - pr[0]) * sx), int((pr[3] - pr[1]) * sy))
        allb = list(boxes.values()) + [fb, padb]
        x0 = min(b[0] for b in allb); y0 = min(b[1] for b in allb)
        x1 = max(b[0] + b[2] for b in allb); y1 = max(b[1] + b[3] for b in allb)
        self.region = (x0, y0, x1, y1)
        self.boxes = {k: (b[0] - x0, b[1] - y0, b[2], b[3]) for k, b in boxes.items()}
        self.forte_box = (fb[0] - x0, fb[1] - y0, fb[2], fb[3])
        self.sx, self.sy, self.ox, self.oy = sx, sy, x0, y0

    def _pad_xy(self, x, y):
        return int(x * self.sx) - self.ox, int(y * self.sy) - self.oy

    def _pad_update(self, frame, now):
        """Read the button badges; remember where the L2 / R2 icons are."""
        hb, wb = int(30 * self.sy), int(42 * self.sx)
        labels = []
        for y in PAD_SLOTS:
            bx, by = self._pad_xy(PAD_BADGE_X, y)
            crop = frame[max(0, by - hb):by + hb, max(0, bx - wb):bx + wb, :3]
            labels.append(self.ocr.read_gray(crop.mean(-1).astype(np.uint8)) if crop.size else '')
        lay = badge_layout(labels)
        if lay:
            self.pad = {k: PAD_SLOTS[i] for k, i in lay.items()}; self.pad_time = now
        self.layout = '|'.join(labels)

    def _pad_wants(self, frame, now):
        """Ready = no countdown (+ full energy ring for liberation), held for 0.4 s. Going back to
        'not ready' is immediate. First live session: the charged ring's glow pulsed around the
        threshold and single frames missed the countdown -> R2 flipped every 0.1-0.2 s."""
        wants = {'resonance': None}
        hx, hy = int(PAD_HALF * self.sx), int(PAD_HALF * self.sy)
        for k, y in self.pad.items():
            cx, cy = self._pad_xy(PAD_X, y)
            if self.frames % 1 == 0:
                self.cd[k] = self.ocr.cooldown(frame[max(0, cy - hy):cy + hy, max(0, cx - hx):cx + hx])
                if self.cd[k] is not None:
                    self.cd_last[k] = (now, self.cd[k])
            t_cd, v_cd = self.cd_last.get(k, (-9.0, 0.0))
            in_cd = self.cd[k] is not None or (now - t_cd < 0.8 and v_cd > 0.8)   # a missed frame is not "ready"
            raw = not in_cd
            if raw and k == 'liberation':           # no countdown: ready only with a full energy ring (hysteresis)
                f = ring_fill(frame, cx, cy, int(PAD_RING[0] * self.sx), int(PAD_RING[1] * self.sx))
                self.ring_on[k] = f > 0.93 or (self.ring_on.get(k, False) and f > 0.75)
                raw = self.ring_on[k]
            if raw:
                self.ready_since[k] = self.ready_since.get(k) or now
                wants[k] = now - self.ready_since[k] >= 0.4
            else:
                self.ready_since[k] = None; wants[k] = False
        return wants

    def _cast(self, k, now):
        if now - self.last_cast[k] > 1.0:
            self.last_cast[k] = now
            self.events.append(('ww', k, 'cast'))

    def step(self, frame, now):
        """Process one captured region (BGRA). Public for tests."""
        l2, r2 = self.inputs()
        for side, val in (('L', l2), ('R', r2)):
            if val > 0.5 and self.prev_in[side] <= 0.5:          # press edge: remember the icon before it
                self.last_press[side] = now
                k = 'liberation' if side == 'R' else 'echo'
                self.pre_press[k] = self.icons[k].value
            self.prev_in[side] = val
        if self.recorder is not None:
            self.recorder.add(frame, self.boxes, self.forte_box, l2, r2)
        if self.ocr and self.frames % 5 == 0:                  # gamepad HUD: where are L2 / R2? (2 Hz at 10 fps)
            self._pad_update(frame, now)
        pad_on = bool(self.ocr) and self.pad is not None and now - self.pad_time < 2.0
        pad_wants = self._pad_wants(frame, now) if pad_on else None
        bright = {}
        for k, (x, y, w, h) in self.boxes.items():
            crop = frame[y:y + h, x:x + w]
            bright[k] = body_brightness(crop)
            self.icons[k].update(bright[k])
            if self.ocr and self.frames % 1 == 0 and not pad_on:   # keyboard HUD: OCR at 10 Hz
                self.cd[k] = self.ocr.cooldown(crop)
                if self.cd[k] is not None: self.cd_seen[k] = True
        hud_hidden = max(bright.values()) < 15                 # menus / cutscenes: skill bar not drawn
        flips = {}
        for k in ROIS:
            if pad_on:
                want = pad_wants.get(k)
            elif hud_hidden:
                want = None
            elif self.ocr:
                # 'no countdown' only means ready once a countdown was actually read in this box: proves
                # the box sits on the skill icon (wrong HUD layout -> never ready instead of always)
                want = (self.cd[k] is None) if self.cd_seen[k] else None
                if want and k == 'liberation' and self.icons[k].ready is False:
                    want = False                               # no countdown but grey: energy not full
            else:
                want = self.icons[k].ready
            f = self.final[k].update(want)
            if f is not None:
                flips[k] = f
        x, y, w, h = self.forte_box
        fy = luminance(frame[y:y + h, x:x + w])
        forte_ev = self.forte.update(float((fy > 225).mean()), now)
        # several icons flipping the same way in one frame = HUD shown / hidden, not a skill
        if not (len(flips) >= 2 and len(set(flips.values())) == 1):
            for k, ready in flips.items():
                if ready:
                    self.events.append(('ww', k, 'ready'))
                elif SIDE_OF.get(k) and now - self.last_press[SIDE_OF[k]] <= CAST_WINDOW:
                    self._cast(k, now)
            if forte_ev and not flips:
                self.events.append(('ww', 'forte', forte_ev))
        # validation snapshots: the full-resolution skill row at a press and 0.5 s later
        if self.shots_dir and self.shots < 300:
            for side in 'LR':
                if self.last_press[side] == now:
                    self.shot_due += [(now, side, 'press'), (now + 0.5, side, 'after')]
            due = [d for d in self.shot_due if d[0] <= now]
            self.shot_due = [d for d in self.shot_due if d[0] > now]
            for _t, side, tag in due[:2]:
                self._snapshot(frame, f'{side}-{tag}')
        # safety net: a hard drop right after the press is a cast even if 'ready' was missed
        for k, side in SIDE_OF.items():
            st = self.icons[k]; sp = st.span()
            if sp and not pad_on and k in self.pre_press and now - self.last_press[side] <= CAST_WINDOW:
                if self.pre_press[k] - st.value > 0.45 * sp:
                    self._cast(k, now); del self.pre_press[k]
        self.frames += 1
        if self.trace is not None and self.frames % 4 == 0:     # 5 lines/s: values, threshold, state
            ms = int(time.time() * 1000)
            self.trace.write(f'{ms}\tstate\t' + '\t'.join(
                f'{k}={s.value:.1f}/{(s.lo or 0):.1f}-{(s.hi or 0):.1f}/{s.ready}/cd={self.cd[k]}/final={self.final[k].state}'
                for k, s in self.icons.items())
                + f'\tforte={self.forte.value:.3f}\tL2={l2:.2f}\tR2={r2:.2f}\tpad={self.pad if pad_on else None}\tbadges={self.layout}\n')
            if self.mem is not None and self.frames % 200 == 0:
                self.trace.write(f'{ms}\tmem\t{self.mem()}\n')
        if self.trace is not None and (flips or forte_ev):
            self.trace.write(f'{int(time.time() * 1000)}\tflip\t{flips} forte={forte_ev}\n')
            self.trace.flush()

    def _snapshot(self, frame, tag):
        try:
            import pathlib
            from PIL import Image
            d = pathlib.Path(self.shots_dir); d.mkdir(parents=True, exist_ok=True)
            cds = '_'.join(f'{k[:3]}{self.cd[k] if self.cd[k] is not None else "-"}' for k in ROIS)
            Image.fromarray(frame[..., 2::-1]).save(d / f'{time.strftime("%H%M%S")}-{self.shots:03d}-{tag}-{cds}.png')
            self.shots += 1
        except Exception:
            pass

    def _loop(self):
        try:
            self._setup()
            while not self.stop_ev.is_set():
                t0 = time.monotonic()
                fr = self.cam.grab(region=self.region, new_frame_only=False)
                if fr is not None:
                    self.step(fr, t0)
                self.stop_ev.wait(max(0.0, self.period - (time.monotonic() - t0)))
        except Exception as exc:
            self.error = exc

    def start(self):
        if self.thread is None or not self.thread.is_alive():
            self.stop_ev.clear()
            self.thread = threading.Thread(target=self._loop, daemon=True); self.thread.start()

    def stop(self):
        self.stop_ev.set()
        if self.thread:
            self.thread.join(1.0)
        if self.recorder is not None:
            try: self.recorder.flush()
            except Exception: pass
        if self.trace is not None:
            try: self.trace.close()
            except Exception: pass
            self.trace = None

    def poll(self):
        out = []
        while self.events:
            out.append(self.events.popleft())
        return out

    def states(self):
        return {k: d.state for k, d in self.final.items()}
