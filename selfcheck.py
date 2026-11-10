"""Self-check ("自检诊断") and the report people attach to a GitHub issue.

collect() looks at drivers, HidHide, the controller's HID entries, its audio endpoint, Intel USB audio
offload, the running state, the last 60 s of haptic sources and the log tail, and returns
(problems, report). The report is plain text; the user name, home folder and PC name are removed.
Nothing is uploaded: the app opens GitHub's new-issue page and the person pastes the report.
"""
import getpass
import os
import pathlib
import platform
import re
import sys
import time
import urllib.parse

import about

ROOT = pathlib.Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else pathlib.Path(__file__).resolve().parent
REPORT_DIR = ROOT / 'diagnostics'
TL_KEYS = ('snd', 'nat', 'mot', 'fx', 'out_l', 'out_r')        # timeline columns, see Runner.timeline
CFG_KEYS = ('scene', 'mode', 'fusion', 'trigger_mode', 'grip_profile', 'trigger_profile', 'grip_gain', 'trigger_gain', 'rumble_gain',
            'speaker_mode', 'speaker_eq', 'speaker_output', 'capture', 'voice_gate', 'skip_center', 'pbz_fx', 'ww_fx', 'ww_vision',
            'grip_left_gain', 'grip_right_gain', 'trigger_left_gain', 'trigger_right_gain', 'swap_grips', 'volume_comp', 'foreground_exe',
            'trig_preset_L', 'trig_start_L', 'trig_strength_L', 'trig_end_L', 'trig_freq_L',
            'trig_preset_R', 'trig_start_R', 'trig_strength_R', 'trig_end_R', 'trig_freq_R')


def sanitize(text):
    """Remove the home folder, user name and PC name."""
    home = os.path.expanduser('~')
    for h in {home, home.replace('\\', '/')}:
        if len(h) > 3:
            text = re.sub(re.escape(h), '%USERPROFILE%', text, flags=re.I)
    for word, mark in ((getpass.getuser(), '<user>'), (platform.node(), '<pc>')):
        if word and len(word) > 2:
            text = re.sub(r'(?<![A-Za-z0-9])' + re.escape(word) + r'(?![A-Za-z0-9])', mark, text, flags=re.I)
    return text


def _try(fn, default=None):
    try:
        return fn()
    except Exception as exc:                     # one failing check must not stop the others
        return default if default is not None else f'(检查失败：{exc})'


def _drivers(out, problems):
    import deps
    st = deps.status()
    out.append('驱动：' + '　'.join(('✓ ' if ok else '✗ ') + disp for _k, disp, ok, _n in st))
    miss = [disp for _k, disp, ok, _n in st if not ok]
    if miss:
        problems.append('缺少驱动：' + '、'.join(miss) + '。到「系统」页点「一键安装依赖」。')


def _hidhide(out, problems, running):
    import subprocess
    import deps
    cli = deps.hidhide_cli()
    if not cli.exists():
        out.append('HidHide：未安装'); return
    r = subprocess.run([str(cli), '--inv-state', '--cloak-state', '--dev-list'], capture_output=True, timeout=8,
                       stdin=subprocess.DEVNULL, creationflags=0x08000000)
    text = r.stdout.decode('utf-8', 'replace')
    hidden = re.findall(r'--dev-hide "([^"]+)"', text)
    pads = [d for d in hidden if re.search(r'VID_054C&PID_(0CE6|0DF2)', d, re.I)]
    out.append(f"HidHide：{'反向模式' if '--inv-on' in text else '正常模式'}，{'隐藏已开' if '--cloak-on' in text else '隐藏已关'}，"
               f'隐藏列表 {len(hidden)} 个，其中 PS5 手柄 {len(pads)} 个')
    for d in pads:
        out.append('  隐藏中：' + d)
    if pads and '--cloak-on' in text and not running:
        problems.append('软件没在运行，但真手柄还被 HidHide 隐藏着：游戏和其他程序可能认不到手柄。到「系统」页点「修复手柄」。')


def _pads(out, problems):
    import hid
    from ds_hid import VID, PIDS, find_path, is_bluetooth
    rows = [d for d in hid.enumerate(VID, 0) if d['product_id'] in PIDS]
    path, _pid = find_path()
    out.append(f'手柄 HID：{len(rows)} 个条目')
    for d in rows:
        p = d.get('path') or b''
        p = p.decode('ascii', 'replace') if isinstance(p, bytes) else str(p)
        mark = '→ ' if (d.get('path') == path) else '  '
        out.append(f"  {mark}pid {d['product_id']:04x}  接口 {d.get('interface_number')}  usage {d.get('usage_page', 0):04x}/{d.get('usage', 0):02x}"
                   f"  {'蓝牙 ' if is_bluetooth(p) else ''}{p}")
    if path is None:
        problems.append('没检测到 PS5 手柄：请用 USB 线或 DS5Dongle 接收器连接；接收器有多种模式时选 DualSense 或 DualSense Edge 模式。')
    elif is_bluetooth(path):
        problems.append('手柄只通过电脑蓝牙连接：本软件需要 USB 线或 DS5Dongle 接收器。')
    return path is not None and not is_bluetooth(path)


def _audio(out, problems, pad_ok):
    import pyaudiowpatch as pa
    from ds_audio import pa_new, pa_end
    p = pa_new()
    try:
        w = p.get_host_api_info_by_type(pa.paWASAPI)['index']
        outs = [p.get_device_info_by_index(i) for i in range(p.get_device_count())]
        outs = [d for d in outs if d['hostApi'] == w and d['maxOutputChannels'] > 0 and not d.get('isLoopbackDevice')]
        default = p.get_default_wasapi_device()['name'] if hasattr(p, 'get_default_wasapi_device') else ''
    finally:
        pa_end(p)
    out.append('音频输出设备（WASAPI）：')
    for d in outs:
        out.append(f"  {'默认 ' if d['name'] == default else ''}{d['name']}  {d['maxOutputChannels']} 声道  {int(d['defaultSampleRate'])} Hz")
    pads = [d for d in outs if 'dualsense' in d['name'].lower() or 'wireless controller' in d['name'].lower()]
    if pad_ok and not any(d['maxOutputChannels'] >= 4 for d in pads):
        names = '、'.join(f"{d['name']}（{d['maxOutputChannels']} 声道）" for d in pads) or '没有'
        problems.append(f'手柄声卡：{names}。握把音圈要 4 声道的手柄声卡（第 3、4 声道是左右握把），只有 1～2 声道时只能出扬声器声音，'
                        '接收器可能不支持握把触感，或者要在它的设置里切到 DualSense 模式。')


def _intel(out, problems):
    import sysfix
    devs = sysfix.intel_offload_devices()
    on = [d for d in devs if d[2].upper() == 'OK']
    out.append(f"英特尔 USB 音频：{'开启' if on else '关闭或没有'}")
    if on:
        problems.append('「适用于 USB 音频的英特尔® 智音技术」开着，可能吞掉握把触感：到「系统」页点「关闭英特尔USB音频」。')


def _timeline(out, timeline):
    rows = list(timeline or [])
    out.append(f'最近触感来源（每 0.1 秒一行，只列有震动的，共 {len(rows)} 行采样）：')
    out.append('  时间      声音  原生波形  游戏马达  事件特调  输出L  输出R  最近事件  L2  R2')
    t0 = rows[-1][0] if rows else 0
    for r in rows:
        if max(r[1:1 + len(TL_KEYS)]) < 0.02:
            continue
        t, *vals = r[:1 + len(TL_KEYS)]; ev, le, re_ = r[1 + len(TL_KEYS):4 + len(TL_KEYS)]
        out.append(f"  {t - t0:6.1f}s  " + '  '.join(f'{v:.2f}' for v in vals) + f'  {ev or "-"}  {le}  {re_}')


def collect(cfg=None, stats=None, timeline=None, log_path=None, running=False):
    cfg = cfg or {}; problems = []
    out = [f'PS5 手柄 · 声音触感 v{about.VERSION} 诊断报告  {time.strftime("%Y-%m-%d %H:%M:%S")}',
           f'系统：{platform.platform()}  {"打包版" if getattr(sys, "frozen", False) else "源码运行"}  运行中：{"是" if running else "否"}', '']
    _try(lambda: _drivers(out, problems), '')
    _try(lambda: _hidhide(out, problems, running), '')
    pad_ok = _try(lambda: _pads(out, problems), False)
    _try(lambda: _audio(out, problems, pad_ok), '')
    _try(lambda: _intel(out, problems), '')
    out.append('')
    out.append('设置：' + '，'.join(f'{k}={cfg[k]}' for k in CFG_KEYS if k in cfg))
    if stats:
        keep = {k: v for k, v in stats.items() if isinstance(v, (int, float, str, bool, tuple)) and k not in ('level',)}
        out.append('运行状态：' + '，'.join(f'{k}={v}' for k, v in keep.items()))
        if stats.get('xruns', 0) > 50 or stats.get('late', 0) > 50:
            problems.append(f"音频断流较多（xruns {stats.get('xruns', 0)}，late {stats.get('late', 0)}）：握把可能断断续续，关掉其他占用手柄声卡的软件试试。")
    out.append('')
    _timeline(out, timeline)
    out.append('')
    if log_path and pathlib.Path(log_path).exists():
        lines = pathlib.Path(log_path).read_text('utf8', 'replace').splitlines()[-150:]
        errs = [ln for ln in lines if '出错' in ln or '失败' in ln or '已停止。原因' in ln]
        if errs:
            problems.append('最近的报错：' + errs[-1].split('  ', 1)[-1][:160])
        out.append('最近日志：'); out += ['  ' + ln for ln in lines]
    traces = sorted((ROOT / 'traces').glob('native-*.tsv'), key=lambda p: p.stat().st_mtime) if (ROOT / 'traces').exists() else []
    if traces:
        out.append(f'最近的调校录制：traces/{traces[-1].name}（{traces[-1].stat().st_size // 1024} KB），反馈时可以一起附上')
    head = ['发现的问题：'] + ([f'{i}. {p}' for i, p in enumerate(problems, 1)] or ['没有发现明显问题。']) + ['']
    return problems, sanitize('\n'.join(head + out) + '\n')


def save(report):
    REPORT_DIR.mkdir(exist_ok=True)
    path = REPORT_DIR / time.strftime('诊断报告-%Y%m%d-%H%M%S.txt')
    path.write_text(report, 'utf8')
    reports = sorted(REPORT_DIR.glob('诊断报告-*.txt'))
    for old in reports[:-20]:                    # keep the last 20
        old.unlink(missing_ok=True)
    return path


def issue_url(problems):
    """GitHub new-issue page with a short template; the report itself is pasted by the person (it is too long for a URL)."""
    title = f'[反馈] v{about.VERSION} ' + (re.sub(r'[：:].*', '', problems[0])[:40] if problems else '问题反馈')
    body = ('**问题描述**（什么时候出现、在哪个游戏、怎么操作）：\n\n\n'
            '**诊断报告**：软件已经把报告复制到剪贴板，请在下面按 Ctrl+V 粘贴。\n\n')
    return f'{about.GITHUB_URL}/issues/new?' + urllib.parse.urlencode({'title': title, 'body': body})
