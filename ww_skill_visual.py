"""Read-only visual evidence for one Wuthering Waves skill icon."""
from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import time
import math

import numpy as np

SIZE = 48
Y, X = np.ogrid[-24:24, -24:24]
DIST = X*X+Y*Y
DISC = DIST <= 16**2
RING = (DIST >= 19**2) & (DIST <= 23**2)
INNER = DIST <= 8**2
OUTER = (DIST >= 14**2) & (DIST <= 17**2)
Y_GRAD = DISC[1:] & DISC[:-1]
X_GRAD = DISC[:, 1:] & DISC[:, :-1]


def crop_icon(frame, x, y, radius):
    """Square ROI input can be RGB, BGR or BGRA. Channel-order invariant."""
    if frame.ndim != 3 or frame.shape[2] < 3 or radius < 8:
        return None
    if x-radius < 0 or y-radius < 0 or x+radius > frame.shape[1] or y+radius > frame.shape[0]:
        return None
    return frame[y-radius:y+radius,x-radius:x+radius,:3]


def normalize(pixels):
    """Quantile scaling, with a floor to avoid amplifying nearly uniform crops."""
    value = pixels.mean(-1)
    low, high = np.quantile(np.concatenate((value[DISC], value[RING])), (.20,.95))
    return np.clip((value-low)/max(high-low,.16),0,1)


@lru_cache(maxsize=1)
def gamepad_models():
    try:
        data = json.loads((Path(__file__).resolve().parent / 'models' / 'ww-skill-visual.json').read_text('utf-8'))
        models = {}
        for skill in ('echo', 'liberation'):
            model = {k: np.asarray(data[skill]['relative'][k], dtype=float)
                     for k in ('center', 'scale', 'weights')}
            if ([v.shape for v in model.values()] != [(9,), (9,), (10,)]
                    or not all(np.isfinite(v).all() for v in model.values()) or np.any(model['scale'] <= 0)):
                return {}
            models[skill] = model
        return models
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def gamepad_score(frame, x, y, radius, skill):
    model = gamepad_models().get(skill)
    if model is None:
        return None
    return probability(features(crop_icon(frame, x, y, radius)), model)


def features(crop, relative=True):
    """Extract from an unsigned 8-bit square icon crop with its ring."""
    if crop is None or crop.dtype != np.uint8 or min(crop.shape[:2]) < 16:
        return None
    # Bilinear interpolation, with coordinates centered on equal-width sample cells.
    h,w=crop.shape[:2]
    y=(np.arange(SIZE)+.5)*h/SIZE-.5
    x=(np.arange(SIZE)+.5)*w/SIZE-.5
    yf=np.clip(np.floor(y).astype(int),0,h-2); xf=np.clip(np.floor(x).astype(int),0,w-2)
    wy=np.clip(y-yf,0,1)[:,None,None]; wx=np.clip(x-xf,0,1)[None,:,None]
    src=crop[...,:3]
    top=src[yf[:,None],xf]*(1-wx)+src[yf[:,None],xf+1]*wx
    bottom=src[(yf+1)[:,None],xf]*(1-wx)+src[(yf+1)[:,None],xf+1]*wx
    pixels=(top*(1-wy)+bottom*wy)/255
    chroma=np.ptp(pixels,axis=-1)
    value=pixels.mean(-1)
    if relative:
        value=normalize(pixels)
        neutral=chroma/np.maximum(.18,pixels.max(-1)) < .23
        strong=(value>.78)&neutral
        edges = (np.abs(np.diff(value,axis=0))[Y_GRAD].mean() +
                 np.abs(np.diff(value,axis=1))[X_GRAD].mean()) / 2
        mid, high = np.quantile(value[DISC], (.4, .9))
        return np.array([
            strong[DISC].mean(), (value[DISC]<.25).mean(), value[INNER].mean()-value[OUTER].mean(),
            high, high-mid,
            edges, (value[RING]>.75).mean(), value[RING].mean()-value[DISC].mean(),
            (value[DISC]>.94).mean()])
    inner=value[DISC]; border=value[RING]
    return np.array([
        ((pixels.min(-1)>.77)&(chroma<.16))[DISC].mean(), (inner<.32).mean(), (inner>.92).mean(),
        np.quantile(inner,.9), np.quantile(inner,.9)-np.quantile(inner,.4),
        (chroma[DISC]>.18).mean(), (chroma[RING]>.18).mean(), (border>.77).mean(), border.mean()-inner.mean()])


def probability(x, model):
    if x is None:
        return None
    z=float((x-model['center'])/model['scale'] @ model['weights'][:-1] + model['weights'][-1])
    return 1/(1+math.exp(-max(-60,min(60,z))))


@dataclass(frozen=True)
class Evidence:
    status: str
    score: float | None = None


def observe(crop, *, context, cooldown, model, relative=True):
    """Context comes from the caller. Score is visual evidence only, with cooldown as a veto."""
    if not context or crop is None:
        return Evidence('unknown')
    if cooldown:
        return Evidence('cooldown')
    score=probability(features(crop,relative),model)
    return Evidence('visual_only',score) if score is not None else Evidence('unknown')


class SkillShadow:
    """Rate-limited diagnostic. A model decision never leaves this class."""
    INTERVAL = 0.20
    SNAP_INTERVAL = 2.0
    MAX_CROPS = 48
    THRESHOLD = 0.50
    MODEL_ID = 'relative-v1'

    def __init__(self, model_path, trace, crop_dir=None):
        data = json.loads(Path(model_path).read_text(encoding='utf-8'))
        self.models = {skill: data[skill]['relative'] for skill in ('echo', 'liberation')}
        self.trace = trace
        self.crop_dir = crop_dir
        self.last_sample = {}
        self.last_crop = {}
        self.crops_saved = 0
        self.failed = False

    def sample(self, frame, x, y, radius, skill, existing, cooldown, *, layout):
        if self.failed or skill not in self.models or type(existing) is not bool:
            return
        now = time.monotonic()
        if now - self.last_sample.get(skill, -math.inf) < self.INTERVAL:
            return
        self.last_sample[skill] = now
        try:
            self._sample(frame, x, y, radius, skill, existing, cooldown, layout, now)
        except (OSError, ValueError, TypeError, IndexError, ArithmeticError) as exc:
            self.failed = True
            try:
                self.trace.write(f'{int(time.time()*1000)}\tshadow_error\t{exc!r}\n')
            except (OSError, ValueError):
                pass

    def _sample(self, frame, x, y, radius, skill, existing, cooldown, layout, now):
        if frame is None:
            return
        crop = crop_icon(frame, x, y, radius)
        score = probability(features(crop), self.models[skill])
        if score is None:
            return
        candidate = score >= self.THRESHOLD and not cooldown
        name = None
        if (candidate != existing and self.crop_dir is not None and self.crops_saved < self.MAX_CROPS
                and now - self.last_crop.get(skill, -math.inf) >= self.SNAP_INTERVAL):
            from PIL import Image
            self.crop_dir.mkdir(parents=True, exist_ok=True)
            name = f'{time.time_ns()}-{skill}.png'
            # Both consumers pass BGR or BGRA screen pixels.
            Image.fromarray(crop[..., ::-1]).save(self.crop_dir / name)
            self.last_crop[skill] = now
            self.crops_saved += 1
        row = dict(model=self.MODEL_ID, layout=layout, skill=skill, existing=existing,
                   score=round(score, 4), cooldown=cooldown, candidate=candidate,
                   different=candidate != existing, crop=name)
        self.trace.write(f'{int(time.time()*1000)}\tshadow\t{json.dumps(row, separators=(",", ":"))}\n')
