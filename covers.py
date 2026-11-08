"""Card art for the adaptation library.

Game covers are NOT shipped with the program (they belong to the publishers). On the user's PC:
  1. Steam's own local library cache (appcache/librarycache/<appid>/**/library_capsule*.jpg) - the
     vertical cover Steam already downloaded for an installed game;
  2. otherwise the public Steam store API (appdetails) -> the store page background, cropped;
  3. otherwise / offline: generated sci-fi art.
Results are cached in <app>/covers/. Mode cards (游戏 / 影院 / 律动 / 通用) always use generated art.
"""
import json
import math
import pathlib
import sys
import urllib.request
import winreg

from PIL import Image, ImageDraw, ImageFilter, ImageEnhance

ROOT = pathlib.Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else pathlib.Path(__file__).resolve().parent
CACHE = ROOT / 'covers'
UA = {'User-Agent': 'PS5Haptics-covers/1.0'}


def steam_root():
    for hive, key, val in ((winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam', 'SteamPath'),
                           (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Valve\Steam', 'InstallPath')):
        try:
            return pathlib.Path(winreg.QueryValueEx(winreg.OpenKey(hive, key), val)[0])
        except OSError:
            pass
    return None


def _local_capsule(appid):
    r = steam_root()
    if r is None:
        return None
    d = r / 'appcache' / 'librarycache' / str(appid)
    if not d.exists():
        return None
    for pat in ('**/library_600x900*.jpg', '**/library_capsule*.jpg'):
        found = sorted(d.glob(pat), key=lambda p: p.stat().st_size, reverse=True)
        if found:
            return found[0]
    return None


def _store_background(appid):
    url = f'https://store.steampowered.com/api/appdetails?appids={appid}&l=schinese'
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as r:
        data = json.loads(r.read().decode('utf8')).get(str(appid), {}).get('data', {})
    for key in ('background_raw', 'header_image'):
        u = data.get(key)
        if u:
            with urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=20) as r:
                tmp = CACHE / f'{appid}_src.jpg'; tmp.write_bytes(r.read())
            return tmp
    return None


def fit(img, w, h):
    """Crop to the w:h aspect (centre) and resize."""
    iw, ih = img.size
    if iw / ih > w / h:
        nw = int(ih * w / h); img = img.crop(((iw - nw) // 2, 0, (iw + nw) // 2, ih))
    else:
        nh = int(iw * h / w); img = img.crop((0, 0, iw, nh))
    return img.resize((w, h), Image.LANCZOS)


def game_cover(appid):
    """Path of a cached cover for this Steam app, fetching it if needed (call from a thread)."""
    CACHE.mkdir(exist_ok=True)
    out = CACHE / f'{appid}.jpg'
    if out.exists():
        return out
    src = None
    try:
        src = _local_capsule(appid)
    except Exception:
        src = None
    if src is None:
        try:
            src = _store_background(appid)
        except Exception:
            return None
    if src is None:
        return None
    Image.open(src).convert('RGB').save(out, quality=92)
    return out


# ---------------------------------------------------------------- generated art
def _grad(w, h, top, bottom):
    im = Image.new('RGB', (w, h)); d = ImageDraw.Draw(im)
    for y in range(h):
        t = y / max(1, h - 1)
        d.line([(0, y), (w, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
    return im


def generated(kind, w, h):
    if kind == 'impact':                                   # 游戏: burst of impact lines
        im = _grad(w, h, (6, 22, 44), (2, 6, 14)); d = ImageDraw.Draw(im); cx, cy = w // 2, int(h * 0.42)
        for i in range(36):
            a = i * math.tau / 36; r0 = w * (0.10 + 0.05 * (i % 3)); r1 = w * (0.38 + 0.12 * ((i * 7) % 5) / 4)
            d.line([(cx + r0 * math.cos(a), cy + r0 * math.sin(a)), (cx + r1 * math.cos(a), cy + r1 * math.sin(a))],
                   fill=(0, 200 - 6 * (i % 5), 255), width=2)
        for k in range(3):
            r = w * (0.10 + 0.07 * k); d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(0, 229, 255), width=2)
    elif kind == 'cinema':                                 # 影院: film strip + low waves
        im = _grad(w, h, (40, 22, 4), (8, 4, 2)); d = ImageDraw.Draw(im)
        for y0 in (int(h * 0.12), int(h * 0.62)):
            d.rectangle([0, y0, w, y0 + int(h * 0.16)], fill=(14, 9, 4))
            for x in range(6, w, 22):
                d.rectangle([x, y0 + 4, x + 10, y0 + 12], fill=(255, 176, 32))
                d.rectangle([x, y0 + int(h * 0.16) - 12, x + 10, y0 + int(h * 0.16) - 4], fill=(255, 176, 32))
        for k in range(4):
            pts = [(x, int(h * 0.47 + (14 - 3 * k) * math.sin(x / (18 + 8 * k) + k))) for x in range(0, w, 3)]
            d.line(pts, fill=(255, 176 - 25 * k, 32), width=2)
    elif kind == 'rhythm':                                 # 律动: equalizer bars
        im = _grad(w, h, (36, 4, 40), (8, 2, 12)); d = ImageDraw.Draw(im); n = 14; bw = w / n
        for i in range(n):
            v = 0.25 + 0.7 * abs(math.sin(i * 1.7) * math.cos(i * 0.6))
            top = int(h * (0.80 - 0.55 * v))
            for y in range(top, int(h * 0.80), 8):
                t = (h * 0.80 - y) / (h * 0.55)
                d.rectangle([i * bw + 3, y, (i + 1) * bw - 3, y + 5], fill=(255, int(46 + 120 * t), int(122 + 100 * t)))
    else:                                                  # 通用: grid + gamepad outline
        im = _grad(w, h, (6, 20, 30), (2, 6, 10)); d = ImageDraw.Draw(im)
        for x in range(0, w, 20):
            d.line([(x, 0), (x, h)], fill=(14, 40, 56))
        for y in range(0, h, 20):
            d.line([(0, y), (w, y)], fill=(14, 40, 56))
        cx, cy, s = w // 2, int(h * 0.42), w * 0.30
        d.rounded_rectangle([cx - s, cy - s * 0.45, cx + s, cy + s * 0.45], radius=int(s * 0.4), outline=(0, 229, 255), width=3)
        d.ellipse([cx - s * 0.62, cy - s * 0.12, cx - s * 0.38, cy + s * 0.12], outline=(0, 229, 255), width=2)
        d.ellipse([cx + s * 0.38, cy - s * 0.12, cx + s * 0.62, cy + s * 0.12], outline=(0, 229, 255), width=2)
    return im.filter(ImageFilter.SMOOTH)


def compose(img, w, h, on=True):
    """Cover -> card face: dark fade at the bottom for the title; dimmed and desaturated when off."""
    img = fit(img.convert('RGB'), w, h)
    if not on:
        img = ImageEnhance.Brightness(ImageEnhance.Color(img).enhance(0.25)).enhance(0.45)
    fade = Image.new('L', (w, h), 0); d = ImageDraw.Draw(fade)
    for y in range(int(h * 0.55), h):
        d.line([(0, y), (w, y)], fill=int(235 * (y - h * 0.55) / (h * 0.45)))
    return Image.composite(Image.new('RGB', (w, h), (3, 6, 12)), img, fade)
