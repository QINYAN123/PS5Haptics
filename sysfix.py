"""Machine-specific helpers for the release build.

* find_pbz_log_dir(): locate 影之刃零's PS5EventLog log folder in any Steam library
* Intel USB audio offload ("适用于 USB 音频的英特尔® 智音技术"): on some laptops it takes over the
  DualSense USB audio and drops the voice-coil channels 3/4. The guard disables it while the app
  runs and enables it again when the app exits (one UAC prompt, one elevated helper process).
"""
import pathlib
import re
import subprocess
import sys
import time
import winreg

NOWIN = 0x08000000
PBZ_REL = pathlib.Path('steamapps/common/PBZ/platform/steam/ue4ss/Mods/PS5EventLog/logs')


def steam_libraries():
    roots = []
    for hive, key, val in ((winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam', 'SteamPath'),
                           (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Valve\Steam', 'InstallPath')):
        try:
            roots.append(pathlib.Path(winreg.QueryValueEx(winreg.OpenKey(hive, key), val)[0]))
        except OSError:
            pass
    libs = []
    for r in roots:
        libs.append(r)
        vdf = r / 'steamapps' / 'libraryfolders.vdf'
        if vdf.exists():
            for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text('utf8', 'replace')):
                libs.append(pathlib.Path(m.group(1).replace('\\\\', '\\')))
    for d in 'CDEFGHIJ':                                   # common manual library locations
        for sub in ('SteamLibrary', 'Steam', 'Program Files (x86)/Steam', 'Games/Steam'):
            libs.append(pathlib.Path(f'{d}:/') / sub)
    seen, out = set(), []
    for l in libs:
        k = str(l).lower()
        if k not in seen:
            seen.add(k); out.append(l)
    return out


def find_pbz_log_dir():
    """The PS5EventLog log folder (created by the mod), else the mod folder's would-be logs path."""
    mod_only = None
    for lib in steam_libraries():
        p = lib / PBZ_REL
        try:
            if p.exists():
                return p
            if p.parent.exists() and mod_only is None:
                mod_only = p
        except OSError:
            continue
    return mod_only


# ---------------------------------------------------------------- Intel USB audio offload
def _ps(cmd):
    r = subprocess.run(['powershell', '-NoProfile', '-Command', cmd], capture_output=True, text=True, creationflags=NOWIN, timeout=30)
    return r.stdout.strip()


def intel_offload_devices():
    """[(instance_id, name, status)] of Intel 'Smart Sound Technology for USB Audio' devices."""
    out = _ps("Get-PnpDevice -PresentOnly | Where-Object { ($_.FriendlyName -match 'USB') -and "
              "($_.FriendlyName -match '音频|Audio') -and ($_.FriendlyName -match '英特尔|Intel') -and "
              "($_.FriendlyName -match '智音|Smart Sound') } | ForEach-Object { $_.InstanceId + '|' + $_.FriendlyName + '|' + $_.Status }")
    res = []
    for line in out.splitlines():
        parts = line.split('|')
        if len(parts) == 3:
            res.append(tuple(parts))
    return res


def guard_offload(app_pid):
    """Run elevated: disable the offload devices, wait until the app exits, enable them again."""
    devs = [d for d in intel_offload_devices() if d[2].upper() == 'OK']
    for iid, _n, _s in devs:
        subprocess.run(['pnputil', '/disable-device', iid], creationflags=NOWIN)
    import ctypes
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x00100000, False, int(app_pid))           # SYNCHRONIZE
    if h:
        k.WaitForSingleObject(h, 0xFFFFFFFF); k.CloseHandle(h)
    else:
        time.sleep(1)
    for iid, _n, _s in devs:
        subprocess.run(['pnputil', '/enable-device', iid], creationflags=NOWIN)


def start_guard(app_pid):
    """Start the elevated guard (UAC prompt). Returns True if launched."""
    import ctypes
    if getattr(sys, 'frozen', False):
        exe, args = sys.executable, f'--offload-guard {app_pid}'
    else:
        exe, args = sys.executable, f'"{pathlib.Path(__file__).with_name("ps5_haptics_app.py")}" --offload-guard {app_pid}'
    return ctypes.windll.shell32.ShellExecuteW(None, 'runas', exe, args, None, 0) > 32


# ---------------------------------------------------------------- 影之刃零: UE4SS + PS5EventLog
PBZ_NEXUS_UE4SS = 'https://www.nexusmods.com/phantombladezero/mods/5?tab=description'


def find_pbz_game():
    """Game folder (…/steamapps/common/PBZ) or None."""
    for lib in steam_libraries():
        g = lib / 'steamapps' / 'common' / 'PBZ'
        try:
            if (g / 'platform' / 'steam' / 'PBZ.exe').exists():
                return g
        except OSError:
            continue
    return None


def bundled_mod():
    """Our PS5EventLog mod shipped next to / inside the app."""
    root = pathlib.Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else pathlib.Path(__file__).resolve().parent
    for d in (root / 'mods' / 'PS5EventLog', root.parent / '影之刃零事件mod' / 'PS5EventLog',
              root.parent / '影之刃零事件记录mod' / 'PS5EventLog'):
        if (d / 'Scripts' / 'main.lua').exists():
            return d
    return None


def pbz_status(log_dir=None):
    """dict: game, ue4ss (bool), mod (None / 'ok' / 'old' / 'disabled'), log_dir, last_log (epoch or None)."""
    import hashlib
    st = dict(game=None, ue4ss=False, mod=None, log_dir=None, last_log=None)
    g = find_pbz_game()
    if g is not None:
        st['game'] = str(g)
        base = g / 'platform' / 'steam'
        ue = base / 'ue4ss'
        st['ue4ss'] = (ue / 'UE4SS.dll').is_file() and any(
            (base / n).is_file() for n in ('dwmapi.dll', 'xinput1_3.dll', 'dxgi.dll'))
        mod = ue / 'Mods' / 'PS5EventLog'
        script = mod / 'Scripts' / 'main.lua'
        if script.is_file():
            src = bundled_mod()
            same = src is None or hashlib.sha1(script.read_bytes()).digest() == \
                hashlib.sha1((src / 'Scripts' / 'main.lua').read_bytes()).digest()
            st['mod'] = 'disabled' if not (mod / 'enabled.txt').is_file() else ('ok' if same else 'old')
            st['log_dir'] = str(mod / 'logs')
    if log_dir:
        st['log_dir'] = str(log_dir)
    if st['log_dir']:
        logs = pathlib.Path(st['log_dir'])
        try:
            st['last_log'] = max((p.stat().st_mtime for p in logs.glob('events-*.tsv') if p.is_file()), default=None)
        except OSError:
            pass
    return st


def pbz_event_state(status, session_started=None, now=None):
    """Display state. A log from the current game session is stronger evidence than installed files."""
    if status is None:
        return 'checking'
    if status.get('error'):
        return 'error'
    last_log = status['last_log']
    if session_started is not None and last_log is not None and last_log >= session_started:
        return 'logging'
    if not status['game'] and not status['log_dir']:
        return 'game_missing'
    if status['game']:
        if not status['ue4ss']:
            return 'loader_missing'
        if status['mod'] != 'ok':
            return {'disabled': 'mod_disabled', 'old': 'mod_old', None: 'mod_missing'}[status['mod']]
    if session_started is None:
        return 'ready' if status['game'] else 'manual'
    if (now if now is not None else time.time()) - session_started >= 180:
        return 'no_log'
    return 'waiting'


def process_start_time(pid):
    """Creation time in Unix seconds, for comparing event-log mtimes to this game session."""
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k.OpenProcess.restype = wintypes.HANDLE
    filetime_p = ctypes.POINTER(wintypes.FILETIME)
    k.GetProcessTimes.argtypes = (wintypes.HANDLE, filetime_p, filetime_p, filetime_p, filetime_p)
    k.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if k.GetProcessTimes(handle, *(ctypes.byref(t) for t in (created, exited, kernel, user))):
            ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
            return (ticks - 116444736000000000) / 10_000_000
    finally:
        k.CloseHandle(handle)


def install_pbz_mod():
    """Copy our PS5EventLog into ue4ss/Mods and enable it. Returns (ok, message)."""
    import shutil
    g = find_pbz_game(); src = bundled_mod()
    if g is None:
        return False, '没有找到影之刃零的安装目录。'
    if src is None:
        return False, '找不到随软件附带的 PS5EventLog mod 文件。'
    mods = g / 'platform' / 'steam' / 'ue4ss' / 'Mods'
    if not mods.exists():
        return False, '还没有安装 UE4SS：请先按说明安装 UE4SS，再点一键安装。'
    dst = mods / 'PS5EventLog'
    (dst / 'Scripts').mkdir(parents=True, exist_ok=True)
    for f in (src / 'Scripts').iterdir():
        if f.is_file():
            shutil.copy2(f, dst / 'Scripts' / f.name)
    (dst / 'enabled.txt').write_text('', 'utf8')
    (dst / 'logs').mkdir(exist_ok=True)
    return True, f'已安装事件 mod 到 {dst}，下次启动影之刃零生效。'
