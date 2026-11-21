"""Driver dependencies: detection and (elevated, silent) installation.

  usbip-win2  -> 原生透传 (virtual DualSense with audio)      service usbip2_ude      BSD-2-Clause
  HidHide     -> 原生透传 / 接管 (hide the real pad from games)   service HidHide        MIT
  ViGEmBus    -> 接管 (virtual DualShock 4)                      service ViGEmBus       BSD-3-Clause

Installers are the unmodified official releases, shipped in the folder 依赖安装包 next to 依赖.exe.
依赖.exe (deps_installer.py, requests admin) installs whatever is missing; PS5Haptics starts it with
a UAC prompt when it finds something missing. Nothing here runs without the user clicking.
"""
import ctypes
import os
import pathlib
import platform
import subprocess
import sys
import winreg

DEPS = [
    # key, display name, service, installer glob, silent args, needed for
    ('usbip', 'usbip-win2（虚拟 PS5 手柄）', 'usbip2_ude', 'USBip-*-x64.exe',
     ['/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'], '原生透传'),
    ('hidhide', 'HidHide（对游戏隐藏真手柄）', 'HidHide', 'HidHide_*_x64.exe',
     ['/exenoui', '/qn', '/norestart'], '原生透传、接管、虚拟 Xbox'),
    ('vigem', 'ViGEmBus（虚拟 PS4 / Xbox 手柄）', 'ViGEmBus', 'ViGEmBus_*.exe',
     ['/exenoui', '/qn', '/norestart'], '接管、虚拟 Xbox'),
]
NOWIN = 0x08000000
# the same official releases, for when 依赖安装包 is not next to the app (someone unpacked only the update package):
# fetched from the projects' own GitHub releases and checked against these SHA-256 before running
OFFICIAL = {
    'usbip': ('https://github.com/vadimgrn/usbip-win2/releases/download/v.0.9.8.1/USBip-0.9.8.1-x64.exe',
              '38cad6d4432b52d5bb9409d9ad03b72fdffc4ada4cd3a48fbeca1a2752a8518a'),
    'hidhide': ('https://github.com/nefarius/HidHide/releases/download/v1.5.230.0/HidHide_1.5.230_x64.exe',
                'f4bbbcb82e6258641b887c74bc81c4c5f66e4aa811808dfc304347687b7605f6'),
    'vigem': ('https://github.com/nefarius/ViGEmBus/releases/download/v1.22.0/ViGEmBus_1.22.0_x64_x86_arm64.exe',
              '89220a7865076b342892f98865f3499fb7c4cfd673159e89d352c360fd014c6a'),
}


def service_exists(name):
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf'SYSTEM\CurrentControlSet\Services\{name}'))
        return True
    except OSError:
        return False


def _uninstall_location(name_part):
    for hive_root in (r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall', r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'):
        try:
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, hive_root)
        except OSError:
            continue
        for i in range(winreg.QueryInfoKey(k)[0]):
            try:
                sk = winreg.OpenKey(k, winreg.EnumKey(k, i))
                if name_part.lower() in str(winreg.QueryValueEx(sk, 'DisplayName')[0]).lower():
                    return pathlib.Path(winreg.QueryValueEx(sk, 'InstallLocation')[0])
            except OSError:
                continue
    return None


def usbip_exe():
    """usbip.exe of usbip-win2 (VIIPER falls back to it when attaching the virtual pad), or None."""
    for d in (_uninstall_location('USBip'), pathlib.Path(r'C:\Program Files\USBip')):
        if d is not None and (d / 'usbip.exe').exists():
            return d / 'usbip.exe'
    return None


def installed(key):
    svc = next(d[2] for d in DEPS if d[0] == key)
    if key == 'usbip':                      # a driver service left behind without the program is not an install
        return service_exists(svc) and usbip_exe() is not None
    return service_exists(svc)


def status():
    """[(key, display, installed, needed_for)]"""
    return [(k, disp, installed(k), need) for k, disp, svc, _g, _a, need in DEPS]


def missing():
    return [s for s in status() if not s[2]]


def hidhide_cli():
    """HidHideCLI.exe from the uninstall record (falls back to the default path)."""
    default = pathlib.Path(r'C:\Program Files\Nefarius Software Solutions\HidHide\x64\HidHideCLI.exe')
    for root in (r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall', r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'):
        try:
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root)
        except OSError:
            continue
        for i in range(winreg.QueryInfoKey(k)[0]):
            try:
                sk = winreg.OpenKey(k, winreg.EnumKey(k, i))
                if 'HidHide' in str(winreg.QueryValueEx(sk, 'DisplayName')[0]):
                    loc = pathlib.Path(winreg.QueryValueEx(sk, 'InstallLocation')[0])
                    if (loc / 'x64' / 'HidHideCLI.exe').exists():
                        return loc / 'x64' / 'HidHideCLI.exe'
            except OSError:
                continue
    return default


def fetch_official(key, log=print):
    """Download the official installer into %TEMP% and check its SHA-256. Returns the path or None."""
    import hashlib, os, tempfile, urllib.request
    url, sha = OFFICIAL[key]
    dst = pathlib.Path(tempfile.gettempdir()) / 'PS5Haptics-deps' / url.rsplit('/', 1)[1]
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not (dst.exists() and hashlib.sha256(dst.read_bytes()).hexdigest() == sha):
        log(f'本地没有安装包，正在从官方 GitHub 下载：{dst.name} …')
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'PS5Haptics-deps'})
            with urllib.request.urlopen(req, timeout=60) as r, open(dst, 'wb') as f:
                while True:
                    b = r.read(1 << 16)
                    if not b:
                        break
                    f.write(b)
        except Exception as exc:
            log(f'下载失败：{exc}。请下载完整版 PS5Haptics 压缩包（带「依赖安装包」文件夹）。'); return None
        if hashlib.sha256(dst.read_bytes()).hexdigest() != sha:
            os.remove(dst); log('下载的安装包校验不通过，已删除，没有运行。'); return None
    return dst


def installer_dir():
    """依赖安装包: next to the app folder (release layout) or next to 依赖.exe."""
    here = pathlib.Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent
    for d in (here / '依赖安装包', here.parent / '依赖安装包'):
        if d.exists():
            return d
    return here.parent / '依赖安装包'


def installer_exe():
    here = pathlib.Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent
    for p in (here / '依赖.exe', here.parent / '依赖.exe'):
        if p.exists():
            return p
    return None


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_elevated(exe, args=''):
    """ShellExecute 'runas' (UAC prompt). Returns True if it was started."""
    r = ctypes.windll.shell32.ShellExecuteW(None, 'runas', str(exe), args, str(pathlib.Path(exe).parent), 1)
    return r > 32


USBIP_MIN_BUILD = 19041   # Windows 10 2004 (20H1) x64+


def usbip_supported():
    """Return (ok, reason) for usbip-win2 OS compatibility."""
    arch = (os.environ.get('PROCESSOR_ARCHITEW6432') or platform.machine() or '').lower()
    if arch not in ('amd64', 'x86_64'):
        return False, f'当前系统架构（{arch or "未知"}）不支持 64 位 usbip-win2 驱动'
    try:
        build = sys.getwindowsversion().build
    except Exception:
        return True, ''
    if build < USBIP_MIN_BUILD:
        return False, (f'当前 Windows 版本（Build {build}）低于 usbip-win2 要求的 Windows 10 2004（Build {USBIP_MIN_BUILD}）或 Windows 11，'
                       '无法安装虚拟 PS5 手柄驱动（原生透传）。请升级 Windows，或在软件中改用「普通」/「接管」/「虚拟 Xbox」模式。')
    return True, ''


def install(key, log=print):
    """Silent install of one dependency (must already be elevated). Falls back to the normal setup UI."""
    _k, disp, svc, pattern, args, _n = next(d for d in DEPS if d[0] == key)
    if key == 'usbip':
        ok_os, reason = usbip_supported()
        if not ok_os:
            log(f'跳过 {disp}：{reason}')
            return False
    found = sorted(installer_dir().glob(pattern))
    exe = found[-1] if found else fetch_official(key, log)
    if exe is None:
        log(f'找不到 {disp} 的安装包（{pattern}），也没能从官方下载。'); return False
    log(f'正在安装 {disp}：{exe.name} …')
    r = subprocess.run([str(exe), *args])
    if r.returncode not in (0, 3010) or not installed(key):
        # USBip setup registers a new root devnode each time it is run.
        if key == 'usbip':
            log(f'USBip 安装需要检查（返回码 {r.returncode}）。请先重启后检查驱动状态。')
        else:
            log(f'静默安装没有成功（返回码 {r.returncode}），改为打开安装界面，请按提示完成。')
            subprocess.run([str(exe)])
    ok = installed(key)
    log(f'{disp}：' + ('已安装' if ok else '仍未安装'))
    return ok
