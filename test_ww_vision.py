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
    v = VirtualDualSense()
    v.gate['R'] = 170
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
    rep[6], rep[9] = 135, 0x08
    assert v._apply_gate(bytes(rep))[6] == 0                     # icon changed mid-pull
    rep[6], rep[9] = 0, 0
    v._apply_gate(bytes(rep))
    rep[6] = 90; rep[9] = 0x08
    assert v._apply_gate(bytes(rep))[6] == 90                  # on cooldown: untouched
    v.gate['R'] = 170
    assert v._apply_gate(bytes(rep))[6] == 90                  # newly ready: don't interrupt an analog pull
    rep[6], rep[9] = 0, 0
    v._apply_gate(bytes(rep))
    rep[6], rep[9] = 140, 0x08
    assert v._apply_gate(bytes(rep))[6] == 0                  # fresh pull uses the new click point


def test_unforwarded_trigger_does_not_count_as_a_skill_press():
    from types import SimpleNamespace
    from xds import VirtualDualSense

    pad = VirtualDualSense()
    pad.sock = SimpleNamespace(sendall=lambda _: None)
    pad.gate['R'] = 170
    vision = WWVision(lambda: pad.trigger_input, camera=FakeCam(), ocr=False)
    vision._setup()
    frame = make_frame(vision, {'resonance': False, 'echo': False, 'liberation': False})
    report = bytearray(64)
    report[0], report[6], report[8], report[9] = 1, 140, 8, 0x08
    pad.forward(bytes(report))
    vision.step(frame, 20.0)
    assert vision.last_press['R'] < 0
    report[6] = 175
    pad.forward(bytes(report))
    vision.step(frame, 20.1)
    assert vision.last_press['R'] == 20.1


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


def test_lit_icon():
    """An ultimate unlocked by a move (no full energy ring) still counts as ready once its icon is drawn lit."""
    import ww_vision as W
    disc = np.full((160, 160, 4), 30, np.uint8)                           # dark icon disc
    lit = disc.copy(); lit[40:120, 60:100, :3] = 238                     # a white glyph (HDR-dimmed white)
    grey = disc.copy(); grey[40:120, 60:100, :3] = 140                   # unusable: grey glyph
    flash = np.full((160, 160, 4), 250, np.uint8)
    w, d = W.disc_stats(lit, 80, 80, 66); assert W.icon_lit(w, d, 0.3)    # partial ring (move-unlocked): ready
    w, d = W.disc_stats(grey, 80, 80, 66); assert not W.icon_lit(w, d, 1.0)   # full ring, grey glyph: not ready
    w, d = W.disc_stats(flash, 80, 80, 66); assert not W.icon_lit(w, d, 1.0)  # white flash
    w, d = W.disc_stats(lit, 80, 80, 66)
    assert not W.icon_lit(w, d, 0.05)
    assert W.icon_lit(w, d, 0.05, confirmed_badges=True)
    w, d = W.disc_stats(flash, 80, 80, 66)
    assert not W.icon_lit(w, d, 0.05, confirmed_badges=True)
    keep_ring, keep_lit = W.ring_fill, W.disc_stats
    try:
        v = WWVision(lambda: (0.0, 0.0), camera=FakeCam()); v._setup()
        class OCR:
            def cooldown(self, crop): return None
            def read_gray(self, crop): return ''
        v.ocr = OCR(); v._pad_update = lambda f, n: None; v.pad = {'echo': 1740, 'liberation': 1950}
        fr = np.zeros((v.region[3] - v.region[1], v.region[2] - v.region[0], 4), np.uint8)
        W.ring_fill = lambda *a: 0.3                                       # energy ring never full
        level = {'v': 0.0}; W.disc_stats = lambda *a: (level['v'], 0.4)
        t = 0.0
        for _ in range(12):
            t += 0.1; v.pad_time = t; v.step(fr, t)
        assert v.states()['liberation'] is False                          # grey, no countdown: not ready
        level['v'] = 0.3
        for _ in range(12):
            t += 0.1; v.pad_time = t; v.step(fr, t)
        assert v.states()['liberation'] is True                           # lit: ready without the ring
    finally:
        W.ring_fill, W.disc_stats = keep_ring, keep_lit


def test_short_conditional_liberation():
    from unittest.mock import patch
    import ww_vision as W

    class OCR:
        cd = 8.0

        def cooldown(self, crop):
            return self.cd

        def read_gray(self, crop):
            return ''

    ocr = OCR()
    v = WWVision(lambda: (0.0, 0.0), camera=FakeCam(), ocr=ocr)
    v._setup()
    v.pad = {'echo': 1740, 'liberation': 1950}
    v.lib_badges_until = 2.0
    v._pad_update = lambda *_: None
    fr = np.zeros((v.region[3] - v.region[1], v.region[2] - v.region[0], 4), np.uint8)
    with patch.object(W, 'ring_fill', return_value=0.04), patch.object(W, 'disc_stats', return_value=(0.31, 0.42)):
        for t in (1.0, 1.1):
            v.pad_time = t
            v.step(fr, t)
        assert v.states()['liberation'] is False
        ocr.cd = None
        for t in (1.2, 1.3, 1.4):
            v.pad_time = t
            v.step(fr, t)
        assert v.states()['liberation'] is True
        assert ('ww', 'liberation', 'ready') in v.poll()
        ocr.cd = 12.0
        for t in (1.5, 1.6):
            v.pad_time = t
            v.step(fr, t)
        assert v.states()['liberation'] is False


def test_switch_burst():
    """Character switch on the D-pad: the new character's ready skills are seen within ~0.3 s instead of ~1.3 s."""
    import ww_vision as W
    hat = {'v': None}
    keep_ring, keep_disc = W.ring_fill, W.disc_stats

    class OCR:
        cds = {}
        def cooldown(self, crop):
            return OCR.cds.get(crop.shape[0] * 0 + OCR.current, None)
        def read_gray(self, crop):
            return ''

    def run(use_dpad):
        v = WWVision(lambda: (0.0, 0.0), camera=FakeCam(), dpad=(lambda: hat['v']) if use_dpad else None); v._setup()
        v.ocr = OCR(); v._pad_update = lambda f, n: None
        v.pad = {'echo': 1740, 'liberation': 1950}
        fr = np.zeros((v.region[3] - v.region[1], v.region[2] - v.region[0], 4), np.uint8)
        state = {'cd': {'echo': 6.0, 'liberation': 9.0}, 'ring': 0.2}
        W.ring_fill = lambda *a: state['ring']
        W.disc_stats = lambda *a: ((0.3 if state['ring'] > 0.9 else 0.0), 0.4)   # charged icon drawn lit

        def cooldown(crop, _o=v.ocr):                      # which icon is being read: echo first, then liberation
            k = order.pop(0); order.append(k); return state['cd'][k]
        order = ['echo', 'liberation']; v.ocr.cooldown = cooldown
        t = 0.0
        for _ in range(30):                                # old character: both on cooldown
            t += 0.1; v.pad_time = t; v.step(fr, t)
        assert v.states()['echo'] is False and v.states()['liberation'] is False
        hat['v'] = 0; state['cd'] = {'echo': None, 'liberation': None}; state['ring'] = 1.0   # switch: new one is ready
        t_switch = t; got = {}
        for _ in range(80):
            dt = 1 / W.BURST_FPS if (use_dpad and t < v.burst_until) else 0.1
            t += dt; v.pad_time = t; v.step(fr, t)
            if t - t_switch > 0.05:
                hat['v'] = None
            for k in ('echo', 'liberation'):
                if v.states()[k] and k not in got:
                    got[k] = round(t - t_switch, 2)
            if len(got) == 2:
                break
        return got
    def back_and_forth():
        """Slot up: echo on cooldown 3 s, ultimate full. Switch right, then back after 4 s: both ready at once."""
        v = WWVision(lambda: (0.0, 0.0), camera=FakeCam(), dpad=lambda: hat['v']); v._setup()
        v.ocr = OCR(); v._pad_update = lambda f, n: None; v.pad = {'echo': 1740, 'liberation': 1950}
        fr = np.zeros((v.region[3] - v.region[1], v.region[2] - v.region[0], 4), np.uint8)
        screen = {'echo': 3.0, 'liberation': None}; ring = {'v': 1.0}
        W.ring_fill = lambda *a: ring['v']
        W.disc_stats = lambda *a: ((0.3 if ring['v'] > 0.9 else 0.0), 0.4)
        order = ['echo', 'liberation']
        def cooldown(crop):
            k = order.pop(0); order.append(k); return screen[k]
        v.ocr.cooldown = cooldown
        t = 0.0
        def go(h, n, dt=1 / 30):
            nonlocal t
            hat['v'] = h
            for i in range(n):
                t += dt; v.pad_time = t; v.step(fr, t)
                if i == 1: hat['v'] = None
        go(0, 60)                                          # on slot up: echo cooling down, ultimate full
        st = v.states(); assert st['echo'] is False and st['liberation'] is True, st
        screen.update(echo=8.0, liberation=12.0); ring['v'] = 0.1
        go(2, 1); screen.update(echo=8.0, liberation=12.0)
        go(None, 40)                                       # slot right: both cooling down
        assert v.states()['echo'] is False and v.states()['liberation'] is False
        t += 4.0                                           # echo of slot up has run out meanwhile
        screen.update(echo=None, liberation=None); ring['v'] = 1.0
        go(0, 1)
        return v.states()

    try:
        slow = run(False); fast = run(True); back = back_and_forth()
    finally:
        W.ring_fill, W.disc_stats = keep_ring, keep_disc
    assert max(fast.values()) <= 0.35 and max(slow.values()) >= 0.9, (slow, fast)
    assert back['echo'] is None and back['liberation'] is None, back     # both skills need a fresh read
    held = {'m': 0}
    v = WWVision(lambda: (0.0, 0.0), camera=FakeCam(), buttons=lambda: held['m']); v._setup()
    v._holds(0.0); held['m'] = 0x01; v._holds(0.1); v._holds(0.3)
    assert v.burst_until < 0 and v.holds == 0                           # a tap is not a long press
    v._holds(0.5); assert v.holds == 1 and v.burst_until > 1.5        # held 0.4 s: fast scanning
    v._holds(1.0); assert v.holds == 1                                  # once per hold
    held['m'] = 0; v._holds(2.5); assert v.burst_until > 3.5          # let go: again, the move just landed
    print('   ready after switch: before', slow, 'first visit now', fast, 'returning: same frame', back)


def test_short_switch_tap_wakes_capture():
    v = WWVision(lambda: (0.0, 0.0), camera=FakeCam(), ocr=False, dpad=lambda: None)
    v._setup()
    v.notify_hat(2)
    v.notify_hat(8)
    assert v.scan_wake.is_set()
    assert v._switch_pending()
    fr = np.zeros((v.region[3] - v.region[1], v.region[2] - v.region[0], 4), np.uint8)
    v.step(fr, 1.0)
    assert v.slot == 2 and v.switches == 1
    assert not v._switch_pending()
    assert v.settle_until > 1.0


def test_switch_arms_both_until_new_icons_confirm():
    from unittest.mock import patch

    with patch('ww_vision.time.monotonic', return_value=10.0):
        v = WWVision(lambda: (0.0, 0.0), camera=FakeCam(), ocr=False)
        v._setup()
        v.final['echo'].state = v.final['liberation'].state = False
        v.notify_hat(0)
        v.notify_hat(8)
    assert v.states(10.0) == {'resonance': None, 'echo': True, 'liberation': True}
    assert v.poll() == []

    fr = make_frame(v, {'resonance': None, 'echo': False, 'liberation': True})
    with patch.object(v, '_read_skills', wraps=v._read_skills) as read:
        v.step(fr, 10.0)
        v.step(fr, 10.1)
        read.assert_not_called()
    assert v.states(10.1)['echo'] and v.states(10.1)['liberation']

    v.icons['echo'].ready = False
    v.icons['liberation'].ready = None
    v.step(fr, 10.2)
    v.step(fr, 10.24)
    assert v.states(10.24) == {'resonance': None, 'echo': False, 'liberation': True}
    assert v.states(11.5)['liberation'] is None

    v.icons['liberation'].ready = True
    v.step(fr, 10.28)
    v.step(fr, 10.32)
    assert v.states(10.32) == {'resonance': None, 'echo': False, 'liberation': True}
    assert v.states(11.5)['liberation'] is True
    assert v.poll() == []

    with patch('ww_vision.time.monotonic', side_effect=[10.4, 10.41]):
        v.notify_hat(2)
        v.notify_hat(0)
    assert v.states(10.41)['echo'] is True
    v.step(fr, 10.42)
    assert v.slot == 0 and v.switches == 2
    assert v.states(10.42)['echo'] is True


def test_hold_arms_only_liberation_and_rescans_on_release_or_attack():
    from unittest.mock import patch

    for end_buttons, l2, r2, confirmed in ((0, 0, 0, False), (0x09, 0, 0, True),
                                           (0x01, 200, 0, False), (0x01, 0, 200, False)):
        v = WWVision(lambda: (0.0, 0.0), camera=FakeCam(), ocr=False)
        v._setup()
        v.final['echo'].state = v.final['liberation'].state = False
        v.icons['echo'].ready = False

        def send(now, buttons, l2=0, r2=0):
            report = bytearray(64)
            report[0], report[5], report[6], report[8] = 1, l2, r2, buttons << 4 | 8
            with patch('ww_vision.time.monotonic', return_value=now):
                v.notify_input(report)

        send(10.0, 0x01)
        assert v.states(10.1)['liberation'] is False
        assert v.states(10.4) == {'resonance': None, 'echo': False, 'liberation': True}
        send(10.5, end_buttons, l2, r2)
        assert v.scan_wake.is_set()
        assert v.states(10.5)['liberation'] is True

        frame = make_frame(v, {'resonance': None, 'echo': False, 'liberation': confirmed})
        v.step(frame, 10.5)
        assert v.final['liberation'].state is None
        v.icons['liberation'].ready = confirmed
        v.step(frame, 10.65)
        v.step(frame, 10.69)
        assert v.states(10.69)['liberation'] is confirmed
        send(10.7, end_buttons & 0x01)
        assert v.states(11.2)['liberation'] is confirmed
        assert v.states(11.2)['echo'] is False
        assert v.poll() == []


def test_visual_forte_ignores_idle_and_hidden_hud():
    from unittest.mock import patch

    v = WWVision(lambda: (0, 0), camera=FakeCam(), ocr=False, buttons=lambda: 0)
    v._setup()
    shown = make_frame(v, {'resonance': True, 'echo': False, 'liberation': False})
    hidden = np.zeros_like(shown)
    with patch.object(v.forte, 'update', return_value='release'):
        v.step(shown, 20.0)
        assert v.poll() == []
        report = bytearray(64)
        report[8] = 0x18
        with patch('ww_vision.time.monotonic', return_value=20.1):
            v.notify_input(report)
        v.step(hidden, 20.2)
        assert v.poll() == []
        v.step(shown, 20.3)
        assert v.poll() == [('ww', 'forte', 'release')]
        v.step(shown, 21.0)
        assert v.poll() == []


if __name__ == '__main__':
    for f in (test_sdr, test_hdr_like, test_rules_and_state_triggers, test_forte_release_and_speaker, test_click_gate, test_ocr_states, test_lit_icon, test_short_conditional_liberation, test_switch_burst, test_short_switch_tap_wakes_capture, test_switch_arms_both_until_new_icons_confirm):
        f(); print('ok', f.__name__)
    test_hold_arms_only_liberation_and_rescans_on_release_or_attack()
    print('ok test_hold_arms_only_liberation_and_rescans_on_release_or_attack')
    test_visual_forte_ignores_idle_and_hidden_hud()
    print('ok test_visual_forte_ignores_idle_and_hidden_hud')
    test_unforwarded_trigger_does_not_count_as_a_skill_press()
    print('ok test_unforwarded_trigger_does_not_count_as_a_skill_press')
