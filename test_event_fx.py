"""Event-tuning tests. Optional argv[1]: a real PS5EventLog events-*.tsv to replay."""
import collections
import pathlib
import sys
import tempfile
import time
import numpy as np
from event_fx import EventFX, DEFAULT_CONFIG, EventTail, rule_matches
from haptic_dsp import HOP, RATE


def fx_with(tmp):
    cfg = dict(DEFAULT_CONFIG); cfg['log_dir'] = str(tmp)
    return EventFX(cfg)


def run(fx, ms):
    out_l, out_r, gains, trig = [], [], [], []
    for _ in range(int(ms * RATE / 1000 / HOP)):
        g = fx.native_gain(HOP); l, r = fx.render(HOP)
        out_l.append(l); out_r.append(r); gains.append(g[-1]); trig.append(fx.triggers())
    return np.concatenate(out_l), np.concatenate(out_r), gains, trig


def test_perfect_vs_normal():
    with tempfile.TemporaryDirectory() as d:
        fx = fx_with(d)
        assert fx.feed('deflect', 'DeflectComponent:GetSelfResultData', 'Deflect.Result.Perfect') == '完美格挡'
        l, r, g, t = run(fx, 300)
        peaks = [float(np.max(np.abs(l[i:i + 480]))) for i in range(0, 48 * 140, 480)]     # 10 ms frames
        assert max(peaks[0:2]) > 0.8 and max(peaks[5:7]) > 0.6 and min(peaks[2:5]) < 0.5, peaks                     # two clicks
        assert min(g[:40]) < 0.4 and g[-1] == 1.0                                            # native ducked, then restored
        assert all(x == (None, None) for x in t)                                              # parry is L1: triggers untouched
        assert float(np.max(np.abs(l))) > float(np.max(np.abs(r)))                            # left-hand emphasis
        fx2 = fx_with(d)
        assert fx2.feed('deflect', 'x', 'Deflect.Result.Normal') == '普通格挡'
        l2, r2, g2, t2 = run(fx2, 300)
        assert np.max(np.abs(l2)) < 0.6 and all(x == (None, None) for x in t2)              # dull, no trigger


def test_dodge_not_parry():
    with tempfile.TemporaryDirectory() as d:
        fx = fx_with(d)
        assert fx.feed('deflect', 'x', 'Deflect.Result.Perfect|dodge') == '完美闪避'
        l, r, _, _ = run(fx, 200)
        e = lambda a, s, n: float(np.max(np.abs(a[s:s + n])))
        assert e(l, 0, 2400) > e(r, 0, 2400) and e(r, 4800, 2400) > e(l, 4800, 2400)         # sweeps left -> right


def test_hit_levels_and_cooldown():
    with tempfile.TemporaryDirectory() as d:
        fx = fx_with(d)
        assert fx.feed('shake', 'ClientStartCameraShake', 'CameraShake_PlayerHit_Level3_C|scale=1.00') == '受击·重'
        assert fx.feed('shake', 'ClientStartCameraShake', 'CameraShake_PlayerHit_Level3_C|scale=1.00') is None   # cooldown
        l, r, _, t = run(fx, 100)
        assert np.max(np.abs(l)) > 0.5 and all(x == (None, None) for x in t)                   # grips only, L2 stays free
        fx = fx_with(d)
        assert fx.feed('ffb', 'x', 'ForceFeedbackLevel1_PS5') == '力反馈1档'
        _, _, g, _ = run(fx, 100)
        assert abs(g[-1] - 0.4) < 1e-6
        assert fx.feed('fired', 'BP_RenewalNPC_C:On Dead Cancel', '') is None                # excluded noise
        fx = fx_with(d)
        assert fx.feed('shake', 'ClientStartCameraShake', 'CameraShake_PlayerHit_Level4_C|scale=1.00') == '破防'
        l, r, g, t = run(fx, 120)
        assert np.max(np.abs(l)) > 0.8 and min(g) < 0.6 and all(x == (None, None) for x in t)
        fx = fx_with(d)
        assert fx.feed('ffb', 'x', 'ForceFeedback_CombatArt_Success') == '武技成功'
        _, _, _, t = run(fx, 50)
        assert t[0][0] is not None and t[0][0][0] == 0x26 and t[0][1] is None                 # weapon art = L2
        fx = fx_with(d)
        assert fx.feed('ffb', 'x', 'ForceFeedback_BrutalAxe_Phase1') == '煞器·巨斧'
        _, _, _, t = run(fx, 50)
        assert t[0][1] is not None and t[0][1][0] == 0x26 and t[0][0] is None                 # Shachi weapon = R2


def test_tail_skips_history_and_follows():
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / 'events-20260101-000000.tsv'
        p.write_text('1\tdeflect\tx\tDeflect.Result.Perfect\n', 'utf8')
        tail = EventTail(d)
        assert tail.poll() == []                                  # started mid-session: old lines skipped
        with open(p, 'a', encoding='utf8') as f:
            f.write('2\tshake\tClientStartCameraShake\tCameraShake_PlayerHit_Level1_C\n'); f.flush()
        got = tail.poll()
        assert got == [('shake', 'ClientStartCameraShake', 'CameraShake_PlayerHit_Level1_C')], got
        time.sleep(1.1)
        p2 = pathlib.Path(d) / 'events-20260101-000100.tsv'      # a new game session starts a new file
        p2.write_text('3\tdeflect\tx\tDeflect.Result.Normal\n', 'utf8')
        got = tail.poll()
        tail.close()
        assert got == [('deflect', 'x', 'Deflect.Result.Normal')], got


def test_render_cost():
    with tempfile.TemporaryDirectory() as d:
        fx = fx_with(d)
        for _ in range(6):
            fx.fire(DEFAULT_CONFIG['rules'][1])
        t0 = time.perf_counter()
        for _ in range(200):
            fx.native_gain(HOP); fx.render(HOP)
        assert (time.perf_counter() - t0) / 200 < 0.0008


def test_priority_and_masking():
    with tempfile.TemporaryDirectory() as d:
        fx = fx_with(d)
        # the game reports these within 1 ms of each other on a guard break
        assert fx.feed('ffb', 'x', 'ForceFeedbackLevel4_PS5') == '力反馈4档'
        assert fx.feed('shake', 'x', 'CameraShake_PlayerHit_Level4_C|scale=1.00') == '破防'
        assert fx.feed('shake', 'x', 'CameraShake_PlayerHit_Level1_C|scale=1.00') is None       # weaker hit masked
        assert fx.feed('deflect', 'x', 'Deflect.Result.Normal') is None                       # defense masked too
        assert fx.feed('shake', 'x', 'CameraShake_PlayerAttack_Level3_C|scale=1.00') is None   # attack masked too
        _, _, g, _ = run(fx, 100)
        assert abs(g[-1] - 0.45 * 1.15) < 1e-3                                                # channels multiply
        # a weak cue first, then a stronger one in the same group right after: upgrade
        fx = fx_with(d)
        assert fx.feed('deflect', 'x', 'Deflect.Result.Normal') == '普通格挡'
        assert fx.feed('deflect', 'x', 'Deflect.Result.Perfect') == '完美格挡'
        assert all(v[1].get('freq') != 65 for v in fx.voices)                                 # dull normal cue dropped


def replay(path):
    with tempfile.TemporaryDirectory() as d:
        fx = fx_with(d)
        hits = collections.Counter(); last_t = None
        for line in open(path, encoding='utf8'):
            p = line.rstrip('\n').split('\t')
            if len(p) < 3 or p[1] in ('info', 'hot'):
                continue
            t = int(p[0])
            if last_t is not None and t > last_t:                  # advance the clock in real time
                n = min(int((t - last_t) * RATE / 1000), RATE * 5)
                while n >= HOP:
                    fx.native_gain(HOP); fx.render(HOP); n -= HOP
            last_t = t
            k = fx.feed(p[1], p[2], p[3] if len(p) > 3 else '')
            if k: hits[k] += 1
        print('replay of', pathlib.Path(path).name)
        for k, v in hits.most_common():
            print(f'  {k:<8} {v}')


if __name__ == '__main__':
    for f in (test_perfect_vs_normal, test_dodge_not_parry, test_hit_levels_and_cooldown,
              test_tail_skips_history_and_follows, test_render_cost, test_priority_and_masking):
        f(); print('ok', f.__name__)
    if len(sys.argv) > 1:
        replay(sys.argv[1])
