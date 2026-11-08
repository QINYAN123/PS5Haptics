"""Wuthering Waves skill-state reader tests with synthetic skill-bar frames (SDR and HDR-like)."""
import numpy as np
from ww_vision import WWVision, ROIS, REF_W, REF_H
from event_fx import EventFX, DEFAULT_CONFIG
from ww_rules import STATE_TRIGGERS


class FakeCam:
    width, height = REF_W, REF_H


def make_frame(v, bright, scale=1.0, gamma=1.0):
    """v: WWVision (for boxes); bright: {skill: True(ready) | False(cooldown) | None(HUD hidden)}."""
    x0, y0, x1, y1 = v.region
    f = np.full((y1 - y0, x1 - x0, 4), 20, np.uint8)
    rng = np.random.default_rng(len(str(bright)))
    for k, (x, y, w, h) in v.boxes.items():
        st = bright[k]
        if st is None:
            base = 25
        else:
            base = 170 if st else 60
        icon = np.clip(base + rng.normal(0, 12, (h, w, 3)), 0, 255)
        if st is False:                                   # white countdown digits on a dim icon
            icon[h // 3: h // 3 + 12, w // 3: w // 3 + 30] = 255
        icon = np.clip(255 * (icon / 255) ** gamma * scale, 0, 255)
        f[y:y + h, x:x + w, :3] = icon.astype(np.uint8)
    return f


def run_case(scale=1.0, gamma=1.0):
    press = {'l2': 0.0, 'r2': 0.0}
    v = WWVision(lambda: (press['l2'], press['r2']), camera=FakeCam(), ocr=False); v._setup()
    t = [0.0]

    def feed(state, n=10):
        for _ in range(n):
            t[0] += 0.05; v.step(make_frame(v, state, scale, gamma), t[0])

    ready = {'resonance': True, 'echo': True, 'liberation': True}
    feed(ready)
    feed({'resonance': True, 'echo': True, 'liberation': False})      # learn the dim level (no press: no cue)
    feed(ready)
    ev = v.poll()
    assert ('ww', 'liberation', 'ready') in ev, ev                       # cooldown over: ready cue
    press['r2'] = 1.0; feed(ready, 2); press['r2'] = 0.0
    feed({'resonance': True, 'echo': True, 'liberation': False})
    ev = v.poll()
    assert ('ww', 'liberation', 'cast') in ev, ev                        # pressed R2, icon dimmed: cast
    feed({'resonance': None, 'echo': None, 'liberation': None})           # HUD hidden (menu/cutscene)
    feed({'resonance': True, 'echo': True, 'liberation': True})          # HUD back
    ev = v.poll()
    assert not any(e[2] == 'cast' for e in ev), ev
    assert not any(e[2] == 'ready' and e[1] != 'liberation' for e in ev), ev   # no burst of ready cues
    press['l2'] = 1.0; feed(ready, 2); press['l2'] = 0.0
    feed({'resonance': True, 'echo': False, 'liberation': True})
    feed({'resonance': True, 'echo': True, 'liberation': True})
    press['l2'] = 1.0; feed(ready, 2); press['l2'] = 0.0
    feed({'resonance': True, 'echo': False, 'liberation': True})
    ev = v.poll()
    assert ('ww', 'echo', 'cast') in ev, ev
    return v


def test_sdr():
    run_case()


def test_hdr_like():
    # HDR / RenoDX tone mapping: brighter overall, different curve - relative judgement still works
    run_case(scale=1.5, gamma=0.7)
    run_case(scale=0.8, gamma=1.4)


def test_rules_and_state_triggers():
    cfg = dict(DEFAULT_CONFIG); cfg['log_dir'] = '.'
    fx = EventFX(cfg)
    assert fx.feed('ww', 'liberation', 'cast') == '大招释放'
    l, r = fx.triggers()
    assert r is not None and r[0] == 0x26                                # cast kick on R2
    skill, eff, gate = STATE_TRIGGERS['R']
    fx.set_state_trigger('R', eff)
    for _ in range(100):
        fx.render(240)                                                   # let the kick expire
    assert fx.triggers()[1][0] == 0x25                                   # back to the loaded 'gun' R2
    assert fx.feed('ww', 'echo', 'cast') == '声骸释放'
    assert fx.triggers()[0][0] == 0x26


def test_forte_release_and_speaker():
    from ww_vision import ForteState
    fs = ForteState(); t = 0.0; ev = []
    for frac in [0.0] * 5 + [0.1] * 10 + [0.0] * 3:
        t += 0.05; e = fs.update(frac, t)
        if e: ev.append(e)
    assert ev == ['release'], ev
    fs = ForteState(); ev = []
    for frac in [0.0, 0.1, 0.0, 0.1, 0.0]:                     # flicker: not a release
        t += 0.05; e = fs.update(frac, t)
        if e: ev.append(e)
    assert ev == [], ev
    cfg = dict(DEFAULT_CONFIG); cfg['log_dir'] = '.'
    fx = EventFX(cfg)
    assert fx.feed('ww', 'forte', 'release') == '共鸣回路释放'
    assert not fx.speaker_on()               # 鸣潮: grips only, the speaker is not event-driven any more


def test_click_gate():
    from xds import VirtualDualSense
    v = VirtualDualSense.__new__(VirtualDualSense)
    v.gate = {'L': None, 'R': 170}; v._gate_down = {'L': False, 'R': False}
    rep = bytearray(64); rep[0] = 1; rep[9] = 0x08
    seen = []
    for raw in (40, 120, 165, 175, 200, 150, 120, 60):
        rep[6] = raw; rep[9] = 0x08 if raw > 20 else 0
        out = v._apply_gate(bytes(rep))
        seen.append((out[6], bool(out[9] & 0x08)))
    assert seen[:3] == [(0, False)] * 3, seen                  # below the click: game sees nothing
    assert seen[3] == (255, True) and seen[5] == (255, True), seen   # past the click, held with hysteresis
    assert seen[-1] == (0, False), seen
    v.gate['R'] = None
    rep[6] = 90; rep[9] = 0x08
    assert v._apply_gate(bytes(rep))[6] == 90                  # on cooldown: untouched


def test_ocr_states():
    """Character-independent: a countdown on the icon = cooldown, no countdown = ready (echo),
    liberation also needs to be lit (charged). Rendered digits, SDR white and dimmer HDR white."""
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype('C:/Windows/Fonts/arialbd.ttf', 44)
    press = {'l2': 0.0, 'r2': 0.0}
    v = WWVision(lambda: (press['l2'], press['r2']), camera=FakeCam()); v._setup()
    assert v.ocr, v.error

    def frame(states, white=(255, 255, 255)):
        x0, y0, x1, y1 = v.region
        f = np.full((y1 - y0, x1 - x0, 4), 20, np.uint8)
        for k, (x, y, w, h) in v.boxes.items():
            st = states[k]
            col = (70, 160, 220) if st != 'grey' else (90, 90, 90)
            im = Image.new('RGB', (w, h), (40, 50, 70) if st == 'cd' else col)
            d = ImageDraw.Draw(im)
            if st == 'cd':
                d.text((w // 2, h // 2), '7.4', fill=white, font=font, anchor='mm')
            f[y:y + h, x:x + w, :3] = np.asarray(im)[..., ::-1]
        return f
    t = 0.0
    def feed(states, n=12, white=(255, 255, 255)):
        nonlocal t
        for _ in range(n):
            t += 0.05; v.step(frame(states, white), t)
    feed({'resonance': 'ready', 'echo': 'cd', 'liberation': 'ready'})
    assert v.states()['resonance'] is None, 'no countdown ever seen -> unknown, not ready'
    feed({'resonance': 'cd', 'echo': 'cd', 'liberation': 'cd'})
    feed({'resonance': 'ready', 'echo': 'cd', 'liberation': 'grey'})
    feed({'resonance': 'ready', 'echo': 'cd', 'liberation': 'ready'})
    feed({'resonance': 'ready', 'echo': 'cd', 'liberation': 'grey'})
    feed({'resonance': 'ready', 'echo': 'cd', 'liberation': 'ready'})
    st = v.states()
    assert st['echo'] is False and st['resonance'] is True and st['liberation'] is True, (st, v.cd)
    feed({'resonance': 'ready', 'echo': 'ready', 'liberation': 'ready'}, white=(232, 232, 232))
    assert v.states()['echo'] is True
    v.poll()
    press['r2'] = 1.0; feed({'resonance': 'ready', 'echo': 'ready', 'liberation': 'ready'}, 2); press['r2'] = 0.0
    feed({'resonance': 'ready', 'echo': 'ready', 'liberation': 'cd'}, white=(232, 232, 232))
    assert v.states()['liberation'] is False and ('ww', 'liberation', 'cast') in v.poll(), v.cd


if __name__ == '__main__':
    for f in (test_sdr, test_hdr_like, test_rules_and_state_triggers, test_forte_release_and_speaker, test_click_gate, test_ocr_states):
        f(); print('ok', f.__name__)
