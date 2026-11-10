"""Sci-fi HUD front end for PS5Haptics - pure tkinter (Canvas-drawn widgets), no extra dependencies.

run_ui(cfg, runner_cls, save_cfg, test_fn) builds the window. The Runner contract is unchanged:
Runner(cfg, log).run() in a thread, .stop_ev to stop, .stats dict for the live dashboard.
"""
from __future__ import annotations
import ctypes as C
import ctypes
import math
import os
import pathlib
import about
import i18n
from i18n import tr
import queue
import random
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

from takeover import FUSION_ALIAS
from haptic_dsp import GRIP_PROFILES, TRIGGER_PROFILES, SPEAKER_MODES, SPEAKER_EQS, SPEAKER_MODE_ALIAS, HAPTIC_STYLES

VERSION = about.VERSION
BG = '#03060c'; PANEL = '#07101c'; PANEL2 = '#0b1726'; LINE = '#13283f'; LINE2 = '#1d4263'
CYAN = '#00e5ff'; BLUE = '#2f7bff'; MAG = '#ff2e7a'; AMBER = '#ffb020'; GREEN = '#2dffa0'
TEXT = '#d8ecff'; DIM = '#6f8bab'; FAINT = '#2f4a66'
ZH = 'Microsoft YaHei UI'; NUM = 'Bahnschrift'; NUMB = 'Bahnschrift SemiBold'; MONO = 'Consolas'
S = 1.0                                   # DPI scale, set in run_ui


def px(v):
    return int(round(v * S))


def mix(a, b, t):
    a = [int(a[i:i + 2], 16) for i in (1, 3, 5)]; b = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return '#%02x%02x%02x' % tuple(int(x + (y - x) * max(0.0, min(1.0, t))) for x, y in zip(a, b))


def chamfer(x0, y0, x1, y1, c):
    """Rectangle with the top-left and bottom-right corners cut - the HUD look."""
    return [x0 + c, y0, x1, y0, x1, y1 - c, x1 - c, y1, x0, y1, x0, y0 + c]


def heat(t):
    """0..1 -> cyan -> blue -> magenta."""
    return mix(CYAN, BLUE, t * 2) if t < 0.5 else mix(BLUE, MAG, (t - 0.5) * 2)


class Panel(tk.Frame):
    """Card with a chamfered neon outline, corner ticks and a title tag; put children in .body."""

    def __init__(self, master, title='', sub='', accent=CYAN, pad=14, **kw):
        super().__init__(master, bg=BG, **kw)
        self.title, self.sub, self.accent = title, sub, accent
        self.cv = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.cv.place(x=0, y=0, relwidth=1, relheight=1)
        self.body = tk.Frame(self, bg=PANEL)
        self.body.pack(fill='both', expand=True, padx=px(pad), pady=(px(36) if title else px(pad), px(pad)))
        self.bind('<Configure>', lambda e: self.draw())

    def draw(self):
        cv = self.cv; cv.delete('all')
        w, h = self.winfo_width() - 1, self.winfo_height() - 1
        if w < 20 or h < 20:
            return
        c = px(14)
        cv.create_polygon(chamfer(1, 1, w - 1, h - 1, c), fill=PANEL, outline=LINE2, width=1)
        a, t = self.accent, px(18)
        cv.create_line(1, c + t, 1, c, c, 1, c + t, 1, fill=a, width=px(2))            # cut corner accent
        cv.create_line(w - 1, h - c - t, w - 1, h - c, w - c, h - 1, w - c - t, h - 1, fill=a, width=px(2))
        cv.create_line(w - px(26), 1, w - 1, 1, w - 1, px(26), fill=mix(PANEL, a, 0.5))
        cv.create_line(1, h - px(26), 1, h - 1, px(26), h - 1, fill=mix(PANEL, a, 0.5))
        if self.title:
            y = px(19)
            cv.create_rectangle(px(16), y - px(6), px(19), y + px(6), fill=a, outline='')
            cv.create_text(px(27), y, text=self.title, anchor='w', fill=TEXT, font=(ZH, 11, 'bold'))
            if self.sub:
                cv.create_text(w - px(18), y, text=self.sub, anchor='e', fill=FAINT, font=(NUM, 8))
            cv.create_line(px(16), px(32), w - px(16), px(32), fill=LINE)
            for i in range(6):
                x = px(16) + i * px(7)
                cv.create_line(x, px(32), x + px(4), px(32), fill=mix(PANEL, a, 0.7), width=px(2))


class NeonButton(tk.Canvas):
    def __init__(self, master, text, command, w=150, h=40, color=CYAN, bg=PANEL, size=11, filled=False):
        need = tkfont.Font(family=ZH, size=size, weight='bold').measure(tr(text)) + px(30)     # fits any language
        super().__init__(master, width=max(px(w), need), height=px(h), bg=bg, highlightthickness=0, bd=0, cursor='hand2')
        self.text, self.command, self.color, self.bgc, self.size, self.filled = text, command, color, bg, size, filled
        self.hover = self.down = False; self.enabled = True
        self.bind('<Enter>', lambda e: self._set(hover=True)); self.bind('<Leave>', lambda e: self._set(hover=False, down=False))
        self.bind('<ButtonPress-1>', lambda e: self._set(down=True)); self.bind('<ButtonRelease-1>', self._release)
        self.draw()

    def _set(self, **kw):
        for k, v in kw.items(): setattr(self, k, v)
        self.draw()

    def _release(self, e):
        fire = self.down and self.hover and self.enabled
        self._set(down=False)
        if fire and self.command: self.command()

    def config_look(self, text=None, color=None, filled=None, enabled=None):
        if text is not None: self.text = text
        if color is not None: self.color = color
        if filled is not None: self.filled = filled
        if enabled is not None: self.enabled = enabled
        self.draw()

    def draw(self):
        self.delete('all')
        w, h = int(self['width']), int(self['height']); c = px(10); col = self.color if self.enabled else FAINT
        if self.filled:
            fill = mix(self.bgc, col, 0.95 if self.down else (0.85 if self.hover else 0.72)); fg = BG
        else:
            fill = mix(self.bgc, col, 0.32 if self.down else (0.18 if self.hover else 0.06)); fg = col if self.enabled else DIM
        if self.hover and self.enabled:                                         # outer glow
            self.create_polygon(chamfer(0, 0, w - 1, h - 1, c), fill='', outline=mix(self.bgc, col, 0.35), width=px(3))
        self.create_polygon(chamfer(px(2), px(2), w - px(3), h - px(3), c - px(1)), fill=fill, outline=col, width=1)
        self.create_line(w - px(16), h - px(6), w - px(8), h - px(6), fill=fg if self.filled else col, width=px(2))
        self.create_text(w // 2, h // 2, text=self.text, fill=fg, font=(ZH, self.size, 'bold'))


# one-click combinations for the game scene; anything else is the player's own 自定义 combination
PRESETS = {'声感': dict(fusion='替换', trigger_mode='替换', speaker_mode='全部游戏声'),
           '全感': dict(fusion='叠加', trigger_mode='自动', speaker_mode='特殊事件')}
PRESET_KEYS = tuple(PRESETS['全感'])


def preset_of(fusion, trigger_mode, speaker_mode):
    now = dict(fusion=fusion, trigger_mode=trigger_mode, speaker_mode=speaker_mode)
    return next((name for name, vals in PRESETS.items() if vals == now), '自定义')


def bind_presets(preset, sv):
    """preset: the StringVar of the picker; sv: the scene's settings. Picking 自定义 brings back the
    player's own combination, which is remembered whenever the settings match no preset."""
    busy = {'on': False}

    def current():
        return preset_of(*(sv[k].get() for k in PRESET_KEYS))

    def remember():
        if current() == '自定义':
            for k in PRESET_KEYS:
                sv['custom_' + k].set(sv[k].get())

    def choose(*_a):
        if busy['on']:
            return
        name = preset.get()
        vals = PRESETS.get(name) or {k: sv['custom_' + k].get() for k in PRESET_KEYS}
        busy['on'] = True
        for k, v in vals.items():
            sv[k].set(v)
        if sv['mode'].get() == '普通':                    # no game effects reach us without a virtual pad
            sv['mode'].set('原生透传')
        busy['on'] = False

    def follow(*_a):                                     # changed by hand
        if busy['on']:
            return
        busy['on'] = True
        remember(); preset.set(current())
        busy['on'] = False
    remember(); preset.set(current())
    preset.trace_add('write', choose)
    for k in PRESET_KEYS:
        sv[k].trace_add('write', follow)


def pad_status():
    """How the controller is connected right now: 'usb', 'edge', 'bluetooth' or None."""
    from ds_hid import find_path, is_bluetooth
    path, pid = find_path()
    if path is None:
        return None
    if is_bluetooth(path):
        return 'bluetooth'
    return 'edge' if pid == 0x0DF2 else 'usb'


def next_step(run, pad, missing):
    """What a person should do next: (message, action), or None while running."""
    if run:
        return None
    if missing:
        return '还缺驱动：' + '、'.join(missing) + '。装好才能用当前的连接方式。', 'deps'
    if pad is None:
        return '没检测到手柄：请用 USB 线或 DS5Dongle 接收器（测试版）连接。已经插着还认不到，就点「修复手柄」。', 'repair'
    if pad == 'bluetooth':
        return '手柄现在是蓝牙连接，音圈和扬声器用不了：请改用 USB 线或 DS5Dongle 接收器（测试版）。', None
    return '手柄已就绪：点右上角「启动」，再打开游戏。游戏已经开着的话，要重启游戏才能认到虚拟手柄。', 'start'


class Segmented(tk.Canvas):
    """Option picker drawn as HUD cells (replaces OptionMenu)."""

    def __init__(self, master, var, options, cell=118, h=34, cols=None, bg=PANEL, color=CYAN, rec=None):
        self.rec = rec; self.opts = list(options); self.var = var; self.h = px(h); self.color = color
        # cell width fits the longest (translated) option and the "recommended" badge
        fb = tkfont.Font(family=ZH, size=10, weight='bold'); ft = tkfont.Font(family=ZH, size=7, weight='bold')
        self.bw = max(px(30), ft.measure(tr('推荐')) + px(8))
        need = max(fb.measure(tr(o)) + px(26) + ((self.bw + px(8)) if o == rec else 0) for o in self.opts)
        self.cellw = max(px(cell), need)
        self.cols = cols or len(self.opts); rows = math.ceil(len(self.opts) / self.cols)
        gap = px(6)
        super().__init__(master, width=self.cols * (self.cellw + gap), height=rows * (self.h + gap), bg=bg, highlightthickness=0, bd=0, cursor='hand2')
        self.bgc = bg; self.gap = gap; self.hover = -1
        self.bind('<Button-1>', self._click); self.bind('<Motion>', self._motion); self.bind('<Leave>', lambda e: self._hov(-1))
        var.trace_add('write', lambda *a: self.draw()); self.draw()

    def _at(self, e):
        i = (e.y // (self.h + self.gap)) * self.cols + e.x // (self.cellw + self.gap)
        return i if 0 <= i < len(self.opts) else -1

    def _hov(self, i):
        if i != self.hover: self.hover = i; self.draw()

    def _motion(self, e): self._hov(self._at(e))

    def _click(self, e):
        i = self._at(e)
        if i >= 0: self.var.set(self.opts[i])

    def draw(self):
        self.delete('all')
        for i, o in enumerate(self.opts):
            r, c = divmod(i, self.cols); x = c * (self.cellw + self.gap); y = r * (self.h + self.gap)
            sel = self.var.get() == o; hov = i == self.hover
            col = self.color if sel else (mix(LINE2, self.color, 0.4) if hov else LINE2)
            fill = mix(self.bgc, self.color, 0.22 if sel else (0.08 if hov else 0.0))
            self.create_polygon(chamfer(x + 1, y + 1, x + self.cellw - 1, y + self.h - 1, px(7)), fill=fill, outline=col)
            if sel:
                self.create_rectangle(x + px(8), y + self.h - px(4), x + px(22), y + self.h - px(2), fill=self.color, outline='')
            tx = x + self.cellw // 2
            if o == self.rec:                                   # recommended option: green tag, like the mode cards
                bw, bh = self.bw, px(14)
                self.create_rectangle(x + self.cellw - bw - px(4), y + px(3), x + self.cellw - px(4), y + px(3) + bh,
                                      fill=mix(self.bgc, GREEN, 0.15), outline=GREEN)
                self.create_text(x + self.cellw - bw / 2 - px(4), y + px(3) + bh / 2, text='推荐', fill=GREEN, font=(ZH, 7, 'bold'))
                tx = x + (self.cellw - bw - px(8)) // 2
            self.create_text(tx, y + self.h // 2 + (px(3) if o == self.rec else 0), text=o, fill=TEXT if sel else DIM,
                             font=(ZH, 10, 'bold' if sel else 'normal'))


class Slider(tk.Canvas):
    def __init__(self, master, label, var, frm=0, to=200, unit='%', bg=PANEL, color=CYAN, hint=''):
        super().__init__(master, height=px(56), bg=bg, highlightthickness=0, bd=0, cursor='hand2')
        self.label, self.var, self.frm, self.to, self.unit, self.bgc, self.color, self.hint = label, var, frm, to, unit, bg, color, hint
        self.bind('<Configure>', lambda e: self.draw()); self.bind('<Button-1>', self._drag); self.bind('<B1-Motion>', self._drag)
        # no mouse wheel here: the wheel scrolls the page (it used to change a slider the pointer passed over)
        var.trace_add('write', lambda *a: self.draw())

    def _geom(self):
        return px(6), self.winfo_width() - px(6), px(38)

    def _step(self, d):
        self.var.set(max(self.frm, min(self.to, round(self.var.get() + d))))

    def _drag(self, e):
        x0, x1, _ = self._geom()
        t = max(0.0, min(1.0, (e.x - x0) / max(1, x1 - x0)))
        self.var.set(round(self.frm + t * (self.to - self.frm)))

    def draw(self):
        self.delete('all'); x0, x1, y = self._geom()
        if x1 - x0 < 20: return
        v = self.var.get(); t = (v - self.frm) / (self.to - self.frm); xv = x0 + t * (x1 - x0)
        self.create_text(x0, px(12), text=self.label, anchor='w', fill=TEXT, font=(ZH, 10))
        if self.hint:
            self.create_text(x0 + px(8) + len(self.label) * px(14), px(12), text=self.hint, anchor='w', fill=FAINT, font=(ZH, 8))
        self.create_text(x1, px(12), text=f'{int(round(v))}{self.unit}', anchor='e', fill=self.color, font=(NUMB, 13))
        for i in range(21):                                                    # ruler ticks
            x = x0 + i * (x1 - x0) / 20; big = i % 5 == 0
            self.create_line(x, y + px(7), x, y + px(12 if big else 9), fill=LINE2 if big else LINE)
        self.create_rectangle(x0, y - px(2), x1, y + px(2), fill=LINE, outline='')
        n = 40                                                                 # gradient fill
        for i in range(n):
            a = x0 + (xv - x0) * i / n; b = x0 + (xv - x0) * (i + 1) / n
            self.create_rectangle(a, y - px(2), b + 1, y + px(2), fill=mix(mix(self.bgc, self.color, 0.25), self.color, i / n), outline='')
        if self.frm < 100 < self.to:                                           # 100 % mark
            xm = x0 + (100 - self.frm) / (self.to - self.frm) * (x1 - x0)
            self.create_line(xm, y - px(8), xm, y - px(4), fill=DIM)
        k = px(8)
        self.create_polygon(xv, y - k, xv + k, y, xv, y + k, xv - k, y, fill=BG, outline=self.color, width=px(2))
        self.create_oval(xv - px(2), y - px(2), xv + px(2), y + px(2), fill=self.color, outline='')


class Toggle(tk.Frame):
    def __init__(self, master, text, var, sub='', bg=PANEL, color=CYAN, wrap=560):
        super().__init__(master, bg=bg, cursor='hand2')
        self.var, self.bgc, self.color = var, bg, color
        self.sw = tk.Canvas(self, width=px(46), height=px(24), bg=bg, highlightthickness=0, bd=0)
        self.sw.pack(side='left', anchor='n', pady=(px(2), 0))
        txt = tk.Frame(self, bg=bg); txt.pack(side='left', fill='x', expand=True, padx=(px(12), 0))
        self.l1 = tk.Label(txt, text=text, bg=bg, fg=TEXT, font=(ZH, 10, 'bold'), anchor='w', justify='left')
        self.l1.pack(fill='x')
        ws = [self, self.sw, txt, self.l1]
        if sub:
            l2 = tk.Label(txt, text=sub, bg=bg, fg=DIM, font=(ZH, 9), anchor='w', justify='left', wraplength=px(wrap))
            l2.pack(fill='x'); ws.append(l2)
        for wdg in ws: wdg.bind('<Button-1>', lambda e: var.set(not var.get()))
        var.trace_add('write', lambda *a: self.draw()); self.draw()

    def draw(self):
        cv = self.sw; cv.delete('all'); on = bool(self.var.get()); w, h = px(46), px(24)
        col = self.color if on else FAINT
        cv.create_polygon(chamfer(1, 1, w - 2, h - 2, px(6)), fill=mix(self.bgc, col, 0.25 if on else 0.05), outline=col)
        x = w - px(14) if on else px(13)
        cv.create_rectangle(x - px(7), px(6), x + px(7), h - px(7), fill=col, outline='')
        cv.create_text(px(15) if on else w - px(14), h // 2, text='ON' if on else 'OFF', fill=col, font=(NUMB, 6))


class GameCard(tk.Canvas):
    """Library card: cover art, title, status chip, badge; click = action, gear (bottom-right) = settings."""
    W, H = 220, 300

    def __init__(self, master, title, sub, art='general', is_on=lambda: True, badge=lambda: None,
                 on_click=None, on_gear=None, accent=CYAN, on_text='已开启', off_text='未开启'):
        super().__init__(master, width=px(self.W), height=px(self.H), bg=BG, highlightthickness=0, bd=0, cursor='hand2')
        import covers
        self.covers = covers
        self.title, self.sub, self.is_on, self.badge = title, sub, is_on, badge
        self.on_click, self.on_gear, self.accent = on_click, on_gear, accent
        self.on_text, self.off_text = on_text, off_text
        self.src = covers.generated(art, px(self.W), px(self.H)); self.photo = None; self.face = None
        self.hover = False; self.gear_hover = False
        self.bind('<Enter>', lambda e: self._hov(True)); self.bind('<Leave>', lambda e: self._hov(False, False))
        self.bind('<Motion>', lambda e: self._hov(True, self._in_gear(e))); self.bind('<Button-1>', self._click)
        self.redraw()

    def set_cover(self, img):
        self.src = img; self.face = None; self.redraw()

    def _gear_xy(self):
        return px(self.W) - px(28), px(self.H) - px(28), px(17)

    def _in_gear(self, e):
        x, y, r = self._gear_xy()
        return (e.x - x) ** 2 + (e.y - y) ** 2 <= (r + px(4)) ** 2

    def _hov(self, h, g=None):
        g = self.gear_hover if g is None else g
        if (h, g) != (self.hover, self.gear_hover):
            self.hover, self.gear_hover = h, g; self.redraw()

    def _click(self, e):
        if self._in_gear(e):
            if self.on_gear: self.on_gear()
        elif self.on_click:
            self.on_click()

    def redraw(self):
        from PIL import ImageTk
        w, h = px(self.W), px(self.H); on = bool(self.is_on())
        if self.face != (on, id(self.src)):
            self.photo = ImageTk.PhotoImage(self.covers.compose(self.src, w, h, on)); self.face = (on, id(self.src))
        self.delete('all')
        self.create_image(0, 0, image=self.photo, anchor='nw')
        col = self.accent if on else (mix(LINE2, self.accent, 0.5) if self.hover else LINE2)
        c = px(14)
        if self.hover:
            self.create_polygon(chamfer(1, 1, w - 2, h - 2, c), fill='', outline=mix(BG, self.accent, 0.6), width=px(4))
        self.create_polygon(chamfer(1, 1, w - 2, h - 2, c), fill='', outline=col, width=px(2) if on else 1)
        b = self.badge()
        if b:
            text, bc = b; tw = px(13) * len(text) + px(14)
            self.create_rectangle(px(10), px(10), px(10) + tw, px(32), fill=bc, outline='')
            self.create_text(px(10) + tw / 2, px(21), text=text, fill=BG, font=(ZH, 9, 'bold'))
        st = self.on_text if on else self.off_text
        sw = px(13) * len(st) + px(18)
        self.create_rectangle(w - sw - px(10), px(10), w - px(10), px(32), fill=mix(BG, self.accent if on else FAINT, 0.25),
                              outline=self.accent if on else FAINT)
        self.create_text(w - sw / 2 - px(10), px(21), text=st, fill=self.accent if on else DIM, font=(ZH, 9, 'bold'))
        self.create_text(px(16), h - px(56), text=self.title, anchor='w', fill=TEXT if on else DIM, font=(ZH, 15, 'bold'))
        self.create_text(px(16), h - px(30), text=self.sub, anchor='w', fill=DIM if on else FAINT, font=(ZH, 8), width=w - px(70))
        x, y, r = self._gear_xy(); gc = self.accent if self.gear_hover else (TEXT if self.hover else DIM)
        self.create_oval(x - r, y - r, x + r, y + r, fill=mix(BG, gc, 0.18 if self.gear_hover else 0.08), outline=gc, width=px(2))
        self.create_text(x, y, text='', fill=gc, font=('Segoe MDL2 Assets', 12))   # Windows 'Settings' gear glyph


class ModeCards(tk.Frame):
    def __init__(self, master, var, items, bg=PANEL):
        super().__init__(master, bg=bg); self.var = var; self.cards = []
        for i, (val, title, tag, desc) in enumerate(items):
            cv = tk.Canvas(self, height=px(176), bg=bg, highlightthickness=0, bd=0, cursor='hand2')
            cv.grid(row=0, column=i, sticky='nsew', padx=(0 if i == 0 else px(10), 0))
            self.columnconfigure(i, weight=1, uniform='m')
            cv.bind('<Button-1>', lambda e, v=val: var.set(v)); cv.bind('<Configure>', lambda e: self.draw())
            self.cards.append((cv, val, title, tag, desc))
        var.trace_add('write', lambda *a: self.draw())

    def draw(self):
        for cv, val, title, tag, desc in self.cards:
            cv.delete('all'); w, h = cv.winfo_width(), cv.winfo_height()
            if w < 30: continue
            sel = self.var.get() == val; col = CYAN if sel else LINE2
            cv.create_polygon(chamfer(1, 1, w - 2, h - 2, px(12)), fill=mix(PANEL, CYAN, 0.12 if sel else 0.0), outline=col, width=px(2) if sel else 1)
            cv.create_text(px(16), px(22), text=title, anchor='w', fill=TEXT if sel else DIM, font=(ZH, 12, 'bold'))
            if tag:
                tw = tkfont.Font(family=ZH, size=8, weight='bold').measure(tr(tag)) + px(14)   # fits any language
                title_w = tkfont.Font(family=ZH, size=12, weight='bold').measure(tr(title))
                beside = px(16) + title_w + px(8) + tw + px(12) <= w     # narrow card: tag moves to the bottom left
                ty = px(22) if beside else h - px(22)
                x1 = w - px(12) if beside else px(16) + tw
                cv.create_rectangle(x1 - tw, ty - px(9), x1, ty + px(9), fill=mix(PANEL, GREEN, 0.15), outline=GREEN)
                cv.create_text(x1 - tw / 2, ty, text=tag, fill=GREEN, font=(ZH, 8, 'bold'))
            cv.create_text(px(16), px(44), text=desc, anchor='nw', fill=TEXT if sel else DIM, font=(ZH, 9), width=w - px(32))
            if sel:
                cv.create_oval(w - px(26), h - px(24), w - px(14), h - px(12), outline=CYAN, width=px(2))
                cv.create_oval(w - px(23), h - px(21), w - px(17), h - px(15), fill=CYAN, outline='')


# ---------------------------------------------------------------- live dashboard gauges
class GripMeter(tk.Canvas):
    SEG = 22

    def __init__(self, master, bg=PANEL):
        super().__init__(master, width=px(150), height=px(250), bg=bg, highlightthickness=0, bd=0)
        self.items = {'L': [], 'R': []}; self.peak = {}; self.vals = {'L': 0.0, 'R': 0.0}; self.pk = {'L': 0.0, 'R': 0.0}
        self.lit = {'L': 0, 'R': 0}; self.pky = {'L': None, 'R': None}
        H = px(214); y0 = px(10); sh = H / self.SEG
        for side, x in (('L', px(28)), ('R', px(92))):
            for i in range(self.SEG):
                y = y0 + H - (i + 1) * sh
                self.items[side].append(self.create_polygon(chamfer(x, y + px(1), x + px(40), y + sh - px(2), px(3)), fill=LINE, outline=''))
            self.peak[side] = self.create_line(x - px(4), y0 + H, x + px(44), y0 + H, fill=CYAN, width=px(2))
            self.create_text(x + px(20), px(240), text=side + ' 握把', fill=DIM, font=(ZH, 9))
        for i in range(5):
            y = y0 + H - i * H / 4
            self.create_line(px(10), y, px(20), y, fill=FAINT); self.create_line(px(140), y, px(130), y, fill=FAINT)
        self.H, self.y0 = H, y0

    def update_vals(self, l, r):
        for side, v in (('L', l), ('R', r)):
            v = max(0.0, min(1.0, v)); s = self.vals[side] = max(v, self.vals[side] * 0.82)
            self.pk[side] = max(s, self.pk[side] - 0.01)
            lit = int(round(s * self.SEG)); old = self.lit[side]
            if lit != old:                                     # only the segments that changed
                for i in range(min(lit, old), max(lit, old)):
                    self.itemconfigure(self.items[side][i], fill=heat(i / (self.SEG - 1)) if i < lit else (LINE if i % 2 else mix(LINE, PANEL, 0.4)))
                self.lit[side] = lit
            y = int(self.y0 + self.H * (1 - self.pk[side])); x = px(28) if side == 'L' else px(92)
            if y != self.pky[side]:
                self.coords(self.peak[side], x - px(4), y, x + px(44), y); self.pky[side] = y


class Scope(tk.Canvas):
    N = 70

    def __init__(self, master, bg=PANEL):
        super().__init__(master, height=px(210), bg=bg, highlightthickness=0, bd=0)
        self.hist = {'a': [0.0] * self.N, 'b': [0.0] * self.N}; self.sweep = 0
        self.bind('<Configure>', lambda e: self._grid())
        self.la = self.create_line(0, 0, 1, 1, fill=CYAN, width=px(2))
        self.lb = self.create_line(0, 0, 1, 1, fill=AMBER, width=1)
        self.flat = False
        self.scan = self.create_line(0, 0, 0, 1, fill=mix(PANEL, CYAN, 0.4))

    def _grid(self):
        self.delete('grid'); w, h = self.winfo_width(), self.winfo_height()
        for i in range(1, 12):
            self.create_line(w * i / 12, 0, w * i / 12, h, fill=mix(PANEL, LINE, 0.7), tags='grid')
        for i in range(1, 6):
            self.create_line(0, h * i / 6, w, h * i / 6, fill=mix(PANEL, LINE, 0.7) if i != 3 else LINE, tags='grid')
        lg = self.create_text(px(6), px(8), text='触感输出', anchor='nw', fill=CYAN, font=(ZH, 8), tags='grid')
        self.create_text(max(px(70), self.bbox(lg)[2] + px(14)), px(8), text='游戏原生', anchor='nw', fill=AMBER, font=(ZH, 8), tags='grid')
        self.tag_lower('grid')

    def push(self, a, b, idle=False):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 20: return
        for k, v in (('a', a), ('b', b)):
            self.hist[k] = self.hist[k][1:] + [max(0.0, min(1.0, v))]
        mid = h / 2
        flat = max(self.hist['a']) < 0.002 and max(self.hist['b']) < 0.002
        if flat and self.flat and not idle:
            return                                             # nothing moving: no redraw
        self.flat = flat
        def pts(hs, sign):
            out = []
            for i, v in enumerate(hs):
                wob = math.sin(i * 0.9 + time.time() * 14) * 0.6 + 0.4
                out += [w * i / (self.N - 1), mid - sign * v * wob * (h * 0.46)]
            return out
        self.coords(self.la, *pts(self.hist['a'], 1)); self.coords(self.lb, *pts(self.hist['b'], -1))
        self.sweep = (self.sweep + px(6)) % max(1, w)
        self.coords(self.scan, self.sweep, 0, self.sweep, h)
        self.itemconfigure(self.scan, state='normal' if idle else 'hidden')


class TriggerGauge(tk.Canvas):
    def __init__(self, master, side, bg=PANEL):
        sz = px(150)
        super().__init__(master, width=sz, height=px(170), bg=bg, highlightthickness=0, bd=0)
        self.side = side; c = sz / 2; r1, r2 = sz * 0.44, sz * 0.33; self.c = c
        self.create_arc(c - r1, c - r1, c + r1, c + r1, start=-30, extent=240, style='arc', outline=LINE, width=px(8))
        self.create_arc(c - r2, c - r2, c + r2, c + r2, start=-30, extent=240, style='arc', outline=LINE, width=px(5))
        for i in range(13):
            a = math.radians(210 - i * 20); ro = sz * 0.5
            self.create_line(c + math.cos(a) * (r1 + px(7)), c - math.sin(a) * (r1 + px(7)), c + math.cos(a) * ro, c - math.sin(a) * ro, fill=FAINT)
        self.press = self.create_arc(c - r1, c - r1, c + r1, c + r1, start=210, extent=0, style='arc', outline=CYAN, width=px(8))
        self.vib = self.create_arc(c - r2, c - r2, c + r2, c + r2, start=210, extent=0, style='arc', outline=MAG, width=px(5))
        self.create_text(c, c - px(14), text=side + '2', fill=DIM, font=(NUMB, 11))
        self.num = self.create_text(c, c + px(10), text='0', fill=TEXT, font=(NUMB, 20))
        self.tag = self.create_text(c, px(158), text='原生扳机  —', fill=FAINT, font=(ZH, 9))
        self.vp = self.vv = 0.0

    def update_vals(self, press, vib, native):
        self.vp = press; self.vv = max(vib, self.vv * 0.85)
        key = (int(self.vp * 100), int(self.vv * 60), bool(native))
        if key == getattr(self, '_last', None):
            return
        self._last = key
        self.itemconfigure(self.press, extent=-240 * max(0.0, min(1.0, self.vp)))
        self.itemconfigure(self.vib, extent=-240 * max(0.0, min(1.0, self.vv)))
        self.itemconfigure(self.num, text=f'{key[0]}')
        self.itemconfigure(self.tag, text='原生扳机  ON' if native else '原生扳机  —', fill=GREEN if native else FAINT)


class SkillHex(tk.Canvas):
    NAMES = (('resonance', '技能'), ('echo', '声骸'), ('liberation', '大招'))

    def __init__(self, master, bg=PANEL):
        super().__init__(master, width=px(330), height=px(130), bg=bg, highlightthickness=0, bd=0)
        self.hx = {}
        for i, (k, n) in enumerate(self.NAMES):
            cx, cy, r = px(55) + i * px(110), px(56), px(40)
            pts = []
            for j in range(6):
                a = math.radians(60 * j + 30); pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
            glow = self.create_polygon([cx + (x - cx) * 1.14 if q % 2 == 0 else cy + (x - cy) * 1.14 for q, x in enumerate(pts)], fill='', outline=BG, width=px(2))
            hexa = self.create_polygon(pts, fill=PANEL2, outline=LINE2, width=px(2))
            t = self.create_text(cx, cy - px(6), text=n, fill=DIM, font=(ZH, 12, 'bold'))
            st = self.create_text(cx, cy + px(14), text='—', fill=FAINT, font=(ZH, 8))
            self.hx[k] = (glow, hexa, t, st)
        self.note = self.create_text(px(165), px(118), text='切到鸣潮后自动识别技能栏', fill=FAINT, font=(ZH, 8))

    def update_vals(self, states, note):
        pulse = 0.5 + 0.5 * math.sin(time.time() * 6)
        for k, (glow, hexa, t, st) in self.hx.items():
            v = states.get(k) if states else None
            if v:
                self.itemconfigure(hexa, fill=mix(PANEL2, CYAN, 0.28), outline=CYAN); self.itemconfigure(t, fill=TEXT)
                self.itemconfigure(glow, outline=mix(PANEL, CYAN, 0.3 + 0.5 * pulse)); self.itemconfigure(st, text='就绪', fill=CYAN)
            else:
                self.itemconfigure(hexa, fill=PANEL2, outline=LINE2 if v is False else FAINT); self.itemconfigure(t, fill=DIM)
                self.itemconfigure(glow, outline=PANEL); self.itemconfigure(st, text='冷却' if v is False else '—', fill=FAINT)
        self.itemconfigure(self.note, text=note)


class InfoRow(tk.Frame):
    def __init__(self, master, key, bg=PANEL):
        super().__init__(master, bg=bg)
        tk.Label(self, text=key, bg=bg, fg=DIM, font=(ZH, 9), width=8 if i18n.LANG == 'zh' else 13, anchor='w').pack(side='left')
        self.v = tk.Label(self, text='—', bg=bg, fg=TEXT, font=(ZH, 9), anchor='w', justify='left')
        self.v.pack(side='left', fill='x', expand=True)

    def set(self, text, color=TEXT):
        text = tr(text)
        if self.v['text'] != text or self.v['fg'] != color:
            self.v.configure(text=text, fg=color)


# ---------------------------------------------------------------- window
class DemoRunner:
    """--demo: fake stats so the dashboard can be previewed without a pad."""

    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log; self.stop_ev = threading.Event(); self.stats = {}
        self.live_ver = 0; self.start_cfg = dict(cfg)

    def run(self):
        self.log('演示模式：数据是模拟的，没有连接手柄。'); t0 = time.time()
        while not self.stop_ev.is_set():
            t = time.time() - t0; hit = max(0.0, math.sin(t * 2.3)) ** 6
            self.stats = {'battery': (75, 'discharging'), 'level': hit, 'grip': (hit * 0.9 + random.random() * 0.1, hit * 0.7 + random.random() * 0.12),
                          'trigger': (0.2 * hit, 0.6 * hit), 'press': ((math.sin(t) + 1) / 2, max(0, math.sin(t * 0.7))),
                          'active': True, 'source': '扬声器 (Realtek)  2声道 48000Hz', 'late': 0,
                          'rumble': (max(0.0, math.sin(t * 1.3)) ** 4, 0.0), 'speaker': hit * 0.8, 'vpad': (int(t * 250), int(t * 1000)),
                          'takeover': True, 'native_trig': (False, True), 'fx_last': '大招释放', 'profile': '鸣潮',
                          'ww': ({'resonance': True, 'echo': t % 6 < 3, 'liberation': t % 10 < 4}, int(t * 20), '')}
            time.sleep(0.02)
        self.log('已停止。')


def _dark_titlebar(root):
    try:
        hwnd = C.windll.user32.GetParent(root.winfo_id())
        for attr, val in ((20, 1), (35, 0x000C0603), (36, 0x00FFECD8), (34, 0x0063421D)):  # dark, caption, text, border
            C.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, C.byref(C.c_int(val)), 4)
    except Exception:
        pass


def install_translation():
    """Translate every text at the moment it reaches the screen (program values stay Chinese)."""
    if i18n.LANG == 'zh' or getattr(tk, '_i18n_on', False):
        return
    tk._i18n_on = True

    def fix(kw):
        if 'text' in kw:
            kw['text'] = tr(kw['text'])
        return kw
    ct = tk.Canvas.create_text
    tk.Canvas.create_text = lambda self, *a, **kw: ct(self, *a, **fix(kw))
    ic = tk.Canvas.itemconfigure

    def itemconfigure(self, tagOrId, cnf=None, **kw):
        if isinstance(cnf, dict):
            cnf = fix(dict(cnf))
        return ic(self, tagOrId, cnf, **fix(kw))
    tk.Canvas.itemconfigure = tk.Canvas.itemconfig = itemconfigure
    li = tk.Label.__init__
    tk.Label.__init__ = lambda self, master=None, cnf={}, **kw: li(self, master, cnf, **fix(kw))
    lc = tk.Label.configure

    def lconf(self, cnf=None, **kw):
        if isinstance(cnf, dict):
            cnf = fix(dict(cnf))
        return lc(self, cnf, **fix(kw))
    tk.Label.configure = tk.Label.config = lconf
    ti = tk.Text.insert
    tk.Text.insert = lambda self, index, chars, *args: ti(self, index, tr(chars), *args)
    wt = tk.Wm.wm_title
    tk.Wm.wm_title = tk.Wm.title = lambda self, string=None: wt(self, tr(string) if string else string)


def other_fullscreen(own_hwnd=0):
    """True while ANOTHER program is fullscreen: exclusive fullscreen (D3D), or a window whose content
    area (client rect, no title bar) covers its whole monitor - a borderless fullscreen game. A normal
    maximized window always keeps its title bar, so it never counts. The dashboard then stops drawing."""
    try:
        u = ctypes.windll.user32
        st = ctypes.c_int(0)
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(st)) == 0 and st.value == 3:
            return True                                   # QUNS_RUNNING_D3D_FULL_SCREEN (exclusive fullscreen)
        u.GetForegroundWindow.restype = ctypes.c_void_p
        h = u.GetForegroundWindow()
        if not h:
            return False
        pid = ctypes.c_ulong(0); u.GetWindowThreadProcessId(ctypes.c_void_p(h), ctypes.byref(pid))
        if pid.value == os.getpid():
            return False                                  # that's us
        cls = ctypes.create_unicode_buffer(64); u.GetClassNameW(ctypes.c_void_p(h), cls, 64)
        if cls.value in ('Progman', 'WorkerW', 'Shell_TrayWnd'):
            return False                                  # the desktop itself
        cr = (ctypes.c_long * 4)()
        if not u.GetClientRect(ctypes.c_void_p(h), cr):
            return False
        tl = (ctypes.c_long * 2)(cr[0], cr[1]); br = (ctypes.c_long * 2)(cr[2], cr[3])
        u.ClientToScreen(ctypes.c_void_p(h), tl); u.ClientToScreen(ctypes.c_void_p(h), br)
        u.MonitorFromWindow.restype = ctypes.c_void_p
        mon = u.MonitorFromWindow(ctypes.c_void_p(h), 2)
        mi = (ctypes.c_ulong * 10)(); mi[0] = 40          # MONITORINFO: cbSize, rcMonitor, rcWork, dwFlags
        if not u.GetMonitorInfoW(ctypes.c_void_p(mon), mi):
            return False
        ml, mt, mr, mb = (ctypes.c_long * 4).from_buffer(mi, 4)
        return tl[0] <= ml and tl[1] <= mt and br[0] >= mr and br[1] >= mb
    except Exception:
        return False


def run_ui(cfg, runner_cls, save_cfg, test_fn, log_path=None):
    global ZH
    ZH = i18n.ui_font()                    # Korean / Japanese / English need fonts with their glyphs
    install_translation()
    global S
    try: C.windll.shcore.SetProcessDpiAwareness(1)          # crisp on 4K instead of bitmap-stretched
    except Exception: pass
    try: C.windll.shell32.SetCurrentProcessExplicitAppUserModelID('HapticLink.PS5Haptics')   # own taskbar identity
    except Exception: pass
    root = tk.Tk(); root.title(f'PS5 手柄 · 声音触感 {VERSION}'); root.configure(bg=BG)
    try:
        from PIL import ImageTk
        from app_icon import make_icon
        root._icons = [ImageTk.PhotoImage(make_icon(n)) for n in (64, 32, 16)]
        root.iconphoto(True, *root._icons)
    except Exception:
        pass
    S = root.winfo_fpixels('1i') / 96.0
    root.geometry(f'{px(1240)}x{px(830)}'); root.minsize(px(1100), px(760))
    _dark_titlebar(root)
    msgs = queue.Queue(); runner = {'r': None, 't': None}

    V = {
        'grip_profile': tk.StringVar(value=cfg['grip_profile']), 'trigger_profile': tk.StringVar(value=cfg['trigger_profile']),
        'haptic_style': tk.StringVar(value={'标准': '游戏', '电影': '影院', '音乐': '律动'}.get(cfg.get('haptic_style', '游戏'), cfg.get('haptic_style', '游戏'))),
        'pbz_fx': tk.BooleanVar(value=cfg.get('pbz_fx', cfg.get('event_fx', True))),
        'ww_enabled': tk.BooleanVar(value=cfg.get('ww_enabled', True)),
        **{k: tk.DoubleVar(value=cfg.get(k, 1.0) * 100) for k in ('cinema_rumble', 'cinema_kick', 'cinema_trigger', 'music_kick', 'music_tick', 'music_trigger')},
        'ww_fx': tk.BooleanVar(value=cfg.get('ww_fx', cfg.get('event_fx', True))),
        'stick_dz_l': tk.DoubleVar(value=cfg.get('stick_dz_l', 0.0) * 100), 'stick_dz_r': tk.DoubleVar(value=cfg.get('stick_dz_r', 0.0) * 100),
        'swap_grips': tk.BooleanVar(value=cfg.get('swap_grips', False)), 'volume_comp': tk.BooleanVar(value=cfg.get('volume_comp', False)),
        'grip_gain': tk.DoubleVar(value=cfg['grip_gain'] * 100), 'trigger_gain': tk.DoubleVar(value=cfg['trigger_gain'] * 100),
        'skip_center': tk.BooleanVar(value=cfg['skip_center']), 'foreground_exe': tk.StringVar(value=cfg['foreground_exe']),
        'mode': tk.StringVar(value=cfg['mode']), 'fusion': tk.StringVar(value=FUSION_ALIAS.get(cfg['fusion'], cfg['fusion'])),
        'rumble_gain': tk.DoubleVar(value=cfg['rumble_gain'] * 100), 'speaker_mode': tk.StringVar(value={'仅大招/特殊技': '仅关键事件', '智能增强': '全部游戏声音'}.get(cfg['speaker_mode'], cfg['speaker_mode'])),
        'speaker_volume': tk.DoubleVar(value=cfg['speaker_volume'] * 100), 'record_native': tk.BooleanVar(value=cfg.get('record_native', True)),
        'event_fx': tk.BooleanVar(value=cfg.get('event_fx', True)), 'ww_vision': tk.BooleanVar(value=cfg.get('ww_vision', True)),
        'ww_click_gate': tk.BooleanVar(value=cfg.get('ww_click_gate', True)), 'game_profiles': tk.BooleanVar(value=cfg.get('game_profiles', True)), 'capture': tk.StringVar(value=cfg.get('capture', '自动')), 'speaker_scene': tk.BooleanVar(value=cfg.get('speaker_scene', True)), 'pbz_log_dir': tk.StringVar(value=cfg.get('pbz_log_dir', '')),
    }
    PCT = ('grip_gain', 'trigger_gain', 'rumble_gain', 'speaker_volume', 'cinema_rumble', 'cinema_kick', 'cinema_trigger',
           'music_kick', 'music_tick', 'music_trigger', 'button_gain', 'stick_dz_l', 'stick_dz_r', 'grip_left_gain', 'grip_right_gain', 'trigger_left_gain', 'trigger_right_gain')

    # ---- scenes: every usage scenario keeps its own complete setup
    SCENES = ('游戏', '影院', '音乐')
    STYLE_OF = {'游戏': '游戏', '影院': '影院', '音乐': '律动'}

    def make_vars(vals):
        return {k: (tk.DoubleVar(value=v * 100) if k in PCT else tk.BooleanVar(value=v) if isinstance(v, bool)
                    else tk.IntVar(value=v) if isinstance(v, int) else tk.StringVar(value=v)) for k, v in vals.items()}
    SCENE_DEF = {
        '游戏': dict(mode='原生透传', voice_gate=True, fusion='叠加', trigger_mode='自动', custom_fusion='叠加', custom_trigger_mode='叠加', custom_speaker_mode='全部游戏声', grip_profile='均衡', trigger_profile='均衡冲击', grip_gain=1.0, trigger_gain=1.0,
                   rumble_gain=1.0, speaker_mode='特殊事件', speaker_eq='原味', speaker_volume=0.6, capture='自动', skip_center=True, button_haptics=False, trigger_haptics=False, stick_haptics=False, touch_haptics=False, button_gain=1.0, speaker_output='扬声器', grip_left_gain=1.0, grip_right_gain=1.0, trigger_left_gain=1.0, trigger_right_gain=1.0),
        '影院': dict(mode='普通', voice_gate=False, fusion='叠加', trigger_mode='自动', grip_profile='均衡', trigger_profile='均衡冲击', grip_gain=1.0, trigger_gain=0.8,
                   rumble_gain=1.0, speaker_mode='全部游戏声', speaker_eq='低音饱满', speaker_volume=0.7, capture='自动', skip_center=True, button_haptics=False, trigger_haptics=False, stick_haptics=False, touch_haptics=False, button_gain=1.0, speaker_output='扬声器', grip_left_gain=1.0, grip_right_gain=1.0, trigger_left_gain=1.0, trigger_right_gain=1.0),
        '音乐': dict(mode='普通', voice_gate=False, fusion='叠加', trigger_mode='自动', grip_profile='均衡', trigger_profile='均衡冲击', grip_gain=1.0, trigger_gain=1.0,
                   rumble_gain=1.0, speaker_mode='全部游戏声', speaker_eq='低音饱满', speaker_volume=0.6, capture='自动', skip_center=False, button_haptics=False, trigger_haptics=False, stick_haptics=False, touch_haptics=False, button_gain=1.0, speaker_output='扬声器', grip_left_gain=1.0, grip_right_gain=1.0, trigger_left_gain=1.0, trigger_right_gain=1.0),
    }
    import trigger_presets as TP
    for _d in SCENE_DEF.values():
        _d.update(TP.scene_defaults())
    saved = dict(cfg.get('scenes') or {})
    if '游戏' not in saved:                                   # first run of the scene system: keep the user's old setup
        saved['游戏'] = {k: cfg.get(k, v) for k, v in SCENE_DEF['游戏'].items()}
    V['scene'] = tk.StringVar(value=cfg.get('scene', '游戏') if cfg.get('scene') in SCENES else '游戏')
    SV = {}
    for sc in SCENES:
        vals = dict(SCENE_DEF[sc]); vals.update(saved.get(sc, {}))
        vals['speaker_mode'] = SPEAKER_MODE_ALIAS.get(vals['speaker_mode'], vals['speaker_mode'])
        if vals.get('fusion') == '仅游戏震动' and 'trigger_mode' not in saved.get(sc, {}):
            vals['trigger_mode'] = '纯用游戏'                 # that old choice also left the triggers to the game
        vals['fusion'] = FUSION_ALIAS.get(vals.get('fusion'), vals.get('fusion', '叠加'))
        if vals['speaker_mode'] not in SPEAKER_MODES: vals['speaker_mode'] = '全部游戏声'
        if vals.get('speaker_eq') not in SPEAKER_EQS: vals['speaker_eq'] = '原味'
        SV[sc] = make_vars(vals)

    def SC(key):
        return SV[V['scene'].get()][key].get()

    def watch_vars():
        for k, v in V.items():
            v.trace_add('write', lambda *a: apply_live())
        for sc in SCENES:
            for v in SV[sc].values():
                v.trace_add('write', lambda *a: apply_live())

    demo_mode = runner_cls.__name__ == 'DemoRunner'
    pad = {'state': 'usb' if demo_mode else None, 'known': demo_mode, 'demo': demo_mode}

    def running():
        return bool(runner['t'] and runner['t'].is_alive())

    def collect():
        for k, v in V.items():
            cfg[k] = v.get() / 100 if k in PCT else v.get()
        cfg['scenes'] = {sc: {k: (v.get() / 100 if k in PCT else v.get()) for k, v in SV[sc].items()} for sc in SCENES}
        sc = V['scene'].get()
        cfg.update(cfg['scenes'][sc]); cfg['scene'] = sc; cfg['haptic_style'] = STYLE_OF[sc]   # the active scene drives the run
        save_cfg(cfg)

    NEEDS = {'原生透传': ('usbip', 'hidhide'), '接管': ('hidhide', 'vigem'), 'Xbox': ('hidhide', 'vigem'), '普通': ()}

    def start():
        if running(): return
        hide_error()
        if not pad['demo'] and pad['state'] in (None, 'bluetooth') and pad['known']:
            show_error(next_step(False, pad['state'], [])[0]); return
        try:
            import deps
            miss = [m for m in deps.missing() if m[0] in NEEDS.get(SC('mode'), ())]
        except Exception:
            miss = []
        if miss:
            msgs.put('「' + SC('mode') + '」需要先安装：' + '、'.join(m[1] for m in miss) + '。正在打开依赖安装…')
            install_deps(); return
        if V['pbz_fx'].get():
            try:
                import sysfix
                st = sysfix.pbz_status()
                if st['game'] and (not st['ue4ss'] or st['mod'] != 'ok'):
                    msgs.put('提示：影之刃零的事件特调缺少 UE4SS 或事件 mod（游戏适配 → 影之刃零的齿轮里有安装说明）。其他功能照常启动。')
            except Exception:
                pass
        collect()
        r = runner_cls(cfg, msgs.put); runner['r'] = r
        runner['t'] = threading.Thread(target=r.run, daemon=True); runner['t'].start()

    def stop():
        if runner['r']: runner['r'].stop_ev.set()

    def toggle():
        stop() if running() else start()

    # settings that the running engine picks up live (within 0.5 s) vs the ones that need a restart
    RESTART_KEYS = {'mode': '连接方式', 'capture': '声音来源', 'pbz_fx': '影之刃零事件特调', 'ww_fx': '鸣潮事件特调',
                    'ww_vision': '鸣潮技能识别', 'record_native': '录制调校数据', 'foreground_exe': '指定前台游戏',
                    'pbz_log_dir': '事件日志目录'}
    live = {'job': None}

    def restart():
        if not running():
            start(); return
        msgs.put('正在按新设置重启…'); stop()

        def again(n=0):
            if running() and n < 60:
                root.after(100, lambda: again(n + 1))
            else:
                start()
        again()

    def apply_live(*_a):
        """Any setting changed: save it and hand it to the running engine (debounced)."""
        if live['job']:
            root.after_cancel(live['job'])

        def go():
            live['job'] = None
            collect()
            if running():
                r = runner['r']; r.live_ver += 1
                pend = [name for k, name in RESTART_KEYS.items() if r.start_cfg.get(k) != cfg.get(k)]
                show_banner(pend)
            else:
                show_banner([])
        live['job'] = root.after(300, go)

    def switch_scene(sc):
        """Scene card clicked: switch live - haptics, speaker and style change at once, no restart."""
        V['scene'].set(sc)

    def test():
        if running():
            msgs.put('请先停止再测试握把触感。'); return
        threading.Thread(target=lambda: msgs.put(test_fn()), daemon=True).start()

    # ---- header
    head = tk.Canvas(root, height=px(78), bg=BG, highlightthickness=0, bd=0); head.pack(fill='x')
    bigbtn = NeonButton(root, '▶  启动', toggle, w=170, h=46, color=CYAN, bg=BG, size=13, filled=True)
    scene_sw = Segmented(root, V['scene'], SCENES, cell=72, h=34, bg=BG)       # always one click away, on every page
    hstate = {'w': 0}

    def draw_head(e=None):
        head.delete('all'); w = head.winfo_width(); hstate['w'] = w
        cx, cy, r = px(44), px(39), px(22)
        for rr, col in ((r + px(6), mix(BG, CYAN, 0.25)), (r, CYAN)):
            pts = []
            for j in range(6):
                a = math.radians(60 * j); pts += [cx + rr * math.cos(a), cy + rr * math.sin(a)]
            head.create_polygon(pts, fill='' if rr > r else mix(BG, CYAN, 0.12), outline=col, width=px(2) if rr == r else 1)
        head.create_line(cx - px(10), cy + px(6), cx - px(4), cy - px(8), cx + px(2), cy + px(4), cx + px(8), cy - px(4), cx + px(12), cy + px(2), fill=CYAN, width=px(2))
        tt = head.create_text(px(80), px(28), text='声音触感', anchor='w', fill=TEXT, font=(ZH, 18, 'bold'))
        head.create_text(max(px(200), head.bbox(tt)[2] + px(18)), px(31), text='HAPTIC  LINK', anchor='w', fill=CYAN, font=(NUMB, 13))
        head.create_text(px(81), px(56), text=f'V{VERSION}  ·  USB / DS5Dongle（测试版）', anchor='w', fill=DIM, font=(ZH, 9))
        head.create_line(0, px(77), w, px(77), fill=LINE)
        head.create_line(0, px(77), px(240), px(77), fill=CYAN, width=px(2))
        for i in range(24):
            x = px(250) + i * px(9)
            head.create_line(x, px(77), x + px(4), px(77), fill=mix(BG, CYAN, 0.6 - i * 0.025), width=px(2))
        head.create_text(w - px(208), px(29), text='', anchor='e', fill=FAINT, font=(ZH, 9), tags='pad')
        head.create_oval(0, 0, 0, 0, tags='dot', outline='')
        head.create_text(w - px(208), px(48), text='', anchor='e', font=(ZH, 11, 'bold'), tags='stt')
        head.create_window(w - px(20), px(39), window=bigbtn, anchor='e')
        head.create_window(w - px(400), px(39), window=scene_sw, anchor='e')
    head.bind('<Configure>', draw_head)

    # ---- restart-needed banner (no pop-up): shown while some changed settings wait for a restart
    banner = tk.Frame(root, bg=mix(BG, AMBER, 0.18), highlightthickness=1, highlightbackground=AMBER)
    banner_lb = tk.Label(banner, text='', bg=mix(BG, AMBER, 0.18), fg=AMBER, font=(ZH, 10, 'bold'), anchor='w')
    banner_lb.pack(side='left', padx=px(14), pady=px(6), fill='x', expand=True)
    NeonButton(banner, '立即重启应用', restart, w=140, h=30, size=10, color=AMBER, bg=mix(BG, AMBER, 0.18), filled=True).pack(side='right', padx=px(10), pady=px(4))

    def show_banner(pend):
        if pend:
            banner_lb.configure(text='⚠  ' + '、'.join(pend) + ' 已修改：要重启后才生效（其他设置已实时生效）')
            if not banner.winfo_ismapped():
                banner.pack(fill='x', padx=px(14), pady=(px(8), 0), after=head)
        elif banner.winfo_ismapped():
            banner.pack_forget()

    # ---- error bar: a failure must not hide in the log; it says what happened and offers the fix
    EB = mix(BG, MAG, 0.16)
    errbar = tk.Frame(root, bg=EB, highlightthickness=1, highlightbackground=MAG)
    err_lb = tk.Label(errbar, text='', bg=EB, fg=TEXT, font=(ZH, 10, 'bold'), anchor='w', justify='left')
    err_lb.pack(side='left', padx=px(14), pady=px(6), fill='x', expand=True)

    def hide_error():
        if errbar.winfo_ismapped():
            errbar.pack_forget()

    def show_error(text):
        err_lb.configure(text=text, wraplength=max(px(300), root.winfo_width() - px(520)))
        if not errbar.winfo_ismapped():
            errbar.pack(fill='x', padx=px(14), pady=(px(8), 0), after=head)
    NeonButton(errbar, '关闭', hide_error, w=70, h=30, size=10, color=MAG, bg=EB).pack(side='right', padx=(0, px(8)), pady=px(4))
    NeonButton(errbar, '查看日志', lambda: show('日志'), w=100, h=30, size=10, color=MAG, bg=EB).pack(side='right', padx=(0, px(8)), pady=px(4))
    NeonButton(errbar, '反馈问题', lambda: run_selfcheck(open_issue=True), w=100, h=30, size=10, color=MAG, bg=EB).pack(side='right', padx=(0, px(8)), pady=px(4))
    NeonButton(errbar, '修复手柄', lambda: repair_pad_ui(), w=100, h=30, size=10, color=MAG, bg=EB, filled=True).pack(side='right', padx=(0, px(8)), pady=px(4))

    # ---- update banner (no pop-up): a newer release on GitHub -> online update or manual download
    UB = mix(BG, GREEN, 0.14)
    upd = {'info': None, 'busy': False}
    ubar = tk.Frame(root, bg=UB, highlightthickness=1, highlightbackground=GREEN)
    ubar_lb = tk.Label(ubar, text='', bg=UB, fg=GREEN, font=(ZH, 10, 'bold'), anchor='w')
    ubar_lb.pack(side='left', padx=px(14), pady=px(6), fill='x', expand=True)

    def upd_skip():
        if upd['info']:
            cfg['skip_version'] = upd['info']['version']; save_cfg(cfg)
        ubar.pack_forget()

    def upd_manual():
        import webbrowser
        webbrowser.open((upd['info'] or {}).get('page') or about.RELEASES_URL)

    def upd_notes():
        import webbrowser, updater
        webbrowser.open(updater.notes_url((upd['info'] or {}).get('page'), i18n.LANG))

    def upd_online():
        if upd['busy'] or not upd['info']:
            return
        info = upd['info']
        import sys as _s
        if not getattr(_s, 'frozen', False) or not info.get('url'):
            ubar_lb.configure(text='这个版本不能在线更新（源码运行或没有更新包），请点「GitHub 下载」手动更新'); return
        upd['busy'] = True

        def work():
            import tempfile, updater
            try:
                dst = pathlib.Path(tempfile.gettempdir()) / (info.get('name') or 'PS5Haptics-update.zip')
                def prog(done, total):
                    pct = f'{done * 100 // total}%' if total else f'{done >> 20} MB'
                    root.after(0, lambda: ubar_lb.configure(text=f'正在下载 v{info["version"]} … {pct}'))
                updater.download(info['url'], dst, prog)
                root.after(0, lambda: ubar_lb.configure(text='下载完成，正在准备更新…'))
                script = updater.prepare(dst)
                root.after(0, lambda: finish(script))
            except Exception as exc:
                err = str(exc)
                def fail():
                    upd['busy'] = False
                    ubar_lb.configure(text=f'在线更新失败（{err[:60]}）：请点「GitHub 下载」手动更新', fg=AMBER)
                root.after(0, fail)
        threading.Thread(target=work, daemon=True).start()

    def finish(script):
        import updater
        msgs.put('更新已就绪：软件将关闭并自动更新，完成后会重新打开。')
        updater.run_script(script)
        root.after(300, close)

    for txt, fn, filled in (('忽略此版本', upd_skip, False), ('GitHub 下载', upd_manual, False), ('在线更新', upd_online, True), ('更新日志', upd_notes, False)):
        NeonButton(ubar, txt, fn, w=118 if filled else 112, h=30, size=10, color=GREEN, bg=UB, filled=filled).pack(side='right', padx=(0, px(8)), pady=px(4))

    def show_update(info, manual=False):
        if not info:
            if manual:
                msgs.put(f'已经是最新版本（v{about.VERSION}）。')
            return
        if not manual and cfg.get('skip_version') == info['version']:
            return
        upd['info'] = info
        first = next((l.strip(' #*-') for l in info['notes'].splitlines() if l.strip(' #*-')), '')
        ubar_lb.configure(text=f'发现新版本 v{info["version"]}（当前 v{about.VERSION}）' + (f'：{first[:50]}' if first else ''), fg=GREEN)
        if not ubar.winfo_ismapped():
            ubar.pack(fill='x', padx=px(14), pady=(px(8), 0), after=head)
        msgs.put(f'发现新版本 v{info["version"]}，可以在顶部点「在线更新」，或到 GitHub 手动下载。')

    def check_update(manual=False):
        def work():
            import updater
            try:
                info = updater.check()
            except Exception as exc:
                if manual:
                    msgs.put(f'检查更新失败（{str(exc)[:60]}），可以到 GitHub 主页查看：{about.RELEASES_URL}')
                return
            root.after(0, lambda: show_update(info, manual))
        threading.Thread(target=work, daemon=True).start()
    root.after(4000, check_update)                 # once per launch, in the background

    # ---- body: sidebar + pages
    main = tk.Frame(root, bg=BG); main.pack(fill='both', expand=True, padx=px(14), pady=(px(10), 0))
    side = tk.Canvas(main, width=px(160), bg=BG, highlightthickness=0, bd=0); side.pack(side='left', fill='y')
    pages_host = tk.Frame(main, bg=BG); pages_host.pack(side='left', fill='both', expand=True, padx=(px(12), 0))
    NAV = [('总览', 'DASHBOARD', '◈'), ('设置', 'SETTINGS', '⚙'), ('游戏适配', 'GAMES', '⬢'), ('系统', 'SYSTEM', '✚'), ('日志', 'LOG', '≡')]
    cur = {'p': '总览', 'hover': None}; pages = {}

    def draw_side(e=None):
        side.delete('all'); h = side.winfo_height()
        for i, (n, en, ic) in enumerate(NAV):
            y = i * px(62); sel = cur['p'] == n; hov = cur['hover'] == i
            if sel or hov:
                side.create_polygon(chamfer(0, y + px(2), px(158), y + px(56), px(10)), fill=mix(BG, CYAN, 0.14 if sel else 0.05), outline=CYAN if sel else LINE2)
            if sel:
                side.create_rectangle(0, y + px(12), px(3), y + px(46), fill=CYAN, outline='')
            side.create_text(px(24), y + px(29), text=ic, fill=CYAN if sel else FAINT, font=('Segoe UI Symbol', 14))
            side.create_text(px(50), y + px(21), text=n, anchor='w', fill=TEXT if sel else DIM, font=(ZH, 11, 'bold'))
            side.create_text(px(50), y + px(40), text=en, anchor='w', fill=CYAN if sel else FAINT, font=(NUM, 7))
        y = h - px(92)
        # status that matters while playing, then author + project links: quiet, but always there
        side.create_line(px(4), y, px(150), y, fill=LINE)
        side.create_text(px(6), y + px(18), text=cur.get('up_t', ''), anchor='w', fill=DIM, font=(ZH, 8), tags='up')
        side.create_text(px(6), y + px(38), text=cur.get('bat_t', ''), anchor='w', fill=cur.get('bat_c', DIM), font=(ZH, 8), tags='battery')
        side.create_text(px(6), y + px(58), text='作者  ' + about.AUTHOR, anchor='w', fill=FAINT, font=(ZH, 8))
        hv = cur.get('link')
        gx = side.create_text(px(6), y + px(78), text='GitHub', anchor='w', fill=CYAN if hv == 'gh' else DIM, font=(NUM, 8), tags='lnk_gh')
        bx = side.bbox(gx)
        side.create_text(bx[2] + px(6), y + px(78), text='·', anchor='w', fill=FAINT, font=(NUM, 8))
        side.create_text(bx[2] + px(16), y + px(78), text='哔哩哔哩', anchor='w', fill=CYAN if hv == 'bili' else DIM, font=(ZH, 8), tags='lnk_bili')
    side.bind('<Configure>', draw_side)

    def open_link(url):
        import webbrowser
        webbrowser.open(url)
    def set_language(code):
        """Save the language and restart the app (the whole UI is built in that language)."""
        if code == i18n.LANG:
            return
        cfg['lang'] = code; save_cfg(cfg)
        msgs.put('切换语言后软件会自动重启。')

        def relaunch():
            import subprocess, sys as _s
            stop()
            if runner['t']:
                runner['t'].join(8)                  # takeover: give the real pad back first
            cmd = [_s.executable] + _s.argv[1:] if getattr(_s, 'frozen', False) else [_s.executable, os.path.abspath(_s.argv[0])] + _s.argv[1:]
            subprocess.Popen(cmd, cwd=os.getcwd())
            root.destroy()
        root.after(300, relaunch)
    for tag, key, url in (('lnk_gh', 'gh', about.GITHUB_URL), ('lnk_bili', 'bili', about.BILIBILI_URL)):
        side.tag_bind(tag, '<Button-1>', lambda e, u=url: open_link(u))
        # recolour only (a full redraw would recreate the item under the pointer -> Enter/Leave loop)
        side.tag_bind(tag, '<Enter>', lambda e, k=key, t=tag: (cur.update(link=k), side.configure(cursor='hand2'), side.itemconfigure(t, fill=CYAN)))
        side.tag_bind(tag, '<Leave>', lambda e, t=tag: (cur.update(link=None), side.configure(cursor=''), side.itemconfigure(t, fill=DIM)))
    side.bind('<Motion>', lambda e: (cur.update(hover=e.y // px(62) if e.y < len(NAV) * px(62) else None), draw_side()))
    side.bind('<Leave>', lambda e: (cur.update(hover=None), draw_side()))

    def show(name):
        name = {'适配库': '游戏适配'}.get(name, name)
        cur['p'] = name
        for n, f in pages.items():
            f.pack_forget()
        pages[name].pack(fill='both', expand=True); draw_side()
    side.bind('<Button-1>', lambda e: show(NAV[e.y // px(62)][0]) if e.y < len(NAV) * px(62) else None)
    root._show = show                       # page switch hook (screenshots / scripted demos)
    root._start = start; root._switch_scene = switch_scene
    root._hooks = {}                        # (scripted checks)
    root._pages = [n for n, _e, _i in NAV]

    def page(name, scroll=False):
        if not scroll:
            f = tk.Frame(pages_host, bg=BG); pages[name] = f; return f
        outer = tk.Frame(pages_host, bg=BG); pages[name] = outer
        cv = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0)
        bar = tk.Canvas(outer, width=px(5), bg=BG, highlightthickness=0, bd=0)
        bar.pack(side='right', fill='y', padx=(px(4), 0)); cv.pack(side='left', fill='both', expand=True)
        inner = tk.Frame(cv, bg=BG); win = cv.create_window(0, 0, window=inner, anchor='nw')

        def draw_bar():
            bar.delete('all'); h = bar.winfo_height(); f0, f1 = cv.yview()
            if f1 - f0 < 0.999:
                bar.create_rectangle(0, h * f0, px(5), h * f1, fill=LINE2, outline='')

        def sync(e=None):
            cv.itemconfigure(win, width=cv.winfo_width())
            ch, vh = inner.winfo_reqheight(), max(1, cv.winfo_height())
            cv.configure(scrollregion=(0, 0, cv.winfo_width(), ch))
            # content got shorter / window got taller: never leave the view scrolled past the content
            if ch <= vh:
                cv.yview_moveto(0)
            elif cv.canvasy(0) + vh > ch:
                cv.yview_moveto(max(0.0, (ch - vh) / ch))
            draw_bar()

        def wheel(e):
            if inner.winfo_reqheight() > cv.winfo_height():
                cv.yview_scroll(int(-e.delta / 120) * 3, 'units'); draw_bar()
        inner.bind('<Configure>', sync); cv.bind('<Configure>', sync)
        outer.bind('<Enter>', lambda e: root.bind_all('<MouseWheel>', wheel))
        outer.bind('<Leave>', lambda e: root.unbind_all('<MouseWheel>'))
        return inner

    def lock_note(parent):
        lb = tk.Label(parent, text='', bg=BG, fg=AMBER, font=(ZH, 9), anchor='w'); lb.pack(fill='x', pady=(0, px(6)))
        locks.append(lb); return lb
    locks = []

    # ---- 总览
    pg = page('总览')
    NB = mix(BG, CYAN, 0.08)
    nextbar = tk.Frame(pg, bg=NB, highlightthickness=1, highlightbackground=LINE2)
    next_lb = tk.Label(nextbar, text='', bg=NB, fg=TEXT, font=(ZH, 10), anchor='w', justify='left')
    next_lb.pack(side='left', padx=px(14), pady=px(8), fill='x', expand=True)
    next_btn = {'act': None}
    next_buttons = {'deps': NeonButton(nextbar, '一键安装依赖', lambda: install_deps(), w=130, h=30, size=10, color=AMBER, bg=NB),
                    'repair': NeonButton(nextbar, '修复手柄', lambda: repair_pad_ui(), w=110, h=30, size=10, color=AMBER, bg=NB)}
    row1 = tk.Frame(pg, bg=BG); row1.pack(fill='both', expand=True)

    def update_next(run):
        need = NEEDS.get(SC('mode'), ())
        step = next_step(run, pad['state'], [m[1] for m in sysstate['missing'] if m[0] in need]) if (pad['known'] or run) else None
        if not step or errbar.winfo_ismapped():
            if nextbar.winfo_ismapped():
                nextbar.pack_forget()
            return
        next_lb.configure(text=step[0], wraplength=max(px(300), nextbar.winfo_width() - px(170)))
        if next_btn['act'] != step[1]:
            for b in next_buttons.values():
                b.pack_forget()
            if step[1] in next_buttons:                    # 'start' already has the big button at the top right
                next_buttons[step[1]].pack(side='right', padx=px(10), pady=px(4))
            next_btn['act'] = step[1]
        if not nextbar.winfo_ismapped():
            nextbar.pack(fill='x', pady=(0, px(10)), before=row1)
    p_grip = Panel(row1, '握把音圈', 'GRIP COILS'); p_grip.pack(side='left', fill='y')
    gm = GripMeter(p_grip.body); gm.pack(padx=px(6), pady=px(4), expand=True)
    p_scope = Panel(row1, '触感波形', 'WAVEFORM'); p_scope.pack(side='left', fill='both', expand=True, padx=px(10))
    scope = Scope(p_scope.body); scope.pack(fill='both', expand=True, pady=px(4))
    sb = tk.Frame(p_scope.body, bg=PANEL); sb.pack(fill='x')
    test_btn = NeonButton(sb, '测试握把触感', test, w=140, h=32, size=10); test_btn.pack(side='left')
    lvl_lb = tk.Label(sb, text='', bg=PANEL, fg=DIM, font=(MONO, 9)); lvl_lb.pack(side='right')
    p_trig = Panel(row1, '自适应扳机', 'TRIGGERS', accent=MAG); p_trig.pack(side='left', fill='y')
    tg_row = tk.Frame(p_trig.body, bg=PANEL); tg_row.pack(pady=px(4), expand=True)
    gl = TriggerGauge(tg_row, 'L'); gl.pack(side='left'); gr = TriggerGauge(tg_row, 'R'); gr.pack(side='left')
    tk.Label(p_trig.body, text='外圈 = 按压深度　内圈 = 扳机震动', bg=PANEL, fg=FAINT, font=(ZH, 8)).pack()

    row2 = tk.Frame(pg, bg=BG); row2.pack(fill='x', pady=(px(10), px(10)))
    p_ev = Panel(row2, '事件特调', 'EVENT FX', accent=AMBER); p_ev.pack(side='right', fill='both', padx=(px(10), 0))
    p_info = Panel(row2, '链路状态', 'LINK'); p_info.pack(side='left', fill='both', expand=True)
    info = {k: InfoRow(p_info.body, k) for k in ('工作方式', '声源', '虚拟手柄', '扬声器', '场景', '游戏专属', '前台', '丢块')}
    for w_ in info.values(): w_.pack(fill='x', pady=px(3))
    p_ww = Panel(row2, '鸣潮技能识别', 'WW VISION', accent=GREEN)
    hexes = SkillHex(p_ww.body); hexes.pack(pady=px(6))
    ev_box = tk.Text(p_ev.body, bg=PANEL, fg=TEXT, font=(ZH, 9), relief='flat', height=7, width=26, bd=0, highlightthickness=0, state='disabled', cursor='arrow')
    ev_box.configure(width=24); ev_box.pack(fill='both', expand=True); ev_box.tag_configure('t', foreground=FAINT); ev_box.tag_configure('n', foreground=AMBER)
    ev_state = {'last': None}
    ev_box.configure(state='normal'); ev_box.insert('1.0', tr('影之刃零、鸣潮里触发的事件会显示在这里。'), 't'); ev_box.configure(state='disabled')

    # ---- 游戏: 鸣潮 / 影之刃零 / 通用 / 沉浸模式 / 系统检查
    gp = page('游戏适配', scroll=True); lock_note(gp)
    grid_view = tk.Frame(gp, bg=BG); grid_view.pack(fill='x')
    details = {}

    def detail(key, title, accent):
        f = tk.Frame(gp, bg=BG); details[key] = f
        hd = tk.Frame(f, bg=BG); hd.pack(fill='x', pady=(0, px(8)))
        NeonButton(hd, '‹  返回游戏适配', show_grid, w=150, h=34, size=10, bg=BG).pack(side='left')
        tk.Label(hd, text=title + '  ·  设置', bg=BG, fg=accent, font=(ZH, 13, 'bold')).pack(side='left', padx=px(14))
        return f

    def show_grid():
        for f in details.values(): f.pack_forget()
        grid_view.pack(fill='x')

    def show_detail(key):
        grid_view.pack_forget()
        for f in details.values(): f.pack_forget()
        details[key].pack(fill='x')

    def open_detail(key):                   # (scripted screenshots)
        if key in SCENES:
            V['scene'].set(key); show('设置')
        else:
            show('游戏适配'); show_detail(key)
    root._detail = open_detail; root._detail_keys = list(SCENES) + ['鸣潮', '影之刃零']

    pg = None

    def module(title, sub, accent, tag, key=None):
        pnl = Panel(detail(key or title, title, accent), title, sub, accent=accent); pnl.pack(fill='x', pady=(0, px(10)))
        tk.Label(pnl.body, text=tag, bg=PANEL, fg=FAINT, font=(ZH, 9), anchor='w', justify='left',
                 wraplength=px(900)).pack(fill='x', pady=(0, px(6)))
        return pnl

    p = module('鸣潮', 'WUTHERING WAVES', GREEN, '鸣潮（Client-Win64-Shipping.exe）在前台时自动生效。读屏幕识别技能状态，不注入、不修改游戏。')
    Toggle(p.body, '鸣潮专属调校', V['game_profiles'], '在前台时自动切换：原生震动降到 60%、握把风格改为细节丰富', color=GREEN, wrap=820).pack(fill='x', pady=px(4))
    Toggle(p.body, '技能识别（读屏 + OCR）', V['ww_vision'], '识别手柄界面 L2 / R2 技能按钮的冷却数字：大招就绪时 R2 变成「咔哒」武器扳机，声骸就绪时 L2 有阻力', color=GREEN, wrap=820).pack(fill='x', pady=px(4))
    Toggle(p.body, '扳机按到咔哒点才释放', V['ww_click_gate'], '技能就绪时，扳机要按过咔哒点游戏才收到按键，不会误放技能（仅原生透传）', color=GREEN, wrap=820).pack(fill='x', pady=px(4))
    Toggle(p.body, '鸣潮事件特调', V['ww_fx'], '大招、声骸释放和共鸣回路特殊技时：握把专属震动（扬声器不做事件适配，按全部游戏声播放）', color=GREEN, wrap=820).pack(fill='x', pady=px(4))

    p = module('影之刃零', 'PHANTOM BLADE ZERO', MAG, '需要另装事件记录 mod（PS5EventLog，发布包「影之刃零事件mod」文件夹）。mod 只记录事件，不改变游戏行为。')
    Toggle(p.body, '影之刃零事件特调', V['pbz_fx'], '格挡 / 精准格挡 / 完美格挡 / 完美闪避 / 受击 / 破防 / 处决分别给不同的握把、扳机和扬声器反馈', color=MAG, wrap=820).pack(fill='x', pady=px(4))
    fr = tk.Frame(p.body, bg=PANEL); fr.pack(fill='x', pady=(px(8), px(2)))
    tk.Label(fr, text='事件日志目录', bg=PANEL, fg=TEXT, font=(ZH, 10)).pack(side='left')
    tk.Entry(fr, textvariable=V['pbz_log_dir'], width=50, bg=PANEL2, fg=CYAN, insertbackground=CYAN, relief='flat', font=(MONO, 9),
             highlightthickness=1, highlightbackground=LINE2, highlightcolor=CYAN).pack(side='left', padx=px(12), ipady=px(4))
    pbz_lb = tk.Label(p.body, text='', bg=PANEL, fg=FAINT, font=(ZH, 9), anchor='w')

    def pbz_auto(quiet=False):
        try:
            from sysfix import find_pbz_log_dir
            d = find_pbz_log_dir()
        except Exception:
            d = None
        if d:
            if not quiet:
                V['pbz_log_dir'].set(str(d)); msgs.put('找到影之刃零事件日志目录：' + str(d))
            pbz_lb.configure(text='✓ 已找到 mod：' + str(d) + ('（留空时每次启动自动使用它）' if not V['pbz_log_dir'].get() else ''), fg=GREEN)
        else:
            pbz_lb.configure(text='✗ 没有在 Steam 库里找到 PS5EventLog mod：没装影之刃零或没装 mod 时可以忽略', fg=FAINT)
            if not quiet:
                msgs.put('没有在 Steam 库里找到影之刃零的 PS5EventLog mod，请先安装 mod 或点「浏览」手动选择。')

    def pbz_browse():
        from tkinter import filedialog
        d = filedialog.askdirectory(title=tr('选择 PS5EventLog 的 logs 文件夹'))
        if d: V['pbz_log_dir'].set(d)
    NeonButton(fr, '自动查找', pbz_auto, w=96, h=30, size=9).pack(side='left')
    NeonButton(fr, '浏览…', pbz_browse, w=80, h=30, size=9).pack(side='left', padx=px(6))
    pbz_lb.pack(fill='x', pady=(px(4), 0))
    req = Panel(details['影之刃零'], '运行条件', 'REQUIREMENTS', accent=MAG); req.pack(fill='x', pady=(0, px(10)))
    req_lb = tk.Label(req.body, text='检测中…', bg=PANEL, fg=TEXT, font=(ZH, 10), justify='left', anchor='w', wraplength=px(900))
    req_lb.pack(fill='x')
    tk.Label(req.body, justify='left', anchor='w', bg=PANEL, fg=DIM, font=(ZH, 9), wraplength=px(900), text=(
        '事件特调需要两样东西：\n'
        '① UE4SS（社区的虚幻引擎 mod 加载器）：点「打开 UE4SS 下载页」，在 Nexus 下载影之刃零版本，按页面说明解压到\n'
        '　 游戏目录\\platform\\steam\\（和 PBZ.exe 同一层，解压后应有 ue4ss 文件夹和 dwmapi.dll）。\n'
        '② 本软件的事件 mod PS5EventLog：装好 UE4SS 后点「一键安装事件 mod」。它只记录格挡、受击等事件，不改变游戏行为。\n'
        '没有这两样也能玩：原生透传（游戏原生扳机和音圈）和声音触感照常工作，只是没有按事件定制的反馈。'
    )).pack(fill='x', pady=(px(6), px(8)))
    fr = tk.Frame(req.body, bg=PANEL); fr.pack(fill='x')

    def pbz_check():
        def work():
            try:
                import sysfix
                st = sysfix.pbz_status()
            except Exception as exc:
                st = {'error': str(exc)}
            root.after(0, lambda: show_pbz(st))
        threading.Thread(target=work, daemon=True).start()

    def show_pbz(st):
        if st.get('error'):
            req_lb.configure(text='检测失败：' + st['error'], fg=AMBER); return
        if not st['game']:
            req_lb.configure(text='✗ 没有在 Steam 库里找到影之刃零（没装这个游戏可以忽略）', fg=FAINT); return
        lines = ['✓ 游戏：' + st['game'],
                 ('✓ UE4SS 已安装' if st['ue4ss'] else '✗ 没有安装 UE4SS：请先按下面说明安装'),
                 {'ok': '✓ 事件 mod 已安装（最新）', 'old': '! 事件 mod 版本较旧：点「一键安装事件 mod」更新',
                  'disabled': '! 事件 mod 被停用（缺 enabled.txt）：点「一键安装事件 mod」重新启用',
                  None: '✗ 没有安装事件 mod'}[st['mod']]]
        ok = st['ue4ss'] and st['mod'] == 'ok'
        req_lb.configure(text='\n'.join(lines), fg=GREEN if ok else AMBER)

    def pbz_install():
        import sysfix
        ok, msg = sysfix.install_pbz_mod(); msgs.put(msg); pbz_check()

    def open_nexus():
        import webbrowser, sysfix
        webbrowser.open(sysfix.PBZ_NEXUS_UE4SS)
    NeonButton(fr, '打开 UE4SS 下载页', open_nexus, w=170, h=32, size=10, color=MAG).pack(side='left')
    NeonButton(fr, '一键安装事件 mod', pbz_install, w=160, h=32, size=10, color=MAG).pack(side='left', padx=px(8))
    NeonButton(fr, '重新检测', pbz_check, w=100, h=32, size=10).pack(side='left')
    root.after(1500, pbz_check)
    root.after(1200, lambda: threading.Thread(target=lambda: pbz_auto(quiet=True), daemon=True).start())

    SCENE_INFO = {
        '游戏': (CYAN, 'impact', '玩游戏：只把打击、碰撞、爆炸变成触感；支持 PS5 手柄的游戏保留原生扳机和音圈；鸣潮、影之刃零有专属适配。'),
        '影院': (AMBER, 'cinema', '看电影 / 剧集：低频隆隆、爆炸厚重；扬声器播放全部声音。不需要虚拟手柄，自动截播放器或浏览器的声音。'),
        '音乐': (MAG, 'rhythm', '听音乐 / 音游：握把和扳机跟着鼓点律动；扬声器可播放全部声音。也适合给没有手柄震动的游戏「加氛围」。'),
    }
    ROUTING = [('原生透传', '原生透传', '', '游戏看到虚拟PS5手柄：保留原生自适应扳机和音圈触感，再叠加声音触感。适合支持PS5手柄的游戏。'),
               ('接管', '接管', '', '游戏看到虚拟PS4手柄：游戏震动与声音触感融合。原生透传不可用时的备用。'),
               ('Xbox', '虚拟 Xbox', '', '游戏看到虚拟Xbox手柄：游戏震动与声音触感融合。给只认Xbox手柄的游戏用（如鬼武者），建议关掉该游戏的Steam输入。'),
               ('普通', '普通', '', '不创建虚拟手柄，只叠加声音触感和扬声器。看电影、听音乐用这个。')]

    def calibrate_gyro():
        r = runner['r']; flt = getattr(r, 'pad_filter', None) if running() else None
        if flt is None:
            msgs.put('陀螺仪校准要在使用虚拟手柄时进行：先选原生透传、接管或虚拟 Xbox，再点「启动」。'); return
        msgs.put('正在校准：请把手柄平放别动，约 2 秒…'); flt.calibrate()

        def done(n=0):
            if flt.cal_result is None and n < 40:
                root.after(100, lambda: done(n + 1)); return
            state, bias = flt.cal_result or ('no_data', None)
            if state == 'ok':
                cfg['gyro_bias'] = list(bias); save_cfg(cfg); r.live_ver += 1
                msgs.put(f'陀螺仪校准完成，零偏 {bias[0]} / {bias[1]} / {bias[2]}。')
            elif state == 'moved':
                msgs.put('校准失败：手柄在动，请平放后再试。')
            else:
                msgs.put('校准失败：没有收到足够的手柄数据。')
        root.after(2100, done)

    def reset_caps():
        r = runner['r']
        if running() and getattr(r, 'caps', None) is not None:
            r.caps.reset()
        else:
            from game_caps import GameCaps
            GameCaps().reset()
        msgs.put('已清除让位记录，游戏用到时会重新记录。')

    def pad_panel(f):
        pnl = Panel(f, '手柄输入', 'INPUT', accent=CYAN); pnl.pack(fill='x', pady=(0, px(10)))
        Slider(pnl.body, '左摇杆死区', V['stick_dz_l'], 0, 30).pack(fill='x', pady=px(2))
        Slider(pnl.body, '右摇杆死区', V['stick_dz_r'], 0, 30).pack(fill='x', pady=px(2))
        tk.Label(pnl.body, text='摇杆漂移时调大一点。死区和陀螺仪校准只对虚拟手柄生效（原生透传、接管、虚拟 Xbox）。',
                 bg=PANEL, fg=DIM, font=(ZH, 9), anchor='w', justify='left', wraplength=px(900)).pack(fill='x', pady=(px(2), px(8)))
        NeonButton(pnl.body, '校准陀螺仪', calibrate_gyro, w=150, h=36, size=10).pack(anchor='w', pady=(0, px(8)))
        Toggle(pnl.body, '左右握把对调', V['swap_grips'], '觉得左右触感反了时打开', wrap=820).pack(fill='x', pady=px(3))
        Toggle(pnl.body, '触感不随系统音量变化', V['volume_comp'], '调低系统音量时补偿系统混音的触感（实验性，按进程截音不受音量影响）', wrap=820).pack(fill='x', pady=px(3))

    STEAM_XBOX = ('⚠ 虚拟 Xbox 一定要关掉这个游戏的 Steam 输入：Steam 库里右键游戏 → 属性 → 控制器 → 选「禁用 Steam 输入」。'
                  '不关的话，游戏可能同时收到两个手柄、按键乱跳，或者认不到手柄。')
    STEAM_OTHER = '用 Steam 启动的游戏如果按键乱跳、按一下触发两次：Steam 库里右键游戏 → 属性 → 控制器 → 选「禁用 Steam 输入」。'

    def steam_note(parent, sv):
        slot = tk.Frame(parent, bg=PANEL); slot.pack(fill='x')
        BOX = mix(PANEL, AMBER, 0.14)
        box = tk.Frame(slot, bg=BOX, highlightthickness=1, highlightbackground=AMBER)
        lb = tk.Label(box, text='', bg=BOX, fg=AMBER, font=(ZH, 10, 'bold'), anchor='w', justify='left', wraplength=px(880))
        lb.pack(fill='x', padx=px(12), pady=px(8))

        def upd(*_a):
            m = sv['mode'].get()
            if m == '普通':
                box.pack_forget(); return
            strong = m == 'Xbox'
            lb.configure(text=STEAM_XBOX if strong else STEAM_OTHER, fg=AMBER if strong else TEXT, font=(ZH, 10, 'bold' if strong else 'normal'))
            box.configure(highlightbackground=AMBER if strong else LINE2)
            box.pack(fill='x', pady=(0, px(8)))
        sv['mode'].trace_add('write', upd); upd()

    def trigger_panel(f, sv):
        pnl = Panel(f, '扳机手感', 'TRIGGER FEEL', accent=MAG); pnl.pack(fill='x', pady=(0, px(10)))
        tk.Label(pnl.body, text='每个扳机先选一个手感预设，再用滑块微调。「声音震动」就是扳机跟着声音震（默认）。原生透传下游戏自己用扳机时，'
                 '按「连接方式」里扳机来源的设置，游戏的效果优先。', bg=PANEL, fg=DIM, font=(ZH, 9), anchor='w', justify='left',
                 wraplength=px(900)).pack(fill='x', pady=(0, px(6)))
        grid = tk.Frame(pnl.body, bg=PANEL); grid.pack(fill='x')
        labels = dict(start=('起点', '%', '扳机按到这里开始起作用'), strength=('强度', '', ''), end=('断开点', '%', '阻力在这里“咔”地断开'),
                      freq=('频率', 'Hz', ''))
        for i, (side, title) in enumerate((('L', 'L2  左扳机'), ('R', 'R2  右扳机'))):
            grid.columnconfigure(i, weight=1, uniform='trg')
            col = tk.Frame(grid, bg=PANEL); col.grid(row=0, column=i, sticky='new', padx=(0, px(16)) if i == 0 else 0)
            tk.Label(col, text=title, bg=PANEL, fg=MAG, font=(ZH, 10, 'bold'), anchor='w').pack(fill='x')
            pv = sv[TP.preset_key(side)]
            Segmented(col, pv, TP.NAMES, cell=92, cols=3 if i18n.LANG == 'zh' else 2, color=MAG, rec=TP.SOUND).pack(anchor='w', pady=(px(4), px(4)))
            sliders = {}
            for p in TP.PARAMS:
                lo, hi = TP.RANGES[p]; text, unit, hint = labels[p]
                sliders[p] = Slider(col, text, sv[TP.key(p, side)], lo, hi, unit=unit, color=MAG, hint=hint)
            note = tk.Label(col, text='跟着声音震动，强度在「触感」的扳机强度里调。', bg=PANEL, fg=FAINT, font=(ZH, 9), anchor='w')

            def layout(*_a, pv=pv, sliders=sliders, note=note):
                use = TP.uses(pv.get())
                for s in sliders.values():
                    s.pack_forget()
                note.pack_forget()
                for p in TP.PARAMS:
                    if p in use:
                        sliders[p].pack(fill='x', pady=px(1))
                if not use:
                    note.pack(fill='x', pady=px(6))

            def load(*_a, pv=pv, side=side):                  # a preset is a starting point: its numbers go into the sliders
                for p, v in TP.values_for(pv.get()).items():
                    sv[TP.key(p, side)].set(v)
            pv.trace_add('write', load); pv.trace_add('write', layout); layout()

        def copy_lr():
            sv[TP.preset_key('R')].set(sv[TP.preset_key('L')].get())
            for p in TP.PARAMS:
                sv[TP.key(p, 'R')].set(sv[TP.key(p, 'L')].get())
            msgs.put('右扳机已改成和左扳机一样的手感。')
        NeonButton(pnl.body, '右扳机用左边的设置', copy_lr, w=170, h=30, size=10, color=MAG).pack(anchor='w', pady=(px(6), 0))

    def settings_body(f, sv, sc, acc, game=None):
        pnl = Panel(f, '连接方式', 'ROUTING', accent=acc); pnl.pack(fill='x', pady=(0, px(10)))
        rec = '原生透传' if sc == '游戏' else '普通'
        ModeCards(pnl.body, sv['mode'], [(v, t, '推荐' if v == rec else '', d) for v, t, _g, d in ROUTING]).pack(fill='x', pady=(px(4), px(8)))
        steam_note(pnl.body, sv)
        if game is not None:
            tk.Label(pnl.body, text='连接方式和声音来源在点「启动」时决定：先打开这个游戏，再点「启动」，就会用这里的设置；其他设置在游戏切到前台时自动换上。',
                     bg=PANEL, fg=DIM, font=(ZH, 9), anchor='w', justify='left', wraplength=px(900)).pack(fill='x', pady=(0, px(8)))
        if sc == '游戏':
            for label, key, values, rec_v, col in (('握把', 'fusion', ('叠加', '替换', '纯用游戏'), '叠加', CYAN),
                                                   ('扳机', 'trigger_mode', ('自动', '叠加', '替换', '纯用游戏'), '自动', MAG)):
                fr = tk.Frame(pnl.body, bg=PANEL); fr.pack(fill='x', pady=(0, px(6)))
                tk.Label(fr, text=label, bg=PANEL, fg=TEXT, font=(ZH, 10), width=6 if i18n.LANG == 'zh' else 10, anchor='w').pack(side='left')
                Segmented(fr, sv[key], values, cell=124, color=col, rec=rec_v).pack(side='left')
            tk.Label(pnl.body, text='叠加 = 游戏效果和本软件效果一起；替换 = 只用本软件的；纯用游戏 = 关掉本软件的，只用游戏自己的。握把选纯用游戏时，事件特调也一起关掉；扬声器选特殊事件时，游戏不发声手柄就安静。\n'
                     '扳机同一时间只能跑一个效果，所以叠加时，游戏在用的那一侧归游戏，另一侧归本软件。自动 = 游戏发过扳机效果就交给游戏并记住，没发过就用本软件的。',
                     bg=PANEL, fg=DIM, font=(ZH, 9), anchor='w', justify='left', wraplength=px(900)).pack(fill='x', pady=(px(2), px(8)))
            NeonButton(pnl.body, '重置让位记录', reset_caps, w=150, h=32, size=10).pack(anchor='w')
            pnl = Panel(f, '预设', 'PRESET', accent=acc); pnl.pack(fill='x', pady=(0, px(10)))
            preset = tk.StringVar(); bind_presets(preset, sv)
            Segmented(pnl.body, preset, ('声感', '全感', '自定义'), cell=124, rec='全感').pack(anchor='w')
            tk.Label(pnl.body, text='声感 = 不用游戏的震动和扳机，只用本软件由声音生成的，扬声器放全部游戏声；全感 = 游戏效果和声音触感一起，游戏支持的扬声器和扳机交给游戏；自定义 = 你自己的组合，在连接方式和扬声器里调，会自动记住。',
                     bg=PANEL, fg=DIM, font=(ZH, 9), anchor='w', justify='left', wraplength=px(900)).pack(fill='x', pady=(px(4), 0))
        cols = tk.Frame(f, bg=BG); cols.pack(fill='x', pady=(0, px(10)))
        style_pnl = Panel(cols, '风格', 'PROFILE', accent=acc); style_pnl.pack(side='right', fill='both', padx=(px(10), 0))
        pnl = Panel(cols, '触感', 'HAPTICS', accent=acc); pnl.pack(side='left', fill='both', expand=True)
        Slider(pnl.body, '握把强度', sv['grip_gain']).pack(fill='x', pady=px(2))
        Slider(pnl.body, '扳机强度', sv['trigger_gain'], color=MAG).pack(fill='x', pady=px(2))
        if sc == '游戏':
            Slider(pnl.body, '游戏原生触感 / 震动', sv['rumble_gain'], color=AMBER).pack(fill='x', pady=px(2))
        elif sc == '影院':
            Slider(pnl.body, '低频隆隆（持续低音）', V['cinema_rumble'], color=AMBER).pack(fill='x', pady=px(2))
            Slider(pnl.body, '爆炸冲击（低频重击）', V['cinema_kick'], color=AMBER).pack(fill='x', pady=px(2))
            Slider(pnl.body, '扳机跟随', V['cinema_trigger'], color=MAG).pack(fill='x', pady=px(2))
        else:
            Slider(pnl.body, '鼓点（双手握把）', V['music_kick'], color=MAG).pack(fill='x', pady=px(2))
            Slider(pnl.body, '踩镲点触（右手轻快）', V['music_tick'], color=MAG).pack(fill='x', pady=px(2))
            Slider(pnl.body, '扳机律动（手指压着扳机才感觉得到）', V['music_trigger'], color=MAG).pack(fill='x', pady=px(2))
        pnl = style_pnl
        tk.Label(pnl.body, text='握把风格', bg=PANEL, fg=TEXT, font=(ZH, 10), anchor='w').pack(fill='x')
        Segmented(pnl.body, sv['grip_profile'], GRIP_PROFILES, cell=116, rec='均衡').pack(anchor='w', pady=(px(4), px(10)))
        tk.Label(pnl.body, text='扳机风格', bg=PANEL, fg=TEXT, font=(ZH, 10), anchor='w').pack(fill='x')
        Segmented(pnl.body, sv['trigger_profile'], TRIGGER_PROFILES, cell=116, color=MAG, rec='均衡冲击').pack(anchor='w', pady=(px(4), px(10)))
        Toggle(pnl.body, '忽略中置声道', sv['skip_center'], '5.1/7.1 时对白（中置）不触发震动', wrap=300).pack(fill='x', pady=px(4))
        Toggle(pnl.body, '纯人声时不震', sv['voice_gate'], '只有台词、旁白时压掉声音触感；同时有打击、爆炸就不压。不影响游戏原生震动', wrap=300, color=GREEN).pack(fill='x', pady=px(4))
        trigger_panel(f, sv)
        pnl = Panel(f, '手柄扬声器', 'SPEAKER', accent=BLUE); pnl.pack(fill='x', pady=(0, px(10)))
        fr = tk.Frame(pnl.body, bg=PANEL); fr.pack(fill='x')
        tk.Label(fr, text='播放', bg=PANEL, fg=TEXT, font=(ZH, 10), width=6 if i18n.LANG == 'zh' else 10, anchor='w').pack(side='left')
        Segmented(fr, sv['speaker_mode'], SPEAKER_MODES, cell=124, color=BLUE, rec='特殊事件' if sc == '游戏' else '全部游戏声').pack(side='left')
        fr = tk.Frame(pnl.body, bg=PANEL); fr.pack(fill='x', pady=(px(8), 0))
        tk.Label(fr, text='音色', bg=PANEL, fg=TEXT, font=(ZH, 10), width=6 if i18n.LANG == 'zh' else 10, anchor='w').pack(side='left')
        Segmented(fr, sv['speaker_eq'], SPEAKER_EQS, cell=124, color=BLUE, rec='原味' if sc == '游戏' else '低音饱满').pack(side='left')
        fr = tk.Frame(pnl.body, bg=PANEL); fr.pack(fill='x', pady=(px(8), 0))
        tk.Label(fr, text='输出到', bg=PANEL, fg=TEXT, font=(ZH, 10), width=6 if i18n.LANG == 'zh' else 10, anchor='w').pack(side='left')
        Segmented(fr, sv['speaker_output'], ('扬声器', '耳机孔'), cell=124, color=BLUE, rec='扬声器').pack(side='left')
        Slider(pnl.body, '扬声器音量', sv['speaker_volume'], 0, 100, color=BLUE).pack(fill='x', pady=(px(8), 0))
        tk.Label(pnl.body, justify='left', anchor='w', bg=PANEL, fg=DIM, font=(ZH, 9), wraplength=px(900), text=(
            '全部游戏声：手柄扬声器播放完整的游戏声音，并且覆盖游戏自己发给手柄扬声器的声音。音色：原味 = 不改；低音饱满 = 用倍频补出小喇叭放不出的低音；人声清晰 = 突出人声和金属声。\n'
            '特殊事件：不接管游戏自己的手柄扬声器，游戏发什么就原样播放什么（要用原生透传）；影之刃零同时保留事件适配。游戏第一次发声前无法判断，所以可能先放一下全部游戏声。没用手柄扬声器的游戏、电影、音乐会自动按全部游戏声播放。\n'
            '手柄插了耳机时从耳机出声。')).pack(fill='x', pady=(px(8), 0))
        pnl = Panel(f, '操作触感', 'CONTROLS', accent=GREEN); pnl.pack(fill='x', pady=(0, px(10)))
        grid = tk.Frame(pnl.body, bg=PANEL); grid.pack(fill='x')
        for i, (text, key, sub_text) in enumerate((('按键', 'button_haptics', '按下和松开时，对应一侧的握把轻点一下'),
                                                   ('扳机', 'trigger_haptics', '扳机按过一半和松回时轻点一下'),
                                                   ('摇杆', 'stick_haptics', '摇杆推到边缘时轻点一下'),
                                                   ('触摸板', 'touch_haptics', '手指碰到触摸板时轻点一下'))):
            grid.columnconfigure(i % 2, weight=1, uniform='ctl')
            Toggle(grid, text, sv[key], sub_text, wrap=380, color=GREEN).grid(row=i // 2, column=i % 2, sticky='ew', padx=(0, px(14)), pady=px(3))
        Slider(pnl.body, '操作触感强度', sv['button_gain'], color=GREEN).pack(fill='x', pady=(px(6), px(2)))
        pnl = Panel(f, '左右强度', 'BALANCE', accent=acc); pnl.pack(fill='x', pady=(0, px(10)))
        grid = tk.Frame(pnl.body, bg=PANEL); grid.pack(fill='x')
        for i, (text, key, col) in enumerate((('左握把', 'grip_left_gain', CYAN), ('右握把', 'grip_right_gain', CYAN),
                                              ('左扳机', 'trigger_left_gain', MAG), ('右扳机', 'trigger_right_gain', MAG))):
            grid.columnconfigure(i % 2, weight=1, uniform='bal')
            Slider(grid, text, sv[key], color=col).grid(row=i // 2, column=i % 2, sticky='ew', padx=(0, px(14)), pady=px(2))
        tk.Label(pnl.body, text='握把滑块调这一侧的全部震动；扳机滑块只调本软件生成的扳机震动，游戏原生的扳机效果保持原样。',
                 bg=PANEL, fg=DIM, font=(ZH, 9), anchor='w', justify='left', wraplength=px(900)).pack(fill='x', pady=(px(4), 0))
        pnl = Panel(f, '声音来源', 'SOURCE', accent=acc); pnl.pack(fill='x', pady=(0, px(10)))
        fr = tk.Frame(pnl.body, bg=PANEL); fr.pack(fill='x')
        Segmented(fr, sv['capture'], ('自动', '系统混音'), cell=124, rec='自动').pack(side='left')
        tk.Label(fr, text=('自动：先截已适配的游戏，其次截前台正在出声的程序' if sc == '游戏' else '自动：截当前正在出声的播放器 / 浏览器 / 音乐软件') +
                 '（按进程截音，开着杜比全景声也能用）；系统混音：截整个系统的声音。',
                 bg=PANEL, fg=DIM, font=(ZH, 9), justify='left', anchor='w', wraplength=px(620)).pack(side='left', padx=px(14))
        if sc == '游戏' and game is None:
            pad_panel(f)

    settings_page = page('设置', scroll=True)
    scene_frames = {}
    adv_state = {'open': True, 'views': []}           # nothing is folded away any more (kept for scripted screenshots)

    def set_advanced(opened):
        pass

    def show_scene_settings(*_a):
        for fr_ in scene_frames.values():
            fr_.pack_forget()
        scene_frames[V['scene'].get()].pack(fill='x')

    def reset_scene(sc, btn, st={'armed': None}):
        """Two clicks, no pop-up: the first one asks, the second one resets this scene."""
        if st['armed'] != sc:
            st['armed'] = sc; btn.config_look(text='再点一次确认', color=AMBER)
            def disarm():
                if st['armed'] == sc:
                    st['armed'] = None; btn.config_look(text='恢复默认', color=CYAN)
            root.after(3000, disarm)
            return
        st['armed'] = None; btn.config_look(text='恢复默认', color=CYAN)
        for k, v in SCENE_DEF[sc].items():
            if not k.startswith('custom_') and k in SV[sc]:
                SV[sc][k].set(v * 100 if k in PCT else v)
        msgs.put(f'「{sc}」场景已恢复默认设置。')

    def scene_settings(sc):
        acc, _art, tag = SCENE_INFO[sc]; sv = SV[sc]
        f = tk.Frame(settings_page, bg=BG); scene_frames[sc] = f
        hd = tk.Frame(f, bg=BG); hd.pack(fill='x', pady=(0, px(4)))
        tk.Label(hd, text=sc + '场景', bg=BG, fg=acc, font=(ZH, 13, 'bold')).pack(side='left')
        rb = NeonButton(hd, '恢复默认', lambda: reset_scene(sc, rb), w=110, h=30, size=10, bg=BG); rb.pack(side='right')
        tk.Label(f, text=tag, bg=BG, fg=DIM, font=(ZH, 9), anchor='w', justify='left', wraplength=px(940)).pack(fill='x', pady=(0, px(8)))
        settings_body(f, sv, sc, acc)
    for sc in SCENES:
        scene_settings(sc)
    show_scene_settings()
    root._hooks.update(set_advanced=set_advanced, show_error=show_error, hide_error=hide_error, scene_sw=scene_sw, side=side, nav_h=px(62),
                       errbar=errbar, nextbar=lambda: nextbar, frames=scene_frames, adv=adv_state, pad=pad, bigbtn=bigbtn, stop=stop, running=running)

    def scene_changed(*_a):
        show_scene_settings()
        msgs.put(f"已切换到「{V['scene'].get()}」场景" + ('（已实时生效）' if running() else '，点右上角「启动」开始。'))
    V['scene'].trace_add('write', scene_changed)

    # ---- the library grid
    games_on = {'ww': False, 'pbz': False}

    def section(title, sub):
        hd = tk.Frame(grid_view, bg=BG); hd.pack(fill='x', pady=(px(4), px(8)))
        tk.Frame(hd, bg=CYAN, width=px(4), height=px(18)).pack(side='left')
        tk.Label(hd, text=title, bg=BG, fg=TEXT, font=(ZH, 13, 'bold')).pack(side='left', padx=px(8))
        tk.Label(hd, text=sub, bg=BG, fg=FAINT, font=(NUM, 8)).pack(side='left')
        row = tk.Frame(grid_view, bg=BG); row.pack(fill='x', pady=(0, px(14)))
        return row

    def toggle(var):
        var.set(not var.get())
    cards = {}
    row = section('游戏适配', 'ADAPTATIONS  ·  游戏场景下生效，点击开启 / 关闭，齿轮进入设置')
    cards['鸣潮'] = GameCard(row, '鸣潮', '读屏技能识别 · 自适应扳机 · 事件特调', art='impact', accent=GREEN,
                           is_on=lambda: V['ww_enabled'].get(), badge=lambda: ('运行中', GREEN) if games_on['ww'] else None,
                           on_click=lambda: toggle(V['ww_enabled']), on_gear=lambda: show_detail('鸣潮'))
    cards['影之刃零'] = GameCard(row, '影之刃零', '格挡 · 闪避 · 受击事件特调', art='impact', accent=MAG,
                             is_on=lambda: V['pbz_fx'].get(), badge=lambda: ('运行中', GREEN) if games_on['pbz'] else None,
                             on_click=lambda: toggle(V['pbz_fx']), on_gear=lambda: show_detail('影之刃零'))
    for c in ('鸣潮', '影之刃零'):
        cards[c].pack(side='left', padx=(0, px(16)))
    tk.Label(grid_view, text='其他游戏不用设置，打开就能用：在「游戏」场景下自动把前台游戏的声音转成触感，支持 PS5 手柄的游戏走原生透传。',
             bg=BG, fg=DIM, font=(ZH, 9), anchor='w', justify='left', wraplength=px(940)).pack(fill='x')

    # ---- my games: cards the person adds; each can use the general settings or its own
    lib = {'cards': {}, 'vars': {}, 'running': set()}
    my_hd = tk.Frame(grid_view, bg=BG); my_hd.pack(fill='x', pady=(px(14), px(8)))
    tk.Frame(my_hd, bg=CYAN, width=px(4), height=px(18)).pack(side='left')
    tk.Label(my_hd, text='我的游戏', bg=BG, fg=TEXT, font=(ZH, 13, 'bold')).pack(side='left', padx=px(8))
    tk.Label(my_hd, text='MY GAMES  ·  添加游戏后可以给它单独调设置，没开专属设置的用「设置」页的通用设置', bg=BG, fg=FAINT, font=(NUM, 8)).pack(side='left')
    my_row = tk.Frame(grid_view, bg=BG); my_row.pack(fill='x', pady=(0, px(14)))

    def games():
        return cfg.setdefault('user_games', [])

    def game_of(gid):
        return next((g for g in games() if g['id'] == gid), None)

    def exe_name(g):
        return pathlib.Path(g.get('exe', '')).name.lower()

    def cover_img(g):
        import covers
        from PIL import Image
        name = g.get('cover')
        if name and (covers.CACHE / name).exists():
            try:
                return Image.open(covers.CACHE / name).convert('RGB')
            except Exception:
                return None
        return None

    def rebuild_library():
        for w in my_row.winfo_children():
            w.destroy()
        lib['cards'].clear()
        per = 4
        for i, g in enumerate(games() + [None]):
            r_, c_ = divmod(i, per)
            if g is None:
                card = GameCard(my_row, '添加游戏', '选择游戏的 exe，可以单独调触感', art='general', accent=GREEN,
                                is_on=lambda: True, on_text='＋', on_click=add_game, on_gear=add_game)
            else:
                gid = g['id']
                card = GameCard(my_row, g['name'], '专属设置' if g.get('own') else '用通用设置', art='impact', accent=CYAN,
                                is_on=lambda g=g: bool(g.get('own')), on_text='专属设置', off_text='通用设置',
                                badge=lambda g=g: ('运行中', GREEN) if exe_name(g) in lib['running'] else None,
                                on_click=lambda gid=gid: show_game(gid), on_gear=lambda gid=gid: show_game(gid))
                img = cover_img(g)
                if img is not None:
                    card.set_cover(img)
                lib['cards'][gid] = card
            card.grid(row=r_, column=c_, padx=(0, px(16)), pady=(0, px(16)), sticky='w')

    def steam_cover(g, quiet=False):
        def work():
            import covers
            appid, _n = covers.steam_app_for(g.get('exe', ''))
            path = covers.game_cover(appid) if appid else None
            def done():
                if path:
                    g['cover'] = path.name; apply_live(); rebuild_library()
                    if not quiet:
                        msgs.put(f"已换成 Steam 的封面：{g['name']}")
                elif not quiet:
                    msgs.put('这个游戏不在 Steam 库里，或者 Steam 还没有它的封面：可以点「上传封面图片」。')
            root.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    def add_game():
        from tkinter import filedialog
        path = filedialog.askopenfilename(title=tr('选择游戏的 exe'), filetypes=[(tr('程序'), '*.exe')])
        if not path:
            return
        p = pathlib.Path(path)
        if any(exe_name(g) == p.name.lower() for g in games()):
            msgs.put('这个游戏已经在「我的游戏」里了。'); return
        import covers, uuid
        _appid, steam_name = covers.steam_app_for(p)
        generic = p.stem.lower() in ('game', 'launcher', 'start', 'client', 'main') or p.stem.lower().endswith('-win64-shipping')
        name = steam_name or (p.parent.name if generic else p.stem)
        g = dict(id=uuid.uuid4().hex[:8], name=name, exe=str(p), cover='', own=False,
                 settings={k: (v.get() / 100 if k in PCT else v.get()) for k, v in SV['游戏'].items()})
        games().append(g); apply_live(); rebuild_library(); steam_cover(g, quiet=True)
        msgs.put(f'已添加「{name}」：点卡片可以改封面，打开专属设置后单独调触感和扳机。')
        show_game(g['id'])

    def show_game(gid):
        key = 'game:' + gid
        if key not in details:
            build_game(gid)
        show_detail(key)

    def build_game(gid):
        g = game_of(gid)
        f = detail('game:' + gid, g['name'], CYAN)
        pnl = Panel(f, '游戏信息', 'GAME', accent=CYAN); pnl.pack(fill='x', pady=(0, px(10)))
        fr = tk.Frame(pnl.body, bg=PANEL); fr.pack(fill='x', pady=px(3))
        tk.Label(fr, text='名称', bg=PANEL, fg=TEXT, font=(ZH, 10), width=8, anchor='w').pack(side='left')
        name_v = tk.StringVar(value=g['name'])
        tk.Entry(fr, textvariable=name_v, width=30, bg=PANEL2, fg=CYAN, insertbackground=CYAN, relief='flat', font=(ZH, 10),
                 highlightthickness=1, highlightbackground=LINE2, highlightcolor=CYAN).pack(side='left', ipady=px(4))

        def rename(*_a):
            g['name'] = name_v.get().strip() or g['name']; apply_live()
            card = lib['cards'].get(gid)
            if card is not None:
                card.title = g['name']; card.redraw()
        name_v.trace_add('write', rename)
        fr = tk.Frame(pnl.body, bg=PANEL); fr.pack(fill='x', pady=px(3))
        tk.Label(fr, text='程序', bg=PANEL, fg=TEXT, font=(ZH, 10), width=8, anchor='w').pack(side='left')
        exe_lb = tk.Label(fr, text=g['exe'], bg=PANEL, fg=DIM, font=(MONO, 9), anchor='w'); exe_lb.pack(side='left')

        def change_exe():
            from tkinter import filedialog
            path = filedialog.askopenfilename(title=tr('选择游戏的 exe'), filetypes=[(tr('程序'), '*.exe')])
            if path:
                g['exe'] = str(pathlib.Path(path)); exe_lb.configure(text=g['exe']); apply_live()
        NeonButton(fr, '更改', change_exe, w=80, h=28, size=9).pack(side='left', padx=px(10))
        fr = tk.Frame(pnl.body, bg=PANEL); fr.pack(fill='x', pady=(px(8), 0))

        def upload_cover():
            from tkinter import filedialog
            path = filedialog.askopenfilename(title=tr('选择封面图片'), filetypes=[(tr('图片'), '*.jpg *.jpeg *.png *.webp *.bmp')])
            if not path:
                return
            try:
                import covers
                from PIL import Image
                covers.CACHE.mkdir(exist_ok=True)
                img = Image.open(path).convert('RGB'); img.thumbnail((1200, 1800))
                name = f'user-{gid}.jpg'; img.save(covers.CACHE / name, quality=92)
                g['cover'] = name; apply_live(); rebuild_library(); msgs.put('封面已更新。')
            except Exception as exc:
                msgs.put('封面图片打不开：' + str(exc))
        NeonButton(fr, '上传封面图片', upload_cover, w=140, h=32, size=10).pack(side='left')
        NeonButton(fr, '使用 Steam 封面', lambda: steam_cover(g), w=150, h=32, size=10).pack(side='left', padx=px(8))
        arm = {'on': False}

        def delete_game(btn_holder=[]):
            btn = btn_holder[0]
            if not arm['on']:
                arm['on'] = True; btn.config_look(text='再点一次删除', color=MAG)
                root.after(3000, lambda: (arm.update(on=False), btn.config_look(text='删除这个游戏', color=FAINT)))
                return
            cfg['user_games'] = [x for x in games() if x['id'] != gid]; apply_live()
            details.pop('game:' + gid).destroy(); show_grid(); rebuild_library(); msgs.put(f"已从「我的游戏」删除「{g['name']}」。")
        holder = []
        db = NeonButton(fr, '删除这个游戏', lambda: delete_game(holder), w=140, h=32, size=10, color=FAINT); db.pack(side='right'); holder.append(db)
        pnl = Panel(f, '设置方式', 'PROFILE', accent=GREEN); pnl.pack(fill='x', pady=(0, px(10)))
        own_v = tk.BooleanVar(value=bool(g.get('own')))
        Toggle(pnl.body, '使用这个游戏的专属设置', own_v, '关闭 = 用「设置」页的通用设置；打开 = 下面的设置只对这个游戏生效，游戏在前台时自动换上', wrap=820, color=GREEN).pack(fill='x', pady=px(3))
        tools = tk.Frame(pnl.body, bg=PANEL)
        body = tk.Frame(f, bg=BG)
        vals = {k: (v.get() / 100 if k in PCT else v.get()) for k, v in SV['游戏'].items()}
        vals.update(g.get('settings') or {})
        gsv = make_vars(vals); lib['vars'][gid] = gsv

        def store(*_a):
            g['settings'] = {k: (v.get() / 100 if k in PCT else v.get()) for k, v in gsv.items()}; apply_live()
        for v in gsv.values():
            v.trace_add('write', store)

        def from_general():
            for k, v in SV['游戏'].items():
                if k in gsv:
                    gsv[k].set(v.get())
            msgs.put('已把「设置」页的通用设置复制到这个游戏。')
        NeonButton(tools, '从通用设置复制', from_general, w=150, h=30, size=10).pack(side='left')
        settings_body(body, gsv, '游戏', CYAN, game=g)

        def show_own(*_a):
            g['own'] = bool(own_v.get()); apply_live()
            card = lib['cards'].get(gid)
            if card is not None:
                card.sub = '专属设置' if g['own'] else '用通用设置'; card.redraw()
            if g['own']:
                tools.pack(fill='x', pady=(px(6), 0)); body.pack(fill='x')
            else:
                tools.pack_forget(); body.pack_forget()
        own_v.trace_add('write', show_own); show_own()
    rebuild_library(); root._show_game = show_game
    def redraw_cards(*a):
        for c in list(cards.values()) + list(lib['cards'].values()): c.redraw()
    for k in ('ww_enabled', 'pbz_fx', 'scene'):
        V[k].trace_add('write', redraw_cards)

    def load_covers():
        import covers
        from PIL import Image
        for key, appid in (('鸣潮', 3513350), ('影之刃零', 4115450)):
            try:
                path = covers.game_cover(appid)
                if path:
                    img = Image.open(path).convert('RGB'); root.after(0, lambda k=key, im=img: cards[k].set_cover(im))
            except Exception:
                pass
    threading.Thread(target=load_covers, daemon=True).start()

    def watch_games():
        import proc_loopback as plb
        pbz_since = None; warned = False
        while True:
            try:
                games_on['ww'] = plb.find_pid('Client-Win64-Shipping.exe') is not None
                games_on['pbz'] = plb.find_pid('PBZ.exe', 'PBZ-Win64-Shipping.exe') is not None
                lib['running'] = {exe_name(g) for g in list(games()) if exe_name(g) and plb.find_pid(pathlib.Path(g['exe']).name) is not None}
                root.after(0, redraw_cards)
                if games_on['pbz'] and V['pbz_fx'].get():
                    pbz_since = pbz_since or time.time()
                    if not warned and time.time() - pbz_since > 180:
                        import sysfix
                        st = sysfix.pbz_status()
                        if st['mod'] == 'ok' and (st['last_log'] is None or st['last_log'] < pbz_since - 5):
                            warned = True
                            msgs.put('影之刃零已运行 3 分钟，但事件 mod 没有写出日志：可能是游戏更新后 UE4SS 或 mod 失效。'
                                     '事件特调暂时不起作用，原生触感和声音触感不受影响。请检查 UE4SS 是否有新版本。')
                else:
                    pbz_since = None; warned = False
            except Exception:
                pass
            time.sleep(3)
    threading.Thread(target=watch_games, daemon=True).start()
    pg = page('系统', scroll=True)

    p = Panel(pg, '系统检查', 'SYSTEM', accent=AMBER); p.pack(fill='x', pady=(0, px(10)))
    dep_lb = tk.Label(p.body, text='检查中…', bg=PANEL, fg=TEXT, font=(ZH, 10), justify='left', anchor='w'); dep_lb.pack(fill='x')
    fr = tk.Frame(p.body, bg=PANEL); fr.pack(fill='x', pady=(px(8), 0))
    intel_lb = tk.Label(p.body, text='', bg=PANEL, fg=DIM, font=(ZH, 9), justify='left', anchor='w', wraplength=px(900))
    sysstate = {'missing': [], 'asked': False}

    def check_system(auto=False):
        def work():
            try:
                import deps, sysfix
                st = deps.status(); miss = [s for s in st if not s[2]]
                txt = '　'.join(('✓ ' if ok else '✗ ') + disp for _k, disp, ok, _n in st)
                intel = sysfix.intel_offload_devices()
                on = [d for d in intel if d[2].upper() == 'OK']
                itxt = ('检测到「适用于 USB 音频的英特尔® 智音技术」且处于开启状态：它可能吞掉手柄的触感声道。'
                        '点右边按钮在本软件运行期间关闭它，退出软件时自动恢复。') if on else \
                       ('英特尔 USB 音频：已关闭或本机没有，无需处理。' if intel else '英特尔 USB 音频：本机没有这个设备，无需处理。')
                root.after(0, lambda: show_sys(txt, miss, itxt, bool(on), auto))
            except Exception as exc:
                err = str(exc); root.after(0, lambda: dep_lb.configure(text='系统检查失败：' + err))
        threading.Thread(target=work, daemon=True).start()

    def show_sys(txt, miss, itxt, intel_on, auto):
        sysstate['missing'] = miss
        dep_lb.configure(text='驱动依赖：' + txt, fg=TEXT if not miss else AMBER)
        intel_lb.configure(text=itxt, fg=AMBER if intel_on else DIM)
        if miss:
            add_log('缺少依赖：' + '、'.join(m[1] for m in miss) + '。点「系统」页的「一键安装依赖」，或运行软件旁边的「依赖.exe」。')
            if auto and not sysstate['asked']:                 # first open: offer the install right away
                sysstate['asked'] = True; install_deps()

    def install_deps():
        import deps
        exe = deps.installer_exe()
        if exe is None:
            msgs.put('找不到「依赖.exe」：请从发布包里完整解压（依赖.exe 和「依赖安装包」文件夹要在软件旁边）。'); return
        if deps.run_elevated(exe, '--auto'):
            msgs.put('已启动依赖安装（需要在弹出的窗口里允许管理员权限）。装完后点「重新检查」。')
        else:
            msgs.put('依赖安装没有启动（管理员权限被拒绝？）。也可以手动双击「依赖.exe」。')

    def intel_off():
        import sysfix, os
        if sysfix.start_guard(os.getpid()):
            msgs.put('已关闭英特尔 USB 音频（需要允许管理员权限）。退出本软件时会自动恢复。')
            root.after(4000, check_system)
        else:
            msgs.put('没有获得管理员权限，未做修改。')
    NeonButton(fr, '一键安装依赖', install_deps, w=130, h=32, size=10, color=AMBER).pack(side='left')
    NeonButton(fr, '重新检查', check_system, w=100, h=32, size=10).pack(side='left', padx=px(8))
    NeonButton(fr, '关闭英特尔USB音频', intel_off, w=170, h=32, size=10, color=AMBER).pack(side='left', padx=px(8))

    def repair_pad_ui():
        """Crash left the controller hidden / unrecognised: stop, undo every hide, close a leftover virtual pad."""
        msgs.put('正在修复手柄…'); hide_error()
        if running():
            stop()

        def work():
            try:
                if runner['t']:
                    runner['t'].join(10)                         # takeover gives the real pad back first
                from takeover import repair_pad
                res = repair_pad(msgs.put)
                msgs.put('修复手柄完成：' + res + '。如果游戏已经开着，请把手柄拔掉重插或重启游戏。')
            except Exception as exc:
                msgs.put('修复手柄失败：' + str(exc))
        threading.Thread(target=work, daemon=True).start()
    NeonButton(fr, '修复手柄', repair_pad_ui, w=110, h=32, size=10, color=GREEN).pack(side='left', padx=px(8))
    tk.Label(p.body, text='崩溃后手柄插上去电脑或游戏识别不到时，点「修复手柄」：取消对游戏的隐藏、关闭残留的虚拟手柄。软件每次打开也会自动检查一次。',
             bg=PANEL, fg=DIM, font=(ZH, 9), anchor='w', justify='left', wraplength=px(900)).pack(fill='x', pady=(px(8), 0))
    intel_lb.pack(fill='x', pady=(px(8), 0))


    p = Panel(pg, '自检与反馈', 'DIAGNOSE', accent=GREEN); p.pack(fill='x', pady=(0, px(10)))
    tk.Label(p.body, text='遇到问题点「开始自检」：检查驱动、手柄、手柄声卡、HidHide、英特尔 USB 音频和最近的报错，运行中还会记下最近 60 秒每一路震动的来源。'
             '点「反馈到 GitHub」会生成诊断报告、复制到剪贴板并打开 GitHub 反馈页，粘贴后提交即可（需要登录 GitHub）。报告里已经去掉用户名和电脑名，不会自动上传。',
             bg=PANEL, fg=DIM, font=(ZH, 9), anchor='w', justify='left', wraplength=px(900)).pack(fill='x')
    diag_lb = tk.Label(p.body, text='', bg=PANEL, fg=TEXT, font=(ZH, 10), anchor='w', justify='left', wraplength=px(900))
    diag_lb.pack(fill='x', pady=(px(6), 0))
    fr = tk.Frame(p.body, bg=PANEL); fr.pack(fill='x', pady=(px(8), 0))
    diag_state = {'busy': False}

    def run_selfcheck(open_issue=False, auto=False):
        if diag_state['busy']:
            return
        diag_state['busy'] = True
        if not auto:
            diag_lb.configure(text='正在自检…', fg=DIM)
        r = runner['r'] if running() else None
        stats = dict(r.stats) if r is not None else None
        tl = list(getattr(r, 'timeline', ()) or ()) if r is not None else list(getattr(runner['r'], 'timeline', ()) or ()) if runner['r'] else []

        def work():
            try:
                import selfcheck
                probs, rep = selfcheck.collect(dict(cfg), stats, tl, log_path, running=r is not None)
                path = selfcheck.save(rep)
            except Exception as exc:
                err = str(exc); root.after(0, lambda: (diag_state.update(busy=False), diag_lb.configure(text='自检失败：' + err, fg=MAG)))
                return

            def done():
                diag_state['busy'] = False
                head = ('发现的问题：\n' + '\n'.join(f'{i}. {x}' for i, x in enumerate(probs, 1))) if probs else '没有发现明显问题。'
                diag_lb.configure(text=head + '\n报告：diagnostics/' + path.name, fg=AMBER if probs else GREEN)
                if open_issue:
                    import webbrowser
                    root.clipboard_clear(); root.clipboard_append(rep)
                    webbrowser.open(selfcheck.issue_url(probs))
                    msgs.put('诊断报告已复制到剪贴板，并打开了 GitHub 反馈页：在「诊断报告」下面按 Ctrl+V 粘贴后提交。报告也存在 diagnostics 文件夹。')
                elif auto:
                    msgs.put('已自动保存诊断报告：diagnostics/' + path.name + '。需要反馈的话点「反馈问题」。')
            root.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    def open_reports():
        import selfcheck
        selfcheck.REPORT_DIR.mkdir(exist_ok=True); os.startfile(str(selfcheck.REPORT_DIR))
    root._selfcheck = run_selfcheck
    NeonButton(fr, '开始自检', run_selfcheck, w=120, h=32, size=10, color=GREEN).pack(side='left')
    NeonButton(fr, '反馈到 GitHub', lambda: run_selfcheck(open_issue=True), w=150, h=32, size=10, color=GREEN, filled=True).pack(side='left', padx=px(8))
    NeonButton(fr, '打开报告文件夹', open_reports, w=150, h=32, size=10).pack(side='left')

    p = Panel(pg, '语言与更新', 'GENERAL'); p.pack(fill='x', pady=(0, px(10)))
    fr = tk.Frame(p.body, bg=PANEL); fr.pack(fill='x', pady=(0, px(8)))
    tk.Label(fr, text='语言', bg=PANEL, fg=TEXT, font=(ZH, 10), width=6 if i18n.LANG == 'zh' else 10, anchor='w').pack(side='left')
    lang_names = {code: name for code, name in i18n.LANGS}
    lang_var = tk.StringVar(value=lang_names.get(i18n.LANG, ''))
    Segmented(fr, lang_var, tuple(lang_names.values()), cell=64).pack(side='left')
    lang_var.trace_add('write', lambda *a: set_language(next(c for c, n in lang_names.items() if n == lang_var.get())))
    tk.Label(fr, text='切换后软件会自动重启', bg=PANEL, fg=FAINT, font=(ZH, 9)).pack(side='left', padx=px(10))
    fr = tk.Frame(p.body, bg=PANEL); fr.pack(fill='x')
    tk.Label(fr, text=f'当前版本 v{about.VERSION}', bg=PANEL, fg=TEXT, font=(ZH, 10)).pack(side='left', padx=(0, px(14)))
    NeonButton(fr, '检查更新', lambda: check_update(manual=True), w=110, h=32, size=10).pack(side='left')

    p = Panel(pg, '通用设置', 'GENERAL', accent=CYAN); p.pack(fill='x', pady=(0, px(10)))
    fr = tk.Frame(p.body, bg=PANEL); fr.pack(fill='x', pady=px(4))
    tk.Label(fr, text='只在这个游戏在前台时工作', bg=PANEL, fg=TEXT, font=(ZH, 10)).pack(side='left')
    tk.Entry(fr, textvariable=V['foreground_exe'], width=30, bg=PANEL2, fg=CYAN, insertbackground=CYAN, relief='flat', font=(MONO, 10),
             highlightthickness=1, highlightbackground=LINE2, highlightcolor=CYAN).pack(side='left', padx=px(12), ipady=px(4))
    tk.Label(fr, text='exe 名，留空 = 始终工作', bg=PANEL, fg=FAINT, font=(ZH, 9)).pack(side='left')
    Toggle(p.body, '录制调校数据', V['record_native'], '原生透传时把游戏的音圈强度、扳机指令和鸣潮识别结果记到 traces 文件夹，反馈问题时附上它', wrap=820).pack(fill='x', pady=px(4))

    # ---- 日志
    pg = page('日志')
    p = Panel(pg, '运行日志', 'CONSOLE'); p.pack(fill='both', expand=True, pady=(0, px(10)))
    log = tk.Text(p.body, bg=PANEL, fg=TEXT, font=(ZH, 9), relief='flat', bd=0, highlightthickness=0, wrap='word', insertbackground=CYAN)
    log.pack(fill='both', expand=True); log.tag_configure('t', foreground=CYAN); log.tag_configure('e', foreground=MAG)

    # ---- footer
    foot = tk.Canvas(root, height=px(30), bg=BG, highlightthickness=0, bd=0); foot.pack(fill='x', side='bottom')
    fstate = {'msg': '未启动。PS5 手柄需要 USB 线或 DS5Dongle 接收器（测试版）。'}

    def draw_foot():
        foot.delete('all'); w = foot.winfo_width()
        foot.create_line(0, 1, w, 1, fill=LINE)
        foot.create_text(px(16), px(16), text='▸', fill=CYAN, font=(ZH, 9), anchor='w')
        msg = tr(fstate['msg']); fnt = tkfont.Font(family=ZH, size=9); room = max(px(100), w - px(420))   # keep clear of the right-hand label
        if fnt.measure(msg) > room:                                           # translate first, then cut to fit
            while msg and fnt.measure(msg + '…') > room:
                msg = msg[:-1]
            msg += '…'
        foot.create_text(px(32), px(16), text=msg, fill=DIM, font=(ZH, 9), anchor='w')
        foot.create_text(w - px(16), px(16), text='DUALSENSE · VOICE COIL · ADAPTIVE TRIGGER', fill=FAINT, font=(NUM, 8), anchor='e')
    foot.bind('<Configure>', lambda e: draw_foot())                           # the first log line arrives before layout

    def add_log(m):
        ts = time.strftime('%H:%M:%S')
        log.insert('end', ts + '  ', 't'); log.insert('end', m + '\n', 'e' if ('出错' in m or '失败' in m) else ''); log.see('end')
        fstate['msg'] = m.split('\n')[0][:120]; draw_foot()
        if m.startswith('已停止。原因：'):
            show_error(m.split('\n')[0])
            root.after(800, lambda: run_selfcheck(auto=True))       # a report on disk for every failure
        if log_path:                     # same log on disk (traces/app-log.txt) for diagnosis
            try:
                with open(log_path, 'a', encoding='utf8') as f:
                    f.write(time.strftime('%Y-%m-%d ') + ts + '  ' + m + '\n')
            except Exception:
                pass

    def watch_pad():
        while True:
            if not demo_mode and not running():
                try:
                    pad['state'] = pad_status(); pad['known'] = True
                except Exception:
                    pad['known'] = False           # can't tell: never block on that
            time.sleep(2)
    threading.Thread(target=watch_pad, daemon=True).start()
    show('总览'); t_start = {'t': None}; last = {'run': None}
    watch_vars()
    MODE_EN = {'原生透传': 'NATIVE', '接管': 'TAKEOVER', 'Xbox': 'XBOX', '普通': 'OVERLAY'}

    tick = {'n': 0}

    fsc = {'t': 0.0, 'on': False}

    def pump():
        tick['n'] += 1; slow = tick['n'] % 4 == 0
        while not msgs.empty():
            add_log(msgs.get())
        if time.monotonic() - fsc['t'] > 1.0:                # another program fullscreen: stop drawing entirely
            fsc['t'] = time.monotonic(); was = fsc['on']; fsc['on'] = other_fullscreen(root.winfo_id())
            if fsc['on'] != was:
                fstate['msg'] = ('其他程序全屏中：总览已暂停刷新，切回来自动恢复' if fsc['on'] else fstate['msg']); draw_foot()
        if fsc['on']:
            root.after(1000, pump); return
        run = running(); r = runner['r']; s = r.stats if (r and run) else {}
        if run != last['run']:
            last['run'] = run; t_start['t'] = time.time() if run else None
            bigbtn.config_look(text='■  停止' if run else '▶  启动', color=MAG if run else CYAN)
            for lb in locks:
                lb.configure(text='')
            if not run:
                show_banner([])
                test_btn.pack(side='left')
            else:
                test_btn.pack_forget()
        stopping = run and r.stop_ev.is_set()
        if stopping: bigbtn.config_look(text='…  停止中', color=AMBER)
        w = hstate['w']; pulse = 0.5 + 0.5 * math.sin(time.time() * 5)
        if not slow:
            return fast_part(run, s)
        col = AMBER if stopping else (GREEN if run else FAINT)
        head.itemconfigure('stt', text='停止中' if stopping else ('运行中' if run else '待机'), fill=col)
        pad_txt = {'usb': '手柄已连接', 'edge': '手柄已连接（Edge）', 'bluetooth': '手柄：蓝牙（不支持）', None: '未检测到手柄'}[pad['state']] if pad['known'] else ''
        head.itemconfigure('pad', text='手柄使用中' if run else pad_txt, fill=GREEN if run or pad['state'] in ('usb', 'edge') else AMBER)
        update_next(run)
        d = px(5) + (px(2) * pulse if run else 0); x, y = w - px(196), px(48)
        head.coords('dot', x - d, y - d, x + d, y + d); head.itemconfigure('dot', fill=mix(BG, col, 0.5 + 0.5 * pulse) if run else FAINT)
        # side panel clock
        up = int(time.time() - t_start['t']) if t_start['t'] else 0
        cur['up_t'] = tr('运行时长') + '  ' + (f'{up // 3600:02d}:{up // 60 % 60:02d}:{up % 60:02d}' if run else '--:--:--')
        side.itemconfigure('up', text=cur['up_t'])       # kept in cur: the sidebar redraws on every mouse move
        bat = s.get('battery')                              # (percent, state); the level moves in 10 % steps
        bat_txt = '--' if not bat else tr('已充满') if bat[1] == 'full' else f'{bat[0]}%' + ('  ' + tr('充电中') if bat[1] == 'charging' else '')
        cur['bat_t'] = tr('手柄电量') + '  ' + bat_txt; cur['bat_c'] = MAG if bat and bat[1] == 'discharging' and bat[0] <= 15 else DIM
        side.itemconfigure('battery', text=cur['bat_t'], fill=cur['bat_c'])
        # dashboard
        try:
            fg = root.state() != 'iconic'               # minimised: nothing to draw
        except Exception:
            fg = True
        if cur['p'] == '总览' and fg:
            fast_part(run, s, reschedule=False)
            rum = s.get('rumble', (0.0, 0.0))
            lvl_lb.configure(text=f"OUT {s.get('level', 0.0):4.2f}   NATIVE {max(rum):4.2f}" if run else 'STANDBY')
            m = r.start_cfg.get('mode', '') if run else SC('mode')
            info['工作方式'].set(m + ('（运行中）' if run else '（未启动）'), GREEN if run else DIM)
            info['声源'].set(('⚠ 截不到游戏声音（关闭空间音效/独占模式）  ' if s.get('silent') else '') + (s.get('source') or '—') + ('  ·  人声中，声音触感已压低' if s.get('voice_closed') else ''),
                           MAG if s.get('silent') else TEXT)
            vp = s.get('vpad')
            info['虚拟手柄'].set(f'输入转发 {vp[0]}  ·  反馈包 {vp[1]}' if vp else '—', CYAN if vp else DIM)
            spv = s.get('speaker', 0.0)
            info['扬声器'].set(('关闭' if cfg.get('speaker_mode') == '关闭' else f"{s.get('spk_eff') or cfg.get('speaker_mode')}"
                               f"{'（游戏没用手柄扬声器，自动全部游戏声）' if cfg.get('speaker_mode') == '特殊事件' and s.get('spk_eff') == '全部游戏声' else ''}  ·  电平 {'▮' * int(min(1, spv) * 12):<12} {spv:.2f}  ·  延迟 {s.get('lag_ms', 0)} ms  ·  断流 {s.get('xruns', 0)}") if run else '—',
                             CYAN if spv > 0.02 else DIM)
            scn = cfg.get('scene', '游戏') if run else V['scene'].get()
            info['场景'].set(f"{scn}  ·  扬声器 {cfg.get('speaker_mode', '') if run else SC('speaker_mode')}"
                             f"（{cfg.get('speaker_eq', '') if run else SC('speaker_eq')}）", CYAN)
            info['游戏专属'].set(s.get('profile') or '无（通用）', GREEN if s.get('profile') else DIM)
            info['前台'].set(('工作中' if s.get('active') else '等待目标游戏到前台') if run else '—', TEXT if s.get('active') else AMBER)
            info['丢块'].set(str(s.get('late', 0)) if run else '—', MAG if s.get('late', 0) > 50 else TEXT)
            ww = s.get('ww')
            if s.get('profile') and not p_ww.winfo_ismapped():     # only while that game is in front
                p_ww.pack(side='right', fill='y', padx=(px(10), 0), after=p_ev)
            elif not s.get('profile') and p_ww.winfo_ismapped():
                p_ww.pack_forget()
            if s.get('profile') and ww:
                note = f'截图 {ww[1]} 帧' + (f'  错误 {ww[2]}' if ww[2] else '')
                hexes.update_vals(ww[0], note)
            else:
                hexes.update_vals(None, '切到鸣潮后自动识别技能栏' if run else '未启动')
            fxl = s.get('fx_last')
            if fxl and fxl != ev_state['last']:
                if ev_state['last'] is None:
                    ev_box.configure(state='normal'); ev_box.delete('1.0', 'end'); ev_box.configure(state='disabled')
                ev_state['last'] = fxl; ev_box.configure(state='normal')
                ev_box.insert('1.0', fxl + '\n', 'n'); ev_box.insert('1.0', time.strftime('%H:%M:%S  '), 't')
                ev_box.delete('30.0', 'end'); ev_box.configure(state='disabled')
        schedule()

    def schedule():
        try:
            focused = root.focus_displayof() is not None
        except Exception:
            focused = True
        root.after(66 if focused else 200, pump)      # 15 fps in front; 5 fps while gaming

    def fast_part(run, s, reschedule=True):
        """Meters, waveform, trigger gauges - the only things that move every frame."""
        try:
            vis = root.state() != 'iconic'
        except Exception:
            vis = True
        if cur['p'] == '总览' and vis:
            g = s.get('grip') or (s.get('level', 0.0),) * 2
            gm.update_vals(*g)
            rum = s.get('rumble', (0.0, 0.0))
            scope.push(s.get('level', 0.0) if run else 0.0, max(rum) if run else 0.0, idle=not run)
            pr = s.get('press', (0.0, 0.0)); tr = s.get('trigger', (0.0, 0.0)); nt = s.get('native_trig', (False, False))
            gl.update_vals(pr[0], tr[0], nt[0]); gr.update_vals(pr[1], tr[1], nt[1])
        if reschedule:
            schedule()

    root.after(50, draw_foot)

    def close():
        stop()
        if runner['t']:
            runner['t'].join(8)          # takeover needs a moment to un-hide the real pad
        root.destroy()
    root.protocol('WM_DELETE_WINDOW', close); pump()
    add_log('就绪。PS5 手柄请用 USB 线或 DS5Dongle 接收器（测试版）连接，然后点右上角「启动」。')
    root.after(800, lambda: check_system(auto=True))
    return root
