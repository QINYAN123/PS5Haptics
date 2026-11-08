"""Phantom Blade Zero event-tuning rules (PS5 DualSense), designed from touch perception.

Perception model used (and checked by sim_feel.py):
  * Pacinian channel, most sensitive 150-300 Hz: reads as crisp / sharp / metal contact.
    Non-Pacinian (Meissner) 20-60 Hz: reads as weight, body impact, mass.
  * Duration: < 50 ms = click/tick, 100-300 ms = thump, > 400 ms = rumble.
    Two clicks 40-80 ms apart are clearly felt as two (vibrotactile gap detection ~10-20 ms).
  * Forward masking: after a strong vibration, weaker cues within ~100-200 ms are blunted, and
    sustained strong vibration adapts (numbs) the hand. So frequent events (the player's own
    attacks) are kept light and the official layer is ducked under signature cues.
  * Side carries meaning: parry is the left hand (L1), attacks the right hand; hits to the body
    are felt in both hands.
  * Triggers follow the controls: L2 = weapon art, R2 = Shachi weapon. Parry/hit never touch
    the triggers so the next input is never resisted.

Rule fields: name, kind, detail_has/detail_not/fn_has/fn_not, cooldown (ms),
  group + priority (+ suppress_ms, masks): one cue per group at a time, stronger wins,
  grip: list of voices {freq, freq_to, amp, amp_to, dur, attack, decay, delay, pan | pan_from/pan_to,
         am_freq, am_depth},
  native_gain + native_ms (scale the game's own haptics; highest priority wins),
  trigger: {side L/R/LR, effect vibration|feedback, amp|strength, freq, zone, ms}.
"""

L_HAND = [1.0, 0.55]       # left-hand emphasis (parry)
R_HAND = [0.5, 1.0]        # right-hand emphasis (attacks, Shachi weapon)
BOTH = [1.0, 1.0]

RULES = [
    # ------------------------------------------------------------ guard break (masks everything defensive)
    {"name": "破防", "kind": "shake", "detail_has": ["CameraShake_PlayerHit_Level4"],
     "group": "body", "priority": 100, "suppress_ms": 450, "masks": ["defense", "attack"], "cooldown": 500,
     "grip": [{"freq": 150, "amp": 0.8, "dur": 25, "attack": 1, "decay": 10, "pan": BOTH},
              {"freq": 70, "amp": 1.0, "dur": 140, "attack": 2, "decay": 70, "pan": BOTH},
              {"freq": 45, "amp": 0.9, "dur": 520, "attack": 20, "decay": 280, "delay": 80,
               "am_freq": 7, "am_depth": 0.7, "pan": [1.0, 0.85]}],
     "native_gain": 0.45, "native_ms": 520},

    # ------------------------------------------------------------ defense (left hand)
    {"name": "完美闪避反击", "speaker_eq": "whoosh", "speaker_ms": 700, "kind": "deflect", "detail_has": ["PerfectCounter", "|dodge"],
     "group": "defense", "priority": 92, "cooldown": 300,
     "grip": [{"freq": 110, "amp": 0.55, "dur": 160, "attack": 30, "decay": 0, "pan_from": [1, 0.15], "pan_to": [0.15, 1]},
              {"freq": 220, "amp": 0.9, "dur": 25, "attack": 1, "decay": 10, "delay": 150, "pan": R_HAND}],
     "native_gain": 0.45, "native_ms": 300},
    {"name": "完美闪避", "speaker_eq": "whoosh", "speaker_ms": 600, "kind": "deflect", "detail_has": ["Perfect", "|dodge"],
     "group": "defense", "priority": 90, "cooldown": 300,
     "grip": [{"freq": 110, "amp": 0.55, "dur": 170, "attack": 35, "decay": 0, "pan_from": [1, 0.15], "pan_to": [0.15, 1]}],
     "native_gain": 0.45, "native_ms": 240},
    {"name": "完美反击", "speaker_eq": "clang", "speaker_ms": 700, "kind": "deflect", "detail_has": ["Deflect.Result.PerfectCounter"],
     "group": "defense", "priority": 95, "cooldown": 250,
     "grip": [{"freq": 230, "amp": 1.0, "dur": 25, "attack": 1, "decay": 10, "pan": L_HAND},
              {"freq": 90, "freq_to": 160, "amp": 0.45, "amp_to": 0.8, "dur": 150, "attack": 10, "decay": 0,
               "delay": 60, "pan_from": [1, 0.4], "pan_to": [0.4, 1]}],
     "native_gain": 0.4, "native_ms": 300},
    {"name": "完美格挡", "speaker_eq": "clang", "speaker_ms": 550, "kind": "deflect", "detail_has": ["Deflect.Result.Perfect"],
     "group": "defense", "priority": 80, "cooldown": 150,
     "grip": [{"freq": 230, "amp": 1.0, "dur": 25, "attack": 1, "decay": 10, "pan": L_HAND},
              {"freq": 230, "amp": 0.85, "dur": 25, "attack": 1, "decay": 10, "delay": 55, "pan": L_HAND},
              {"freq": 200, "amp": 0.45, "dur": 200, "attack": 3, "decay": 80, "delay": 85, "pan": L_HAND}],   # ring after the 2nd click: keep the gap
     "native_gain": 0.35, "native_ms": 280},
    {"name": "精准格挡", "speaker_eq": "clang", "speaker_ms": 420, "kind": "deflect", "detail_has": ["Deflect.Result.Great"],
     "group": "defense", "priority": 70, "cooldown": 150,
     "grip": [{"freq": 200, "amp": 0.8, "dur": 30, "attack": 1, "decay": 12, "pan": L_HAND},
              {"freq": 150, "amp": 0.2, "dur": 90, "attack": 3, "decay": 35, "pan": L_HAND}],
     "native_gain": 0.5, "native_ms": 240},
    {"name": "普通格挡", "speaker_eq": "clang", "speaker_ms": 320, "kind": "deflect", "detail_has": ["Deflect.Result.Normal"], "detail_not": ["|dodge"],
     "group": "defense", "priority": 50, "suppress_ms": 90, "cooldown": 90,
     "grip": [{"freq": 65, "amp": 0.5, "dur": 60, "attack": 3, "decay": 25, "pan": [1.0, 0.7]},
              {"freq": 140, "amp": 0.3, "dur": 12, "attack": 1, "decay": 5, "pan": [1.0, 0.7]}],
     "native_gain": 0.55, "native_ms": 220},
    {"name": "防御受击", "kind": "ffb", "detail_has": ["DefenceForceFeedback"],
     "group": "defense", "priority": 40, "cooldown": 120,
     "grip": [{"freq": 45, "amp": 0.45, "dur": 130, "attack": 10, "decay": 60, "pan": [1.0, 0.6]}]},

    # ------------------------------------------------------------ hits taken (body, both hands)
    {"name": "受击·重", "kind": "shake", "detail_has": ["CameraShake_PlayerHit_Level3"],
     "group": "body", "priority": 80, "suppress_ms": 200, "cooldown": 150,
     "grip": [{"freq": 42, "amp": 0.85, "dur": 230, "attack": 4, "decay": 110, "pan": BOTH},
              {"freq": 120, "amp": 0.4, "dur": 20, "attack": 1, "decay": 8, "pan": BOTH}],
     "native_gain": 0.65, "native_ms": 260},
    {"name": "受击·中", "kind": "shake", "detail_has": ["CameraShake_PlayerHit_Level2"],
     "group": "body", "priority": 60, "suppress_ms": 160, "cooldown": 150,
     "grip": [{"freq": 50, "amp": 0.65, "dur": 160, "attack": 4, "decay": 80, "pan": BOTH}],
     "native_gain": 0.7, "native_ms": 220},
    {"name": "受击·轻", "kind": "shake", "detail_has": ["CameraShake_PlayerHit_Level1"],
     "group": "body", "priority": 40, "suppress_ms": 120, "cooldown": 150,
     "grip": [{"freq": 60, "amp": 0.45, "dur": 90, "attack": 4, "decay": 40, "pan": BOTH}],
     "native_gain": 0.8, "native_ms": 180},

    # ------------------------------------------------------------ player's own attacks (frequent: light, right hand)
    {"name": "攻击·收招", "kind": "shake", "detail_has": ["CameraShake_PlayerAttack_Level4"],
     "group": "attack", "priority": 40, "suppress_ms": 80, "cooldown": 60,
     "grip": [{"freq": 160, "amp": 0.75, "dur": 30, "attack": 1, "decay": 12, "pan": R_HAND},
              {"freq": 60, "amp": 0.55, "dur": 150, "attack": 4, "decay": 60, "pan": [0.6, 1.0]}],
     "native_gain": 0.9, "native_ms": 320},
    {"name": "攻击·重", "kind": "shake", "detail_has": ["CameraShake_PlayerAttack_Level3"],
     "group": "attack", "priority": 30, "suppress_ms": 80, "cooldown": 60,
     "grip": [{"freq": 170, "amp": 0.6, "dur": 25, "attack": 1, "decay": 10, "pan": R_HAND},
              {"freq": 70, "amp": 0.35, "dur": 80, "attack": 3, "decay": 35, "pan": R_HAND}],
     "native_gain": 0.75, "native_ms": 300},
    {"name": "攻击·中", "kind": "shake", "detail_has": ["CameraShake_PlayerAttack_Level2"],
     "group": "attack", "priority": 20, "suppress_ms": 80, "cooldown": 60,
     "grip": [{"freq": 180, "amp": 0.45, "dur": 22, "attack": 1, "decay": 9, "pan": R_HAND},
              {"freq": 80, "amp": 0.2, "dur": 45, "attack": 3, "decay": 18, "pan": R_HAND}],
     "native_gain": 0.7, "native_ms": 280},
    {"name": "攻击·轻", "kind": "shake", "detail_has": ["CameraShake_PlayerAttack_Level"], "detail_not": ["Level2", "Level3", "Level4"],
     "group": "attack", "priority": 10, "suppress_ms": 80, "cooldown": 60,
     "grip": [{"freq": 190, "amp": 0.28, "dur": 16, "attack": 1, "decay": 7, "pan": R_HAND}],
     "native_gain": 0.6, "native_ms": 260},
    {"name": "破军·蓄力命中", "kind": "shake", "detail_has": ["CameraShake_PoJun_Charge_Hit"],
     "group": "attack", "priority": 35, "suppress_ms": 80, "cooldown": 60,
     "grip": [{"freq": 150, "amp": 0.4, "dur": 20, "attack": 1, "decay": 8, "pan": R_HAND},
              {"freq": 55, "amp": 0.5, "dur": 100, "attack": 3, "decay": 45, "pan": R_HAND}],
     "native_gain": 0.75, "native_ms": 300},

    # ------------------------------------------------------------ weapon art (L2), Shachi weapons (R2), charge
    {"name": "破军·满蓄释放", "kind": "ffb", "detail_has": ["ForceFeedback_PoJun_Charge_Phase3"],
     "group": "weapon", "priority": 80, "cooldown": 300,
     "grip": [{"freq": 180, "amp": 0.9, "dur": 30, "attack": 1, "decay": 12, "pan": R_HAND},
              {"freq": 60, "amp": 0.9, "dur": 320, "attack": 4, "decay": 160, "pan": [0.7, 1.0]}],
     "native_gain": 0.6, "native_ms": 350},
    {"name": "武技成功", "kind": "ffb", "detail_has": ["CombatArt_Success"],
     "group": "weapon", "priority": 70, "cooldown": 400,
     "grip": [{"freq": 60, "freq_to": 90, "amp": 0.25, "amp_to": 0.9, "dur": 400, "attack": 20, "decay": 0, "pan": [1.0, 0.7]}],
     "native_gain": 0.8, "native_ms": 420,
     "trigger": {"side": "L", "effect": "vibration", "amp": 7, "freq": 40, "ms": 160}},
    {"name": "煞器·巨斧二段", "kind": "ffb", "detail_has": ["ForceFeedback_BrutalAxe_Phase2"],
     "group": "weapon", "priority": 75, "cooldown": 200,
     "grip": [{"freq": 160, "amp": 0.6, "dur": 30, "attack": 1, "decay": 12, "pan": R_HAND},
              {"freq": 35, "amp": 1.0, "dur": 380, "attack": 4, "decay": 180, "pan": [0.7, 1.0]}],
     "trigger": {"side": "R", "effect": "vibration", "amp": 8, "freq": 25, "ms": 380}},
    {"name": "煞器·巨斧", "kind": "ffb", "detail_has": ["ForceFeedback_BrutalAxe_Phase1"],
     "group": "weapon", "priority": 60, "cooldown": 200,
     "grip": [{"freq": 40, "amp": 0.85, "dur": 260, "attack": 3, "decay": 120, "pan": [0.7, 1.0]}],
     "trigger": {"side": "R", "effect": "vibration", "amp": 8, "freq": 30, "ms": 260}},
    {"name": "煞器·狮头起", "kind": "ffb", "detail_has": ["ForceFeedback_LionHead_Start"],
     "group": "weapon", "priority": 60, "cooldown": 200,
     "grip": [{"freq": 70, "amp": 0.2, "amp_to": 0.5, "dur": 220, "attack": 20, "decay": 0, "pan": R_HAND}],
     "trigger": {"side": "R", "effect": "vibration", "amp": 6, "freq": 35, "ms": 400}},
    {"name": "煞器·狮头持续", "kind": "ffb", "detail_has": ["ForceFeedback_LionHead_Loop"],
     "group": "weapon", "priority": 55, "cooldown": 200,
     "grip": [{"freq": 55, "amp": 0.4, "dur": 600, "attack": 30, "decay": 0, "am_freq": 12, "am_depth": 0.45, "pan": R_HAND}],
     "trigger": {"side": "R", "effect": "vibration", "amp": 4, "freq": 25, "ms": 600}},
    {"name": "煞器·狮头收", "kind": "ffb", "detail_has": ["ForceFeedback_LionHead_End"],
     "group": "weapon", "priority": 65, "cooldown": 200,
     "grip": [{"freq": 50, "amp": 0.8, "dur": 200, "attack": 3, "decay": 90, "pan": [0.7, 1.0]}],
     "trigger": {"side": "R", "effect": "vibration", "amp": 8, "freq": 30, "ms": 150}},

    # ------------------------------------------------------------ stealth kill: slow push, then the stab
    {"name": "暗杀", "kind": "ffb", "detail_has": ["Stealth"], "group": "weapon", "priority": 85, "cooldown": 600,
     "grip": [{"freq": 45, "amp": 0.2, "amp_to": 0.6, "dur": 180, "attack": 30, "decay": 0, "pan": BOTH},
              {"freq": 210, "amp": 1.0, "dur": 35, "attack": 1, "decay": 14, "delay": 180, "pan": R_HAND},
              {"freq": 80, "amp": 0.8, "dur": 200, "attack": 3, "decay": 90, "delay": 190, "pan": BOTH}],
     "native_gain": 0.5, "native_ms": 400},

    # ------------------------------------------------------------ traversal: ladder hands / feet on their side
    {"name": "爬梯·右手", "kind": "ffb", "detail_has": ["ClimbLadder_RightHand"], "cooldown": 80,
     "grip": [{"freq": 90, "amp": 0.3, "dur": 40, "attack": 2, "decay": 15, "pan": [0.0, 1.0]}]},
    {"name": "爬梯·左手", "kind": "ffb", "detail_has": ["ClimbLadder_LeftHand"], "cooldown": 80,
     "grip": [{"freq": 90, "amp": 0.3, "dur": 40, "attack": 2, "decay": 15, "pan": [1.0, 0.0]}]},
    {"name": "爬梯·右脚", "kind": "ffb", "detail_has": ["ClimbLadder_RightFoot"], "cooldown": 80,
     "grip": [{"freq": 60, "amp": 0.2, "dur": 50, "attack": 3, "decay": 20, "pan": [0.0, 1.0]}]},
    {"name": "爬梯·左脚", "kind": "ffb", "detail_has": ["ClimbLadder_LeftFoot"], "cooldown": 80,
     "grip": [{"freq": 60, "amp": 0.2, "dur": 50, "attack": 3, "decay": 20, "pan": [1.0, 0.0]}]},

    # ------------------------------------------------------------ official force-feedback levels: real grading
    {"name": "力反馈1档", "kind": "ffb", "detail_has": ["ForceFeedbackLevel1_PS5"], "priority": 10, "native_gain": 0.4, "native_ms": 380},
    {"name": "力反馈2档", "kind": "ffb", "detail_has": ["ForceFeedbackLevel2_PS5"], "priority": 10, "native_gain": 0.6, "native_ms": 380},
    {"name": "力反馈3档", "kind": "ffb", "detail_has": ["ForceFeedbackLevel3_PS5"], "priority": 10, "native_gain": 0.8, "native_ms": 380},
    {"name": "力反馈4档", "kind": "ffb", "detail_has": ["ForceFeedbackLevel4_PS5"], "priority": 10, "native_gain": 1.15, "native_ms": 380},
]
