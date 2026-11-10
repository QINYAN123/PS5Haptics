"""Update check + online update.

check()     -> newest GitHub release if it is newer than this build (else None)
download()  -> the release zip, with progress
prepare()   -> unpacks it and writes a small updater script; run it, then exit the app.
               The script waits for the app to exit, copies the new program over the old one
               (keeps config.json, traces, covers, the hide receipt) and starts the new version.
If anything fails, the UI points to the GitHub releases page for a manual update.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

import about

UA = 'PS5Haptics-updater'
KEEP = ['config.json', 'event_fx.json', 'game_caps.json', 'isolation-restore.json', 'traces', 'covers']   # never overwritten


def vtuple(v):
    nums = re.findall(r'\d+', str(v))
    return tuple(int(n) for n in nums[:4]) or (0,)


def newer(remote, local=about.VERSION):
    return vtuple(remote) > vtuple(local)


def _get(url, timeout=8):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/vnd.github+json'})
    return urllib.request.urlopen(req, timeout=timeout)


def _pick(names, version):
    """The update package if present, else the full zip (prepare() finds the program inside either)."""
    want = about.UPDATE_ASSET.format(version)
    for n in names:
        if n == want:
            return n
    for n in names:
        if 'update' in n.lower() or about.UPDATE_ASSET_HINT in n:
            return n
    return names[0] if names else None


def _check_web(timeout):
    """No API (rate limit / blocked): the github.com page that redirects to the newest release tag."""
    req = urllib.request.Request(about.RELEASES_URL + '/latest', headers={'User-Agent': UA}, method='HEAD')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        final = r.geturl()                                   # .../releases/tag/v2.1.0
    tag = final.rstrip('/').rsplit('/', 1)[-1]
    if '/tag/' not in final or not tag:
        raise RuntimeError('没有找到发布版本')
    ver = tag.lstrip('vV')
    base = f'{about.GITHUB_URL}/releases/download/{tag}/'
    return {'version': ver, 'notes': '', 'page': final, 'url': base + about.UPDATE_ASSET.format(ver),
            'name': about.UPDATE_ASSET.format(ver), 'size': 0}


def notes_url(page, lang='zh'):
    """Release page to read the changelog; other interface languages get it through Google's page translation."""
    import urllib.parse
    page = page or about.RELEASES_URL
    tl = {'en': 'en', 'ja': 'ja', 'ko': 'ko'}.get(lang)
    if not tl:
        return page
    u = urllib.parse.urlparse(page)
    if u.netloc != 'github.com':
        return page
    return f'https://github-com.translate.goog{u.path}?' + urllib.parse.urlencode({'_x_tr_sl': 'auto', '_x_tr_tl': tl, '_x_tr_hl': tl})


def check(api=None, local=about.VERSION, timeout=8):
    """{'version', 'notes', 'page', 'url', 'name', 'size'} of a newer release, or None."""
    try:
        with _get(api or about.RELEASES_API, timeout) as r:
            rel = json.loads(r.read().decode('utf8'))
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429) and api is None:          # anonymous API limit (60 / hour / IP)
            info = _check_web(timeout)
            return info if newer(info['version'], local) else None
        raise
    tag = rel.get('tag_name') or rel.get('name') or ''
    if not newer(tag, local):
        return None
    zips = {a['name']: a for a in rel.get('assets', []) if a.get('name', '').lower().endswith('.zip')}
    name = _pick(list(zips), tag.lstrip('vV'))
    pick = zips.get(name)
    return {'version': tag.lstrip('vV'), 'notes': (rel.get('body') or '').strip(), 'page': rel.get('html_url') or about.RELEASES_URL,
            'url': pick and pick.get('browser_download_url'), 'name': pick and pick.get('name'), 'size': pick and pick.get('size', 0)}


def download(url, dest, progress=None, cancel=None, timeout=20):
    dest = pathlib.Path(dest)
    with _get(url, timeout) as r, open(dest, 'wb') as f:
        total = int(r.headers.get('Content-Length') or 0); done = 0
        while True:
            if cancel is not None and cancel.is_set():
                raise RuntimeError('已取消')
            b = r.read(1 << 16)
            if not b:
                break
            f.write(b); done += len(b)
            if progress:
                progress(done, total)
    if not zipfile.is_zipfile(dest):
        raise RuntimeError('下载的文件不是有效的压缩包')
    return dest


def app_dir():
    """Folder of the running program (the frozen exe's folder)."""
    return pathlib.Path(sys.executable).resolve().parent


def prepare(zip_path, target=None, pid=None):
    """Unpack the update next to the app and write the updater script. Returns the script path."""
    target = pathlib.Path(target or app_dir())
    stage = pathlib.Path(tempfile.mkdtemp(prefix='PS5Haptics-update-'))
    with zipfile.ZipFile(zip_path) as z:
        for m in z.namelist():                         # no paths escaping the stage folder
            p = (stage / m).resolve()
            if not str(p).startswith(str(stage.resolve())):
                raise RuntimeError('更新包里有非法路径')
        z.extractall(stage)
    exe = next(iter(sorted(stage.rglob('PS5Haptics.exe'), key=lambda p: len(p.parts))), None)
    if exe is None:
        raise RuntimeError('更新包里没有 PS5Haptics.exe')
    new = exe.parent
    if not (new / '_internal').exists():
        raise RuntimeError('更新包不完整（缺少 _internal）')
    log = target / 'traces' / 'update-log.txt'
    q = lambda x: "'" + str(x).replace("'", "''") + "'"           # PowerShell single-quoted literal
    xd = ','.join(q(k) for k in KEEP if '.' not in k)
    xf = ','.join(q(k) for k in KEEP if '.' in k)
    script = stage / 'apply-update.ps1'
    body = f"""$ErrorActionPreference = 'Continue'
$log = {q(log)}
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
function L($m) {{ Add-Content -LiteralPath $log -Value ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '  ' + $m) -Encoding UTF8 }}
L '开始更新到新版本'
try {{ Wait-Process -Id {pid or os.getpid()} -Timeout 60 -ErrorAction Stop }} catch {{ }}
Start-Sleep -Seconds 1
$xd = @({xd}); $xf = @({xf})
$rc = @({q(new)}, {q(target)}, '/E', '/R:5', '/W:1', '/NP', '/NJH', '/NJS', '/XD') + $xd + @('/XF') + $xf
& robocopy.exe @rc | Out-Null
$code = $LASTEXITCODE
if ($code -ge 8) {{
    L ('复制失败（robocopy ' + $code + '），请到 {about.RELEASES_URL} 手动下载')
    Start-Process {q(about.RELEASES_URL)}
}} else {{
    L ('更新完成（robocopy ' + $code + '），重新启动')
    Start-Process -FilePath {q(target / 'PS5Haptics.exe')} -WorkingDirectory {q(target)}
}}
Set-Location $env:TEMP
Start-Sleep -Seconds 1
Remove-Item -LiteralPath {q(stage)} -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath {q(zip_path)} -Force -ErrorAction SilentlyContinue
"""
    script.write_text(body, 'utf-8-sig')
    return script


def run_script(script):
    """Start the updater detached and hidden (it outlives this process)."""
    subprocess.Popen(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', str(script)],
                     creationflags=0x00000200 | 0x08000000, close_fds=True, cwd=tempfile.gettempdir())   # NEW_PROCESS_GROUP, NO_WINDOW (a hidden console: PowerShell needs one); cwd outside the stage so it can be deleted
