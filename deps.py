"""Driver dependencies: detection and (elevated, silent) installation.

  usbip-win2  -> 原生透传 (virtual DualSense with audio)      service usbip2_ude      BSD-2-Clause
  HidHide     -> 原生透传 / 接管 (hide the real pad from games)   service HidHide        MIT
  ViGEmBus    -> 接管 (virtual DualShock 4)                      service ViGEmBus       BSD-3-Clause

Installers are the unmodified official releases, shipped in the folder 依赖安装包 next to 依赖.exe.
依赖.exe (deps_installer.py, requests admin) installs whatever is missing; PS5Haptics starts it with
a UAC prompt when it finds something missing. Nothing here runs without the user clicking.
"""
import ctypes
import pathlib
import subprocess
import sys
import winreg

DEPS = [
    # key, display name, service, installer glob, silent args, needed for
    ('usbip', 'usbip-win2（虚拟 PS5 手柄）', 'usbip2_ude', 'USBip-*-x64.exe',
     ['/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'], '原生透传'),
    ('hidhide', 'HidHide（对游戏隐藏真手柄）', 'HidHide', 'HidHide_*_x64.exe',
     ['/exenoui', '/qn', '/norestart'], '原生透传、接管'),
    ('vigem', 'ViGEmBus（虚拟 PS4 手柄）', 'ViGEmBus', 'ViGEmBus_*.exe',
     ['/exenoui', '/qn', '/norestart'], '接管'),
]
NOWIN = 0x08000000


def service_exists(name):
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf'SYSTEM\CurrentControlSet\Services\{name}'))
        return True
    except OSError:
        return False


def status():
    """[(key, display, installed, needed_for)]"""
    return [(k, disp, service_exists(svc), need) for k, disp, svc, _g, _a, need in DEPS]


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


def install(key, log=print):
    """Silent install of one dependency (must already be elevated). Falls back to the normal setup UI."""
    _k, disp, svc, pattern, args, _n = next(d for d in DEPS if d[0] == key)
    found = sorted(installer_dir().glob(pattern))
    if not found:
        log(f'找不到 {disp} 的安装包（{pattern}），请检查「依赖安装包」文件夹。'); return False
    exe = found[-1]
    log(f'正在安装 {disp}：{exe.name} …')
    r = subprocess.run([str(exe), *args])
    if r.returncode not in (0, 3010) or not service_exists(svc):
        log(f'静默安装没有成功（返回码 {r.returncode}），改为打开安装界面，请按提示完成。')
        subprocess.run([str(exe)])
    ok = service_exists(svc)
    log(f'{disp}：' + ('已安装' if ok else '仍未安装'))
    return ok
