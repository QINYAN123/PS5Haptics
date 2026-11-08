"""Assemble the public release folder + zip (run after PyInstaller built dist/PS5Haptics and dist_deps/依赖.exe).

  发布/PS5声音触感-v<ver>/
    PS5声音触感/            the app (clean: no traces, no personal config)
    依赖.exe                driver installer (admin)
    依赖安装包/              official installers of usbip-win2, HidHide, ViGEmBus (+ their licenses)
    一键恢复手柄可见性.cmd/.ps1
    影之刃零事件mod/         optional UE4SS mod
    源码/                    GPL-3.0 source
    使用说明.md
"""
import pathlib
import shutil
import sys
import zipfile

SRC = pathlib.Path(__file__).resolve().parent.parent          # PS5声音触感源码
TOP = SRC.parent
sys.path.insert(0, str(SRC))
import about                                                   # noqa: E402
VER = sys.argv[1] if len(sys.argv) > 1 else about.VERSION
OUT = TOP / '发布' / f'PS5声音触感-v{VER}'
INSTALLERS = TOP / '发布' / '依赖安装包-原始'                 # downloaded official installers live here


def copytree(a, b, ignore=None):
    shutil.copytree(a, b, ignore=ignore, dirs_exist_ok=True)


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    app = OUT / 'PS5声音触感'
    copytree(SRC / 'dist' / 'PS5Haptics', app,
             ignore=shutil.ignore_patterns('traces', 'covers', 'config.json', 'event_fx*.json', 'selftest.log', 'last-error.txt',
                                           'isolation-restore.json'))
    copytree(SRC / 'licenses', app / 'licenses')
    mod_src = TOP / '影之刃零事件记录mod' / 'PS5EventLog' / 'Scripts'
    (app / 'mods' / 'PS5EventLog' / 'Scripts').mkdir(parents=True, exist_ok=True)       # for「一键安装事件 mod」
    for f in mod_src.iterdir():
        shutil.copy2(f, app / 'mods' / 'PS5EventLog' / 'Scripts' / f.name)
    copytree(SRC / 'bin', app / 'bin')                                   # libVIIPER + VIIPERHost.exe + ViGEmClient
    shutil.copy2(SRC / 'LICENSE', app / 'LICENSE.txt')
    for p in ('Restore-Visibility.ps1',):
        if (SRC / p).exists():
            shutil.copy2(SRC / p, app / p)
    shutil.copy2(SRC / 'dist_deps' / '依赖.exe', OUT / '依赖.exe')
    dep = OUT / '依赖安装包'; dep.mkdir()
    found = sorted(INSTALLERS.glob('*.exe')) if INSTALLERS.exists() else []
    for f in found:
        shutil.copy2(f, dep / f.name)
    for f in INSTALLERS.glob('*LICENSE*') if INSTALLERS.exists() else []:
        shutil.copy2(f, dep / f.name)
    # restore tool: DualSense only (the Betop patterns of the private build are not shipped)
    ps1 = (TOP / '一键恢复手柄可见性.ps1').read_text('utf-8-sig')
    ps1 = ps1.replace("'VID_20BC&PID_517[34]|VID_054C&PID_(0CE6|0DF2)'", "'VID_054C&PID_(0CE6|0DF2)'")
    ps1 = ps1.replace('# Restores KP70C visibility after any bridge version (0.6/0.7/0.8/0.9/PBZ) or PS5Haptics takeover left HidHide hiding a pad.',
                      '# Restores the DualSense after PS5声音触感 left HidHide hiding it (e.g. after a crash).')
    (OUT / '一键恢复手柄可见性.ps1').write_text(ps1, 'utf-8-sig')
    shutil.copy2(TOP / '一键恢复手柄可见性.cmd', OUT / '一键恢复手柄可见性.cmd')
    mod = TOP / '影之刃零事件记录mod' / 'PS5EventLog'
    if mod.exists():
        copytree(mod, OUT / '影之刃零事件mod' / 'PS5EventLog', ignore=shutil.ignore_patterns('logs', 'research'))
        (OUT / '影之刃零事件mod' / '安装方法.txt').write_text(
            '可选：只有玩《影之刃零》并想要「事件特调」才需要。\n'
            '1. 先给影之刃零安装 UE4SS（游戏目录\\platform\\steam\\ue4ss）。\n'
            '2. 把 PS5EventLog 文件夹复制到 游戏目录\\platform\\steam\\ue4ss\\Mods\\ 下。\n'
            '3. 启动游戏后会在 Mods\\PS5EventLog\\logs 生成事件日志，PS5声音触感会自动找到它。\n'
            '这个 mod 只记录事件，不改变游戏行为；删除 enabled.txt 即停用。\n', 'utf-8')
    shutil.copy2(SRC / 'release' / '使用说明.md', OUT / '使用说明.md')
    # GPL-3.0 source
    s = OUT / '源码'; s.mkdir()
    for f in SRC.iterdir():
        if f.is_file() and f.suffix in ('.py', '.spec', '.ps1') and not f.name.startswith(('diag_',)):
            shutil.copy2(f, s / f.name)
    (s / 'README.md').write_text(
        '# PS5 手柄 · 声音触感 - 源码（MIT；viiper_host/ 为 GPL-3.0）\n\n'
        f'作者：{about.AUTHOR} · 哔哩哔哩 {about.BILIBILI_URL} · GitHub {about.GITHUB_URL}\n\n'
        '使用说明见上一级的「使用说明.md」。\n\n## 构建\n\n'
        '```\npython -m venv .venv\n.venv\\Scripts\\pip install -r requirements.txt\n'
        '.venv\\Scripts\\python -m PyInstaller --noconfirm PS5Haptics.spec\n'
        '.venv\\Scripts\\python -m PyInstaller --noconfirm --onefile --uac-admin --console --name 依赖 deps_installer.py\n'
        '.venv\\Scripts\\python -m PyInstaller --noconfirm --onefile --console --name VIIPERHost viiper_host\\viiper_host.py\n'
        '.venv\\Scripts\\python release\\build_release.py\n```\n\n'
        '虚拟 PS5 手柄由独立程序 VIIPERHost（`viiper_host/`，GPL-3.0）负责，主程序只通过本机套接字和它通信，不加载 libVIIPER。\n'
        '`bin/libVIIPER.dll` 由 VIIPER v0.7.0 加 ApexSenseBridge 补丁构建，对应源码与补丁见 `licenses/VIIPER-SOURCE.txt`、'
        '`licenses/VIIPER-v0.7.0-asb.patch`。\n', 'utf8')
    for d in ('bin', 'licenses', 'models', 'release', 'viiper_host'):
        if (SRC / d).exists():
            copytree(SRC / d, s / d)
    shutil.copy2(SRC / 'LICENSE', s / 'LICENSE')                                   # MIT (viiper_host/ GPL-3.0)
    (s / 'requirements.txt').write_text('numpy\nscipy\npillow\npyaudiowpatch\nhidapi\ncomtypes\ndxcam\nonnxruntime\nonnxocr-ppocrv5\npyinstaller\n', 'utf8')
    z = OUT.parent / (OUT.name + '.zip')
    if z.exists():
        z.unlink()
    with zipfile.ZipFile(z, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in OUT.rglob('*'):
            if f.is_file():
                zf.write(f, f.relative_to(OUT.parent))
    # small update package (program folder only) for the in-app online update
    u = OUT.parent / f'PS5声音触感-{about.UPDATE_ASSET_HINT}-v{VER}.zip'
    if u.exists():
        u.unlink()
    with zipfile.ZipFile(u, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in app.rglob('*'):
            if f.is_file():
                zf.write(f, f.relative_to(OUT))
    print('update package:', u, f'{u.stat().st_size / 1e6:.1f} MB')
    print('release folder:', OUT)
    print('zip:', z, f'{z.stat().st_size / 1e6:.1f} MB')
    print('installers bundled:', [f.name for f in found] or 'NONE - download them into ' + str(INSTALLERS))


if __name__ == '__main__':
    main()
