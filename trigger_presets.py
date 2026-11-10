"""Adaptive-trigger feel presets ("扳机手感"), one per trigger, each fine-tunable.

A preset is a starting point: picking one loads its numbers into the sliders (start, strength, and the
snap point or the frequency where they apply); the person then adjusts them. '声音震动' keeps the
default behaviour (the trigger vibrates with the sound). A game that drives the triggers itself still
wins where the routing says so (原生透传 with trigger mode 自动 / 叠加 / 纯用游戏).

Our own designs on the public DualSense trigger effect formats (0x21 feedback per zone,
0x25 weapon, 0x26 vibration); not taken from any other product.
"""
from ds_hid import effect_feedback, effect_vibration, effect_weapon, effect_zones

SOUND = '声音震动'
# name: kind and the numbers it starts from. start / end: % of the trigger travel; strength 1..8; freq Hz
PRESETS = {
    SOUND:      dict(kind=None),
    '轻阻力':   dict(kind='feedback', start=0, strength=3),
    '重阻力':   dict(kind='feedback', start=0, strength=7),
    '后段阻力': dict(kind='feedback', start=50, strength=6),
    '渐重':     dict(kind='slope', start=10, strength=8),
    '枪械扳机': dict(kind='weapon', start=30, end=60, strength=6),
    '连发震动': dict(kind='vibration', start=20, strength=6, freq=20),
}
NAMES = tuple(PRESETS)
PARAMS = ('start', 'strength', 'end', 'freq')
DEFAULTS = dict(start=0, strength=5, end=60, freq=20)            # sliders before any preset is picked
RANGES = dict(start=(0, 90), strength=(1, 8), end=(20, 80), freq=(5, 60))


def key(param, side):
    return f'trig_{param}_{side}'


def preset_key(side):
    return f'trig_preset_{side}'


def scene_defaults():
    """Config keys for both triggers, as stored per scene."""
    d = {}
    for side in 'LR':
        d[preset_key(side)] = SOUND
        for p in PARAMS:
            d[key(p, side)] = DEFAULTS[p]
    return d


def values_for(name):
    """The slider values a preset starts from."""
    v = dict(DEFAULTS)
    v.update({p: x for p, x in PRESETS.get(name, {}).items() if p in PARAMS})
    return v


def uses(name):
    """Which sliders matter for this preset."""
    kind = PRESETS.get(name, {}).get('kind')
    if kind is None:
        return ()
    return ('start', 'strength') + (('end',) if kind == 'weapon' else ('freq',) if kind == 'vibration' else ())


def _zone(pct):
    return max(0, min(9, int(round(float(pct) / 10))))


def effect(cfg, side):
    """Effect bytes for one trigger, or None when it should follow the sound."""
    name = cfg.get(preset_key(side), SOUND)
    kind = PRESETS.get(name, {}).get('kind')
    if kind is None:
        return None
    get = lambda p: cfg.get(key(p, side), values_for(name)[p])
    lo, hi = RANGES['strength']
    strength = max(lo, min(hi, int(round(float(get('strength'))))))
    start = _zone(get('start'))
    if kind == 'feedback':
        return effect_feedback(strength, start)
    if kind == 'slope':                          # light at the start point, full strength at the end
        n = 10 - start
        return effect_zones([0] * start + [max(1, round(1 + (strength - 1) * i / max(1, n - 1))) for i in range(n)])
    if kind == 'weapon':
        return effect_weapon(max(2, start), max(_zone(get('end')), max(2, start) + 1), strength)
    lo, hi = RANGES['freq']
    return effect_vibration(strength, max(lo, min(hi, int(round(float(get('freq')))))), start)


def effects(cfg):
    return effect(cfg, 'L'), effect(cfg, 'R')


def effect_name(e):
    """Short description of trigger effect bytes, for the self-check timeline."""
    if not e or e[0] in (0x00, 0x05):
        return '-'
    return {0x21: '阻力', 0x25: '枪械', 0x26: '震动'}.get(e[0], f'0x{e[0]:02x}')
