"""Simple touch-perception model to sanity-check the event-tuning rules.

For every rule, render its grip waveform alone and estimate how a hand would feel it:
  intensity  - frequency-weighted RMS (hand sensitivity curve, most sensitive ~200-250 Hz) with
               temporal summation (longer cues feel stronger, saturating around 300 ms);
  sharpness  - sensitivity-weighted spectral centroid (Hz): high = crisp/metallic, low = heavy/body;
  pulses     - separate bursts felt (envelope dips below 35 % between peaks for >= 15 ms);
  duration   - time above 20 % of the peak envelope;
  side       - (L - R) / (L + R) energy: +1 left hand only, -1 right hand only.
Then: ordering checks inside each family, distinctness of look-alike cues, and (with a log file)
forward-masking / busy-ness statistics over a real play session.
"""
import collections
import math
import sys
import numpy as np
from event_fx import EventFX, DEFAULT_CONFIG
from haptic_dsp import HOP, RATE

SENS = [(20, 0.2), (30, 0.3), (40, 0.4), (60, 0.55), (90, 0.75), (120, 0.85), (150, 0.95),
        (200, 1.0), (250, 1.0), (300, 0.9), (400, 0.7)]


def sens(f):
    xs = [a for a, _ in SENS]; ys = [b for _, b in SENS]
    return float(np.interp(f, xs, ys))


def render_rule(rule, ms=800):
    cfg = dict(DEFAULT_CONFIG); cfg['rules'] = [rule]; cfg['log_dir'] = '.'
    fx = EventFX(cfg)
    fx.fire(rule)
    ls, rs = [], []
    for _ in range(int(ms * RATE / 1000 / HOP)):
        l, r = fx.render(HOP); ls.append(l); rs.append(r)
    return np.concatenate(ls), np.concatenate(rs)


def feel(l, r):
    x = (np.abs(l) + np.abs(r)) / 2
    mono = (l + r) / 2
    frame = int(0.01 * RATE)
    n = len(mono) // frame
    env = np.array([np.sqrt(np.mean(x[i * frame:(i + 1) * frame] ** 2)) * math.sqrt(2) for i in range(n)])
    if env.max() <= 1e-4:
        return None
    peak = env.max()
    active = env > 0.2 * peak
    dur = active.sum() * 10
    # frequency weighting on the whole cue
    spec = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))
    freqs = np.fft.rfftfreq(len(mono), 1 / RATE)
    band = (freqs >= 15) & (freqs <= 500)
    w = np.array([sens(f) for f in freqs[band]])
    p = spec[band] ** 2
    centroid = float(np.sum(freqs[band] * p * w) / max(1e-12, np.sum(p * w)))
    weight = float(np.sqrt(np.sum(p * w * w) / max(1e-12, np.sum(p))))      # average sensitivity of this cue
    temporal = min(1.0, (dur / 300.0)) ** 0.35                                # temporal summation
    intensity = peak * weight * (0.55 + 0.45 * temporal)
    # pulses: peaks separated by dips
    pulses, above, low_run = 0, False, 0
    for e in env:
        if e > 0.5 * peak and not above:
            pulses += 1; above = True; low_run = 0
        elif e < 0.35 * peak:
            low_run += 1
            if low_run >= 2:
                above = False
    el, er = float(np.sum(l ** 2)), float(np.sum(r ** 2))
    side = (el - er) / max(1e-12, el + er)
    return dict(intensity=intensity, sharp=centroid, pulses=pulses, dur=int(dur), side=side, peak=float(peak))


def describe(f):
    s = '清脆' if f['sharp'] > 150 else ('适中' if f['sharp'] > 85 else '沉重')
    side = '偏左手' if f['side'] > 0.2 else ('偏右手' if f['side'] < -0.2 else '双手')
    rhythm = f"{f['pulses']}下" if f['pulses'] > 1 else ('长震' if f['dur'] >= 300 else ('一下' if f['dur'] < 120 else '一震'))
    return f"{s}·{rhythm}·{side}"


def main(log=None):
    rules = DEFAULT_CONFIG['rules']
    feels = {}
    print(f"{'事件':<10}{'感知强度':>8}{'清脆度Hz':>9}{'下数':>5}{'时长ms':>7}{'左右':>7}  体感")
    for rule in rules:
        if not rule.get('grip'):
            continue
        f = feel(*render_rule(rule))
        if f is None:
            continue
        feels.setdefault(rule['name'], f)
        print(f"{rule['name']:<10}{f['intensity']:>8.2f}{f['sharp']:>9.0f}{f['pulses']:>5}{f['dur']:>7}{f['side']:>7.2f}  {describe(f)}")

    print('\n排序检查（同类里应当越来越强）:')
    chains = [['攻击·轻', '攻击·中', '攻击·重', '攻击·收招'],
              ['受击·轻', '受击·中', '受击·重', '破防'],
              ['防御受击', '普通格挡', '精准格挡', '完美格挡'],
              ['煞器·巨斧', '煞器·巨斧二段']]
    ok_all = True
    for chain in chains:
        vals = [feels[n]['intensity'] for n in chain]
        ok = all(a < b for a, b in zip(vals, vals[1:]))
        ok_all &= ok
        print(f"  {'OK ' if ok else '!! '}" + ' < '.join(f'{n}({v:.2f})' for n, v in zip(chain, vals)))

    print('\n区分度检查（容易混的两两比较，差异越大越好，< 0.35 视为容易混）:')
    pairs = [('普通格挡', '精准格挡'), ('精准格挡', '完美格挡'), ('普通格挡', '完美格挡'), ('防御受击', '普通格挡'),
             ('受击·重', '破防'), ('完美格挡', '完美反击'), ('完美闪避', '完美反击'), ('攻击·重', '受击·重'),
             ('攻击·收招', '破军·满蓄释放')]
    for a, b in pairs:
        fa, fb = feels[a], feels[b]
        d = (abs(math.log(fa['intensity'] / fb['intensity'])) / math.log(2) * 0.5 +
             abs(math.log(fa['sharp'] / fb['sharp'])) / math.log(2) * 0.6 +
             abs(fa['pulses'] - fb['pulses']) * 0.4 +
             abs(math.log((fa['dur'] + 20) / (fb['dur'] + 20))) / math.log(2) * 0.3 +
             abs(fa['side'] - fb['side']) * 0.3)
        print(f"  {'OK ' if d >= 0.35 else '!! '}{a} vs {b}: {d:.2f}   ({describe(fa)} / {describe(fb)})")

    if log:
        replay(log, feels)
    return ok_all


def replay(path, feels):
    cfg = dict(DEFAULT_CONFIG); cfg['log_dir'] = '.'
    fx = EventFX(cfg)
    fired, last_t = [], None
    busy = total = 0
    for line in open(path, encoding='utf8'):
        p = line.rstrip('\n').split('\t')
        if len(p) < 3 or p[1] in ('info', 'hot'):
            continue
        t = int(p[0])
        if last_t is not None and t > last_t:
            n = min(int((t - last_t) * RATE / 1000), RATE * 2)
            while n >= HOP:
                fx.native_gain(HOP); l, r = fx.render(HOP); n -= HOP
                total += 1; busy += int(max(np.max(np.abs(l)), np.max(np.abs(r))) > 0.2)
        last_t = t
        k = fx.feed(p[1], p[2], p[3] if len(p) > 3 else '')
        if k:
            fired.append((t, k))
    cnt = collections.Counter(k for _, k in fired)
    minutes = (fired[-1][0] - fired[0][0]) / 60000 if len(fired) > 1 else 1
    print(f'\n真实日志回放：{len(fired)} 次特调，{minutes:.0f} 分钟，平均每分钟 {len(fired) / max(1, minutes):.0f} 次')
    print(f'  握把被特调占用（>0.2）的时间比例：{busy / max(1, total) * 100:.1f}%')
    important = ['完美格挡', '完美反击', '破防', '武技成功', '暗杀', '完美闪避', '精准格挡']
    for name in important:
        times = [t for t, k in fired if k == name]
        if not times:
            continue
        masked = 0
        for t in times:
            prev = [k for tt, k in fired if t - 150 <= tt < t and k in feels and name in feels
                    and feels[k]['intensity'] >= feels[name]['intensity']]
            masked += bool(prev)
        print(f'  {name:<8} {len(times):>4} 次，被前 150 ms 内更强提示掩盖 {masked} 次（{masked / len(times) * 100:.0f}%）')
    print('  各事件次数：' + '，'.join(f'{k} {v}' for k, v in cnt.most_common()))


if __name__ == '__main__':
    sys.exit(0 if main(sys.argv[1] if len(sys.argv) > 1 else None) else 1)
