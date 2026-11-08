"""Wuthering Waves (鸣潮) event rules + trigger state, same perception principles as pbz_rules.py.

Layout (DualSense): L2 = Echo (声骸), R2 = Resonance Liberation (大招).
The triggers TELL the player what is ready: with a skill ready its trigger becomes a "gun"
trigger (resistance, then a click) and - in 原生透传 - the game only sees the press once the
trigger passes that click (gate threshold, raw 0..255). On cooldown the trigger is free and
passes through unchanged. Casting gives a kick on that trigger, a grip cue, and for the big
moments the grips get their own cue. The controller speaker is NOT event-driven here (screen
reading could not time the moments reliably) - it plays the game's sound as in 原声.
"""
from ds_hid import effect_weapon

R_HAND = [0.5, 1.0]
L_HAND = [1.0, 0.55]

# side -> (skill, effect while the skill is ready, gate threshold: game sees the press past here)
STATE_TRIGGERS = {
    'R': ('liberation', effect_weapon(3, 6, 6), 170),
    'L': ('echo', effect_weapon(2, 5, 4), 145),
}

RULES = [
    {"name": "大招释放", "kind": "ww", "fn_has": ["liberation"], "detail_has": ["cast"], "cooldown": 800,
     "group": "ww_skill", "priority": 90,
     "grip": [{"freq": 200, "amp": 0.9, "dur": 30, "attack": 1, "decay": 12, "pan": [1, 1]},
              {"freq": 70, "freq_to": 40, "amp": 1.0, "dur": 520, "attack": 8, "decay": 260, "pan": [1, 1]}],
     "native_gain": 0.6, "native_ms": 600,
     "trigger": {"side": "R", "effect": "vibration", "amp": 8, "freq": 30, "ms": 420}},
    {"name": "共鸣回路释放", "kind": "ww", "fn_has": ["forte"], "detail_has": ["release"], "cooldown": 1000,
     "group": "ww_skill", "priority": 80,
     "grip": [{"freq": 180, "amp": 0.8, "dur": 30, "attack": 1, "decay": 12, "pan": [1, 1]},
              {"freq": 90, "freq_to": 55, "amp": 0.85, "dur": 380, "attack": 6, "decay": 170,
               "pan_from": [0.6, 1.0], "pan_to": [1.0, 1.0]}],
     "native_gain": 0.7, "native_ms": 400},
    {"name": "大招就绪", "kind": "ww", "fn_has": ["liberation"], "detail_has": ["ready"], "cooldown": 1500,
     "grip": [{"freq": 230, "amp": 0.45, "dur": 18, "attack": 1, "decay": 7, "pan": R_HAND},
              {"freq": 230, "amp": 0.45, "dur": 18, "attack": 1, "decay": 7, "delay": 70, "pan": R_HAND}]},
    {"name": "声骸释放", "kind": "ww", "fn_has": ["echo"], "detail_has": ["cast"], "cooldown": 500,
     "group": "ww_skill", "priority": 70,
     "grip": [{"freq": 160, "amp": 0.7, "dur": 25, "attack": 1, "decay": 10, "pan": L_HAND},
              {"freq": 60, "amp": 0.7, "dur": 180, "attack": 4, "decay": 80, "pan": L_HAND}],
     "trigger": {"side": "L", "effect": "vibration", "amp": 7, "freq": 45, "ms": 160}},
    {"name": "声骸就绪", "kind": "ww", "fn_has": ["echo"], "detail_has": ["ready"], "cooldown": 1500,
     "grip": [{"freq": 230, "amp": 0.35, "dur": 16, "attack": 1, "decay": 6, "pan": L_HAND}]},
]
