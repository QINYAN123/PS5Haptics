"""Sci-fi HUD front end for PS5Haptics - pure tkinter (Canvas-drawn widgets), no extra dependencies.

run_ui(cfg, runner_cls, save_cfg, test_fn) builds the window. The Runner contract is unchanged:
Runner(cfg, log).run() in a thread, .stop_ev to stop, .stats dict for the live dashboard.
"""
from __future__ import annotations
import ctypes as C
import ctypes
import math
import pathlib
import about
import queue
import random
import threading
import time
import tkinter as tk

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
        super().__init__(master, width=px(w), height=px(h), bg=bg, highlightthickness=0, bd=0, cursor='hand2')
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


class Segmented(tk.Canvas):
    """Option picker drawn as HUD cells (replaces OptionMenu)."""

    def __init__(self, master, var, options, cell=118, h=34, cols=None, bg=PANEL, color=CYAN, rec=None):
        self.rec = rec; self.opts = list(options); self.var = var; self.cellw = px(cell); self.h = px(h); self.color = color
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
                bw, bh = px(30), px(14)
                self.create_rectangle(x + self.cellw - bw - px(4), y + px(3), x + self.cellw - px(4), y + px(3) + bh,
                                      fill=mix(self.bgc, GREEN, 0.15), outline=GREEN)
                self.create_text(x + self.cellw - bw / 2 - px(4), y + px(3) + bh / 2, text='推荐', fill=GREEN, font=(ZH, 7, 'bold'))
                tx -= px(14)
            self.create_text(tx, y + self.h // 2 + (px(3) if o == self.rec else 0), text=o, fill=TEXT if sel else DIM,
                             font=(ZH, 10, 'bold' if sel else 'normal'))


class Slider(tk.Canvas):
    def __init__(self, master, label, var, frm=0, to=200, unit='%', bg=PANEL, color=CYAN, hint=''):
        super().__init__(master, height=px(56), bg=bg, highlightthickness=0, bd=0, cursor='hand2')
        self.label, self.var, self.frm, self.to, self.unit, self.bgc, self.color, self.hint = label, var, frm, to, unit, bg, color, hint
        self.bind('<Configure>', lambda e: self.draw()); self.bind('<Button-1>', self._drag); self.bind('<B1-Motion>', self._drag)
        self.bind('<MouseWheel>', lambda e: self._step(5 if e.delta > 0 else -5))
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
            cv = tk.Canvas(self, height=px(132), bg=bg, highlightthickness=0, bd=0, cursor='hand2')
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
                tw = px(12) * len(tag) + px(14)
                cv.create_rectangle(w - tw - px(12), px(13), w - px(12), px(31), fill=mix(PANEL, GREEN, 0.15), outline=GREEN)
                cv.create_text(w - tw / 2 - px(12), px(22), text=tag, fill=GREEN, font=(ZH, 8, 'bold'))
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
        self.create_text(px(6), px(8), text='触感输出', anchor='nw', fill=CYAN, font=(ZH, 8), tags='grid')
        self.create_text(px(70), px(8), text='游戏原生', anchor='nw', fill=AMBER, font=(ZH, 8), tags='grid')
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
        tk.Label(self, text=key, bg=bg, fg=DIM, font=(ZH, 9), width=8, anchor='w').pack(side='left')
        self.v = tk.Label(self, text='—', bg=bg, fg=TEXT, font=(ZH, 9), anchor='w', justify='left')
        self.v.pack(side='left', fill='x', expand=True)

    def set(self, text, color=TEXT):
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
            self.stats = {'level': hit, 'grip': (hit * 0.9 + random.random() * 0.1, hit * 0.7 + random.random() * 0.12),
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


def other_fullscreen(own_hwnd=0):
    """True while another program is fullscreen: exclusive fullscreen (D3D) or a borderless window
    covering its whole monitor. The dashboard then stops drawing so the game keeps every frame."""
    try:
        u = ctypes.windll.user32
        st = ctypes.c_int(0)
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(st)) == 0 and st.value == 3:
            return True                                   # QUNS_RUNNING_D3D_FULL_SCREEN (exclusive fullscreen)
        u.GetForegroundWindow.restype = ctypes.c_void_p
        h = u.GetForegroundWindow()
        if not h:
            return False
        u.GetAncestor.restype = ctypes.c_void_p
        root_h = u.GetAncestor(ctypes.c_void_p(h), 2)    # GA_ROOT
        if own_hwnd and root_h == u.GetAncestor(ctypes.c_void_p(own_hwnd), 2):
            return False                                  # that's us
        cls = ctypes.create_unicode_buffer(64); u.GetClassNameW(ctypes.c_void_p(h), cls, 64)
        if cls.value in ('Progman', 'WorkerW', 'Shell_TrayWnd'):
            return False                                  # the desktop itself
        r = (ctypes.c_long * 4)(); u.GetWindowRect(ctypes.c_void_p(h), r)
        u.MonitorFromWindow.restype = ctypes.c_void_p
        mon = u.MonitorFromWindow(ctypes.c_void_p(h), 2)
        mi = (ctypes.c_ulong * 10)(); mi[0] = 40          # MONITORINFO: cbSize, rcMonitor, rcWork, dwFlags
        if not u.GetMonitorInfoW(ctypes.c_void_p(mon), mi):
            return False
        ml, mt, mr, mb = (ctypes.c_long * 4).from_buffer(mi, 4)
        return r[0] <= ml and r[1] <= mt and r[2] >= mr and r[3] >= mb
    except Exception:
        return False


def run_ui(cfg, runner_cls, save_cfg, test_fn, log_path=None):
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
        'grip_gain': tk.DoubleVar(value=cfg['grip_gain'] * 100), 'trigger_gain': tk.DoubleVar(value=cfg['trigger_gain'] * 100),
        'skip_center': tk.BooleanVar(value=cfg['skip_center']), 'foreground_exe': tk.StringVar(value=cfg['foreground_exe']),
        'mode': tk.StringVar(value=cfg['mode']), 'fusion': tk.StringVar(value=cfg['fusion']),
        'rumble_gain': tk.DoubleVar(value=cfg['rumble_gain'] * 100), 'speaker_mode': tk.StringVar(value={'仅大招/特殊技': '仅关键事件', '智能增强': '全部游戏声音'}.get(cfg['speaker_mode'], cfg['speaker_mode'])),
        'speaker_volume': tk.DoubleVar(value=cfg['speaker_volume'] * 100), 'record_native': tk.BooleanVar(value=cfg.get('record_native', True)),
        'event_fx': tk.BooleanVar(value=cfg.get('event_fx', True)), 'ww_vision': tk.BooleanVar(value=cfg.get('ww_vision', True)),
        'ww_click_gate': tk.BooleanVar(value=cfg.get('ww_click_gate', True)), 'game_profiles': tk.BooleanVar(value=cfg.get('game_profiles', True)), 'capture': tk.StringVar(value=cfg.get('capture', '自动')), 'speaker_scene': tk.BooleanVar(value=cfg.get('speaker_scene', True)), 'pbz_log_dir': tk.StringVar(value=cfg.get('pbz_log_dir', '')), 'mirror': tk.BooleanVar(value=cfg.get('mirror', False)),
    }
    PCT = ('grip_gain', 'trigger_gain', 'rumble_gain', 'speaker_volume', 'cinema_rumble', 'cinema_kick', 'cinema_trigger',
           'music_kick', 'music_tick', 'music_trigger')

    # ---- scenes: every usage scenario keeps its own complete setup
    SCENES = ('游戏', '影院', '音乐')
    STYLE_OF = {'游戏': '游戏', '影院': '影院', '音乐': '律动'}
    SCENE_DEF = {
        '游戏': dict(mode='原生透传', fusion='融合', grip_profile='均衡', trigger_profile='均衡冲击', grip_gain=1.0, trigger_gain=1.0,
                   rumble_gain=1.0, speaker_mode='原声', speaker_eq='原味', speaker_volume=0.6, capture='自动', skip_center=True),
        '影院': dict(mode='普通', fusion='融合', grip_profile='均衡', trigger_profile='均衡冲击', grip_gain=1.0, trigger_gain=0.8,
                   rumble_gain=1.0, speaker_mode='原声', speaker_eq='低音饱满', speaker_volume=0.7, capture='自动', skip_center=True),
        '音乐': dict(mode='普通', fusion='融合', grip_profile='均衡', trigger_profile='均衡冲击', grip_gain=1.0, trigger_gain=1.0,
                   rumble_gain=1.0, speaker_mode='原声', speaker_eq='低音饱满', speaker_volume=0.6, capture='自动', skip_center=False),
    }
    saved = dict(cfg.get('scenes') or {})
    if '游戏' not in saved:                                   # first run of the scene system: keep the user's old setup
        saved['游戏'] = {k: cfg.get(k, v) for k, v in SCENE_DEF['游戏'].items()}
    V['scene'] = tk.StringVar(value=cfg.get('scene', '游戏') if cfg.get('scene') in SCENES else '游戏')
    SV = {}
    for sc in SCENES:
        vals = dict(SCENE_DEF[sc]); vals.update(saved.get(sc, {}))
        vals['speaker_mode'] = SPEAKER_MODE_ALIAS.get(vals['speaker_mode'], vals['speaker_mode'])
        if vals['speaker_mode'] not in SPEAKER_MODES: vals['speaker_mode'] = '原声'
        if vals.get('speaker_eq') not in SPEAKER_EQS: vals['speaker_eq'] = '原味'
        SV[sc] = {k: (tk.DoubleVar(value=v * 100) if k in PCT else tk.BooleanVar(value=v) if isinstance(v, bool) else tk.StringVar(value=v))
                  for k, v in vals.items()}

    def SC(key):
        return SV[V['scene'].get()][key].get()

    def watch_vars():
        for k, v in V.items():
            v.trace_add('write', lambda *a: apply_live())
        for sc in SCENES:
            for v in SV[sc].values():
                v.trace_add('write', lambda *a: apply_live())

    def running():
        return bool(runner['t'] and runner['t'].is_alive())

    def collect():
        for k, v in V.items():
            cfg[k] = v.get() / 100 if k in PCT else v.get()
        cfg['scenes'] = {sc: {k: (v.get() / 100 if k in PCT else v.get()) for k, v in SV[sc].items()} for sc in SCENES}
        sc = V['scene'].get()
        cfg.update(cfg['scenes'][sc]); cfg['scene'] = sc; cfg['haptic_style'] = STYLE_OF[sc]   # the active scene drives the run
        save_cfg(cfg)

    NEEDS = {'原生透传': ('usbip', 'hidhide'), '接管': ('hidhide', 'vigem'), '普通': ()}

    def start():
        if running(): return
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
                    msgs.put('提示：影之刃零的事件特调缺少 UE4SS 或事件 mod（适配库 → 影之刃零卡片的齿轮里有安装说明）。其他功能照常启动。')
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
        if V['scene'].get() == sc:
            return
        V['scene'].set(sc)
        msgs.put(f'已切换到「{sc}」场景' + ('（已实时生效）' if running() else '，点右上角「启动」开始。'))

    def test():
        if running():
            msgs.put('请先停止再测试握把触感。'); return
        threading.Thread(target=lambda: msgs.put(test_fn()), daemon=True).start()

    # ---- header
    head = tk.Canvas(root, height=px(78), bg=BG, highlightthickness=0, bd=0); head.pack(fill='x')
    bigbtn = NeonButton(root, '▶  启动', toggle, w=170, h=46, color=CYAN, bg=BG, size=13, filled=True)
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
        head.create_text(px(80), px(28), text='声音触感', anchor='w', fill=TEXT, font=(ZH, 18, 'bold'))
        head.create_text(px(200), px(31), text='HAPTIC  LINK', anchor='w', fill=CYAN, font=(NUMB, 13))
        head.create_text(px(81), px(56), text=f'PS5 手柄触感中枢  ·  V{VERSION}  ·  USB 有线', anchor='w', fill=DIM, font=(ZH, 9))
        head.create_line(0, px(77), w, px(77), fill=LINE)
        head.create_line(0, px(77), px(240), px(77), fill=CYAN, width=px(2))
        for i in range(24):
            x = px(250) + i * px(9)
            head.create_line(x, px(77), x + px(4), px(77), fill=mix(BG, CYAN, 0.6 - i * 0.025), width=px(2))
        head.create_text(w - px(208), px(30), text='STATUS', anchor='e', fill=FAINT, font=(NUM, 8), tags='st')
        head.create_oval(0, 0, 0, 0, tags='dot', outline='')
        head.create_text(w - px(208), px(48), text='', anchor='e', font=(ZH, 11, 'bold'), tags='stt')
        head.coords(head.create_window(w - px(20), px(39), window=bigbtn, anchor='e'), w - px(20), px(39))
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

    for txt, fn, filled in (('忽略此版本', upd_skip, False), ('GitHub 下载', upd_manual, False), ('在线更新', upd_online, True)):
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
    NAV = [('总览', 'DASHBOARD', '◈'), ('适配库', 'LIBRARY', '⬢'), ('日志', 'LOG', '≡')]
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
        y = h - px(110)
        # author + project links: quiet, but always there (click opens the browser)
        side.create_text(px(6), y - px(40), text='作者  ' + about.AUTHOR, anchor='w', fill=FAINT, font=(ZH, 8))
        hv = cur.get('link')
        gx = side.create_text(px(6), y - px(20), text='GitHub', anchor='w', fill=CYAN if hv == 'gh' else DIM, font=(NUM, 8), tags='lnk_gh')
        bx = side.bbox(gx)
        side.create_text(bx[2] + px(6), y - px(20), text='·', anchor='w', fill=FAINT, font=(NUM, 8))
        side.create_text(bx[2] + px(16), y - px(20), text='哔哩哔哩', anchor='w', fill=CYAN if hv == 'bili' else DIM, font=(ZH, 8), tags='lnk_bili')
        side.create_text(px(6), y - px(60), text=f'v{about.VERSION}', anchor='w', fill=FAINT, font=(NUM, 8))
        side.create_text(px(52), y - px(60), text='检查更新', anchor='w', fill=CYAN if hv == 'upd' else DIM, font=(ZH, 8), tags='lnk_upd')
        side.create_line(px(4), y, px(150), y, fill=LINE)
        side.create_text(px(6), y + px(16), text='SYS', anchor='w', fill=FAINT, font=(NUM, 8))
        side.create_text(px(6), y + px(38), text='', anchor='w', fill=DIM, font=(MONO, 8), tags='clock')
        side.create_text(px(6), y + px(58), text='', anchor='w', fill=DIM, font=(MONO, 8), tags='up')
        side.create_text(px(6), y + px(78), text='', anchor='w', fill=DIM, font=(MONO, 8), tags='mode')
    side.bind('<Configure>', draw_side)

    def open_link(url):
        import webbrowser
        webbrowser.open(url)
    for tag, key, url in (('lnk_gh', 'gh', about.GITHUB_URL), ('lnk_bili', 'bili', about.BILIBILI_URL), ('lnk_upd', 'upd', None)):
        side.tag_bind(tag, '<Button-1>', (lambda e: check_update(manual=True)) if url is None else (lambda e, u=url: open_link(u)))
        # recolour only (a full redraw would recreate the item under the pointer -> Enter/Leave loop)
        side.tag_bind(tag, '<Enter>', lambda e, k=key, t=tag: (cur.update(link=k), side.configure(cursor='hand2'), side.itemconfigure(t, fill=CYAN)))
        side.tag_bind(tag, '<Leave>', lambda e, t=tag: (cur.update(link=None), side.configure(cursor=''), side.itemconfigure(t, fill=DIM)))
    side.bind('<Motion>', lambda e: (cur.update(hover=e.y // px(62) if e.y < len(NAV) * px(62) else None), draw_side()))
    side.bind('<Leave>', lambda e: (cur.update(hover=None), draw_side()))

    def show(name):
        cur['p'] = name
        for n, f in pages.items():
            f.pack_forget()
        pages[name].pack(fill='both', expand=True); draw_side()
    side.bind('<Button-1>', lambda e: show(NAV[e.y // px(62)][0]) if e.y < len(NAV) * px(62) else None)
    root._show = show                       # page switch hook (screenshots / scripted demos)
    root._start = start; root._switch_scene = switch_scene

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
            cv.configure(scrollregion=(0, 0, cv.winfo_width(), inner.winfo_reqheight())); draw_bar()

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
    row1 = tk.Frame(pg, bg=BG); row1.pack(fill='both', expand=True)
    p_grip = Panel(row1, '握把音圈', 'GRIP COILS'); p_grip.pack(side='left', fill='y')
    gm = GripMeter(p_grip.body); gm.pack(padx=px(6), pady=px(4), expand=True)
    p_scope = Panel(row1, '触感波形', 'WAVEFORM'); p_scope.pack(side='left', fill='both', expand=True, padx=px(10))
    scope = Scope(p_scope.body); scope.pack(fill='both', expand=True, pady=px(4))
    sb = tk.Frame(p_scope.body, bg=PANEL); sb.pack(fill='x')
    NeonButton(sb, '测试握把触感', test, w=140, h=32, size=10).pack(side='left')
    lvl_lb = tk.Label(sb, text='', bg=PANEL, fg=DIM, font=(MONO, 9)); lvl_lb.pack(side='right')
    p_trig = Panel(row1, '自适应扳机', 'TRIGGERS', accent=MAG); p_trig.pack(side='left', fill='y')
    tg_row = tk.Frame(p_trig.body, bg=PANEL); tg_row.pack(pady=px(4), expand=True)
    gl = TriggerGauge(tg_row, 'L'); gl.pack(side='left'); gr = TriggerGauge(tg_row, 'R'); gr.pack(side='left')
    tk.Label(p_trig.body, text='外圈 = 按压深度　内圈 = 扳机震动', bg=PANEL, fg=FAINT, font=(ZH, 8)).pack()

    row2 = tk.Frame(pg, bg=BG); row2.pack(fill='x', pady=(px(10), px(10)))
    p_info = Panel(row2, '链路状态', 'LINK'); p_info.pack(side='left', fill='both', expand=True)
    info = {k: InfoRow(p_info.body, k) for k in ('工作方式', '声源', '虚拟手柄', '扬声器', '场景', '游戏专属', '前台', '丢块')}
    for w_ in info.values(): w_.pack(fill='x', pady=px(3))
    p_ww = Panel(row2, '鸣潮技能识别', 'WW VISION', accent=GREEN); p_ww.pack(side='left', fill='y', padx=px(10))
    hexes = SkillHex(p_ww.body); hexes.pack(pady=px(6))
    p_ev = Panel(row2, '事件特调', 'EVENT FX', accent=AMBER); p_ev.pack(side='left', fill='both', expand=True)
    ev_box = tk.Text(p_ev.body, bg=PANEL, fg=TEXT, font=(ZH, 9), relief='flat', height=7, width=26, bd=0, highlightthickness=0, state='disabled', cursor='arrow')
    ev_box.pack(fill='both', expand=True); ev_box.tag_configure('t', foreground=FAINT); ev_box.tag_configure('n', foreground=AMBER)
    ev_state = {'last': None}

    # ---- 游戏: 鸣潮 / 影之刃零 / 通用 / 沉浸模式 / 系统检查
    gp = page('适配库', scroll=True); lock_note(gp)
    grid_view = tk.Frame(gp, bg=BG); grid_view.pack(fill='x')
    details = {}

    def detail(key, title, accent):
        f = tk.Frame(gp, bg=BG); details[key] = f
        hd = tk.Frame(f, bg=BG); hd.pack(fill='x', pady=(0, px(8)))
        NeonButton(hd, '‹  返回适配库', show_grid, w=140, h=34, size=10, bg=BG).pack(side='left')
        tk.Label(hd, text=title + '  ·  设置', bg=BG, fg=accent, font=(ZH, 13, 'bold')).pack(side='left', padx=px(14))
        return f

    def show_grid():
        for f in details.values(): f.pack_forget()
        grid_view.pack(fill='x')

    def show_detail(key):
        grid_view.pack_forget()
        for f in details.values(): f.pack_forget()
        details[key].pack(fill='x')

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
    Toggle(p.body, '鸣潮事件特调', V['ww_fx'], '大招、声骸释放和共鸣回路特殊技时：握把专属震动（扬声器不做事件适配，按原声播放）', color=GREEN, wrap=820).pack(fill='x', pady=px(4))

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
        d = filedialog.askdirectory(title='选择 PS5EventLog 的 logs 文件夹')
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

    p = module('通用游戏', 'GENERAL', CYAN, '没有专门适配的游戏也能用：在「游戏」场景下自动截前台游戏的声音转成触感。下面是对所有游戏生效的设置。')
    fr = tk.Frame(p.body, bg=PANEL); fr.pack(fill='x', pady=px(4))
    tk.Label(fr, text='只在这个游戏在前台时工作', bg=PANEL, fg=TEXT, font=(ZH, 10)).pack(side='left')
    tk.Entry(fr, textvariable=V['foreground_exe'], width=30, bg=PANEL2, fg=CYAN, insertbackground=CYAN, relief='flat', font=(MONO, 10),
             highlightthickness=1, highlightbackground=LINE2, highlightcolor=CYAN).pack(side='left', padx=px(12), ipady=px(4))
    tk.Label(fr, text='exe 名，留空 = 始终工作', bg=PANEL, fg=FAINT, font=(ZH, 9)).pack(side='left')
    Toggle(p.body, '录制调校数据', V['record_native'], '原生透传时把游戏的音圈强度、扳机指令和鸣潮识别结果记到 traces 文件夹，反馈问题时附上它', wrap=820).pack(fill='x', pady=px(4))

    SCENE_INFO = {
        '游戏': (CYAN, 'impact', '玩游戏：只把打击、碰撞、爆炸变成触感；支持 PS5 手柄的游戏保留原生扳机和音圈；鸣潮、影之刃零有专属适配。'),
        '影院': (AMBER, 'cinema', '看电影 / 剧集：低频隆隆、爆炸厚重；扬声器播放原声。不需要虚拟手柄，自动截播放器或浏览器的声音。'),
        '音乐': (MAG, 'rhythm', '听音乐 / 音游：握把和扳机跟着鼓点律动；扬声器可播放原声。也适合给没有手柄震动的游戏「加氛围」。'),
    }
    ROUTING = [('原生透传', '原生透传', '', '游戏看到虚拟PS5手柄：保留原生自适应扳机和音圈触感，再叠加声音触感。适合支持PS5手柄的游戏。'),
               ('接管', '接管', '', '游戏看到虚拟PS4手柄：游戏震动与声音触感融合。原生透传不可用时的备用。'),
               ('普通', '普通', '', '不创建虚拟手柄，只叠加声音触感和扬声器。看电影、听音乐、不支持PS5手柄的游戏用这个。')]

    def scene_settings(sc):
        acc, _art, tag = SCENE_INFO[sc]; sv = SV[sc]
        f = detail(sc, sc + '场景', acc)
        tk.Label(f, text=tag, bg=BG, fg=DIM, font=(ZH, 9), anchor='w', justify='left', wraplength=px(940)).pack(fill='x', pady=(0, px(8)))
        pnl = Panel(f, '连接方式', 'ROUTING', accent=acc); pnl.pack(fill='x', pady=(0, px(10)))
        rec = '原生透传' if sc == '游戏' else '普通'
        ModeCards(pnl.body, sv['mode'], [(v, t, '推荐' if v == rec else '', d) for v, t, _g, d in ROUTING]).pack(fill='x', pady=(px(4), px(8)))
        if sc == '游戏':
            fr = tk.Frame(pnl.body, bg=PANEL); fr.pack(fill='x')
            tk.Label(fr, text='虚拟手柄时触感来源', bg=PANEL, fg=TEXT, font=(ZH, 10)).pack(side='left', padx=(0, px(14)))
            Segmented(fr, sv['fusion'], ('融合', '仅游戏震动', '仅声音触感'), cell=124, rec='融合').pack(side='left')
            tk.Label(pnl.body, text='仅游戏震动 = 完全用游戏自己设计的原生触感（索尼第一方游戏推荐）；融合 = 原生触感 + 声音触感。',
                     bg=PANEL, fg=FAINT, font=(ZH, 9), anchor='w').pack(fill='x', pady=(px(4), 0))
        cols = tk.Frame(f, bg=BG); cols.pack(fill='x', pady=(0, px(10)))
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
        pnl = Panel(cols, '风格', 'PROFILE', accent=acc); pnl.pack(side='left', fill='both', expand=True, padx=(px(10), 0))
        tk.Label(pnl.body, text='握把风格', bg=PANEL, fg=TEXT, font=(ZH, 10), anchor='w').pack(fill='x')
        Segmented(pnl.body, sv['grip_profile'], GRIP_PROFILES, cell=116, rec='均衡').pack(anchor='w', pady=(px(4), px(10)))
        tk.Label(pnl.body, text='扳机风格', bg=PANEL, fg=TEXT, font=(ZH, 10), anchor='w').pack(fill='x')
        Segmented(pnl.body, sv['trigger_profile'], TRIGGER_PROFILES, cell=116, color=MAG, rec='均衡冲击').pack(anchor='w', pady=(px(4), px(10)))
        Toggle(pnl.body, '忽略中置声道', sv['skip_center'], '5.1/7.1 时对白（中置）不触发震动', wrap=300).pack(fill='x', pady=px(4))
        pnl = Panel(f, '手柄扬声器', 'SPEAKER', accent=BLUE); pnl.pack(fill='x', pady=(0, px(10)))
        fr = tk.Frame(pnl.body, bg=PANEL); fr.pack(fill='x')
        tk.Label(fr, text='播放', bg=PANEL, fg=TEXT, font=(ZH, 10), width=6, anchor='w').pack(side='left')
        Segmented(fr, sv['speaker_mode'], SPEAKER_MODES, cell=124, color=BLUE, rec='原声').pack(side='left')
        fr = tk.Frame(pnl.body, bg=PANEL); fr.pack(fill='x', pady=(px(8), 0))
        tk.Label(fr, text='音色', bg=PANEL, fg=TEXT, font=(ZH, 10), width=6, anchor='w').pack(side='left')
        Segmented(fr, sv['speaker_eq'], SPEAKER_EQS, cell=124, color=BLUE, rec='原味' if sc == '游戏' else '低音饱满').pack(side='left')
        Slider(pnl.body, '扬声器音量', sv['speaker_volume'], 0, 100, color=BLUE).pack(fill='x', pady=(px(8), 0))
        tk.Label(pnl.body, justify='left', anchor='w', bg=PANEL, fg=DIM, font=(ZH, 9), wraplength=px(900), text=(
            '原声：手柄扬声器完整播放声音，不做动态处理，只按「音色」做 EQ。原味 = 不改；低音饱满 = 用倍频补出小喇叭放不出的低音；人声清晰 = 突出人声和金属声。\n'
            '特殊事件：像索尼第一方游戏那样，平时安静，只在关键时刻出声。支持手柄扬声器的游戏（如影之刃零）在原生透传下直接播放游戏自带的手柄扬声器音效，和 PS5 上一样；'
            '其他游戏、电影、音乐里会自动按「原声」播放。\n'
            '两种模式下，关键时刻都会按事件换音色并放大一段。手柄插了耳机时从耳机出声。')).pack(fill='x', pady=(px(8), 0))
        pnl = Panel(f, '声音来源', 'SOURCE', accent=acc); pnl.pack(fill='x', pady=(0, px(10)))
        fr = tk.Frame(pnl.body, bg=PANEL); fr.pack(fill='x')
        Segmented(fr, sv['capture'], ('自动', '系统混音'), cell=124, rec='自动').pack(side='left')
        tk.Label(fr, text=('自动：先截已适配的游戏，其次截前台正在出声的程序' if sc == '游戏' else '自动：截当前正在出声的播放器 / 浏览器 / 音乐软件') +
                 '（按进程截音，开着杜比全景声也能用）；系统混音：截整个系统的声音。',
                 bg=PANEL, fg=DIM, font=(ZH, 9), justify='left', anchor='w', wraplength=px(680)).pack(side='left', padx=px(14))
    for sc in SCENES:
        scene_settings(sc)

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
    row = section('使用场景', 'SCENES  ·  三选一，点击切换（运行中会自动重启），齿轮里是这个场景的全部设置')
    cards = {}
    for key, sub in (('游戏', '触感 · 原生扳机 · 游戏专属'), ('影院', '低频隆隆 · 扬声器原声'), ('音乐', '鼓点律动 · 扬声器原声')):
        acc, art, _t = SCENE_INFO[key]
        cards[key] = GameCard(row, key + '场景', sub, art=art, accent=acc, on_text='使用中', off_text='点击切换',
                              is_on=lambda k=key: V['scene'].get() == k,
                              badge=(lambda k=key: ('运行中', GREEN) if (running() and V['scene'].get() == k) else None),
                              on_click=lambda k=key: switch_scene(k), on_gear=lambda k=key: show_detail(k))
        cards[key].pack(side='left', padx=(0, px(16)))
    row = section('游戏适配', 'ADAPTATIONS  ·  游戏场景下生效，点击开启 / 关闭，齿轮进入设置')
    cards['鸣潮'] = GameCard(row, '鸣潮', '读屏技能识别 · 自适应扳机 · 事件特调', art='impact', accent=GREEN,
                           is_on=lambda: V['ww_enabled'].get(), badge=lambda: ('运行中', GREEN) if games_on['ww'] else None,
                           on_click=lambda: toggle(V['ww_enabled']), on_gear=lambda: show_detail('鸣潮'))
    cards['影之刃零'] = GameCard(row, '影之刃零', '格挡 · 闪避 · 受击事件特调', art='impact', accent=MAG,
                             is_on=lambda: V['pbz_fx'].get(), badge=lambda: ('运行中', GREEN) if games_on['pbz'] else None,
                             on_click=lambda: toggle(V['pbz_fx']), on_gear=lambda: show_detail('影之刃零'))
    cards['通用'] = GameCard(row, '通用游戏', '所有游戏 · 声音触感 · 原生透传', art='general', accent=CYAN,
                           is_on=lambda: True, on_text='始终开启', on_click=lambda: show_detail('通用游戏'), on_gear=lambda: show_detail('通用游戏'))
    for c in ('鸣潮', '影之刃零', '通用'):
        cards[c].pack(side='left', padx=(0, px(16)))
    def redraw_cards(*a):
        for c in cards.values(): c.redraw()
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
    pg = grid_view

    p = Panel(pg, '联动其他软件 · 音频镜像', 'MIRROR', accent=GREEN); p.pack(fill='x', pady=(0, px(10)))
    Toggle(p.body, '音频镜像（给键盘 / 灯带的音乐律动用）', V['mirror'],
           '开着杜比全景声时，XsEngine（EchoRGB）等灯效软件的音乐律动听不到声音。打开后，正在播放的声音（游戏、音乐、视频）会复制一份到'
           '「Steam Streaming Speakers」（听不到的虚拟设备）；再在灯效软件的设置里把音频设备选成「Steam Streaming Speakers」即可。'
           '不需要点「启动」，打开本软件就在工作。', color=GREEN, wrap=820).pack(fill='x', pady=px(4))
    mirror_lb = tk.Label(p.body, text='', bg=PANEL, fg=DIM, font=(ZH, 9), anchor='w'); mirror_lb.pack(fill='x', pady=(px(4), 0))
    mirror = {'m': None}

    def mirror_sync(*_a):
        from mirror import AudioMirror
        on = V['mirror'].get(); m = mirror['m']
        if on and (m is None or not m.is_alive()):
            mirror['m'] = AudioMirror(msgs.put); mirror['m'].start()
        elif not on and m is not None:
            m.stop(); mirror['m'] = None; msgs.put('音频镜像已关闭。')

    def mirror_status():
        m = mirror['m']
        if m is None:
            mirror_lb.configure(text='未开启', fg=FAINT)
        elif m.error:
            mirror_lb.configure(text='出错：' + m.error, fg=MAG)
        else:
            mirror_lb.configure(text=f"镜像中 · 来源 {m.source or '等待有程序发声'} · 电平 {m.level:.2f}", fg=GREEN if m.level > 0.01 else DIM)
        root.after(500, mirror_status)
    V['mirror'].trace_add('write', mirror_sync)
    root.after(1000, mirror_sync); root.after(1500, mirror_status)

    p = Panel(pg, '系统检查', 'SYSTEM', accent=AMBER); p.pack(fill='x')
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
            add_log('缺少依赖：' + '、'.join(m[1] for m in miss) + '。点「适配库」页的「一键安装依赖」，或运行软件旁边的「依赖.exe」。')
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
    intel_lb.pack(fill='x', pady=(px(8), 0))

    # ---- 日志
    pg = page('日志')
    p = Panel(pg, '运行日志', 'CONSOLE'); p.pack(fill='both', expand=True, pady=(0, px(10)))
    log = tk.Text(p.body, bg=PANEL, fg=TEXT, font=(ZH, 9), relief='flat', bd=0, highlightthickness=0, wrap='word', insertbackground=CYAN)
    log.pack(fill='both', expand=True); log.tag_configure('t', foreground=CYAN); log.tag_configure('e', foreground=MAG)

    # ---- footer
    foot = tk.Canvas(root, height=px(30), bg=BG, highlightthickness=0, bd=0); foot.pack(fill='x', side='bottom')
    fstate = {'msg': '未启动。PS5 手柄需要 USB 有线连接。'}

    def draw_foot():
        foot.delete('all'); w = foot.winfo_width()
        foot.create_line(0, 1, w, 1, fill=LINE)
        foot.create_text(px(16), px(16), text='▸', fill=CYAN, font=(ZH, 9), anchor='w')
        msg = fstate['msg']; room = max(10, int((w - px(420)) / px(12)))      # keep clear of the right-hand label
        if len(msg) > room:
            msg = msg[:room - 1] + '…'
        foot.create_text(px(32), px(16), text=msg, fill=DIM, font=(ZH, 9), anchor='w')
        foot.create_text(w - px(16), px(16), text='DUALSENSE · VOICE COIL · ADAPTIVE TRIGGER', fill=FAINT, font=(NUM, 8), anchor='e')

    def add_log(m):
        ts = time.strftime('%H:%M:%S')
        log.insert('end', ts + '  ', 't'); log.insert('end', m + '\n', 'e' if ('出错' in m or '失败' in m) else ''); log.see('end')
        fstate['msg'] = m.split('\n')[0][:120]; draw_foot()
        if log_path:                     # same log on disk (traces/app-log.txt) for diagnosis
            try:
                with open(log_path, 'a', encoding='utf8') as f:
                    f.write(time.strftime('%Y-%m-%d ') + ts + '  ' + m + '\n')
            except Exception:
                pass

    show('总览'); t_start = {'t': None}; last = {'run': None}
    watch_vars()
    MODE_EN = {'原生透传': 'NATIVE', '接管': 'TAKEOVER', '普通': 'OVERLAY'}

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
        stopping = run and r.stop_ev.is_set()
        if stopping: bigbtn.config_look(text='…  停止中', color=AMBER)
        w = hstate['w']; pulse = 0.5 + 0.5 * math.sin(time.time() * 5)
        if not slow:
            return fast_part(run, s)
        col = AMBER if stopping else (GREEN if run else FAINT)
        head.itemconfigure('stt', text='停止中' if stopping else (f"运行中 · {cfg.get('scene', '游戏')}场景" if run else f"待机 · {V['scene'].get()}场景"), fill=col)
        d = px(5) + (px(2) * pulse if run else 0); x, y = w - px(196), px(48)
        head.coords('dot', x - d, y - d, x + d, y + d); head.itemconfigure('dot', fill=mix(BG, col, 0.5 + 0.5 * pulse) if run else FAINT)
        # side panel clock
        side.itemconfigure('clock', text=time.strftime('%H:%M:%S'))
        up = int(time.time() - t_start['t']) if t_start['t'] else 0
        side.itemconfigure('up', text=f'UP  {up // 3600:02d}:{up // 60 % 60:02d}:{up % 60:02d}' if run else 'UP  --:--:--')
        side.itemconfigure('mode', text='MODE ' + MODE_EN.get(r.start_cfg.get('mode', '') if run else SC('mode'), ''))
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
            info['声源'].set(('⚠ 截不到游戏声音（关闭空间音效/独占模式）  ' if s.get('silent') else '') + (s.get('source') or '—'),
                           MAG if s.get('silent') else TEXT)
            vp = s.get('vpad')
            info['虚拟手柄'].set(f'输入转发 {vp[0]}  ·  反馈包 {vp[1]}' if vp else '—', CYAN if vp else DIM)
            spv = s.get('speaker', 0.0)
            info['扬声器'].set(('关闭' if cfg.get('speaker_mode') == '关闭' else f"{s.get('spk_eff') or cfg.get('speaker_mode')}"
                               f"{'（未适配游戏，自动原声）' if cfg.get('speaker_mode') == '特殊事件' and s.get('spk_eff') == '原声' else ''}  ·  电平 {'▮' * int(min(1, spv) * 12):<12} {spv:.2f}  ·  延迟 {s.get('lag_ms', 0)} ms  ·  断流 {s.get('xruns', 0)}") if run else '—',
                             CYAN if spv > 0.02 else DIM)
            scn = cfg.get('scene', '游戏') if run else V['scene'].get()
            info['场景'].set(f"{scn}  ·  扬声器 {cfg.get('speaker_mode', '') if run else SC('speaker_mode')}"
                             f"（{cfg.get('speaker_eq', '') if run else SC('speaker_eq')}）", CYAN)
            info['游戏专属'].set(s.get('profile') or '无（通用）', GREEN if s.get('profile') else DIM)
            info['前台'].set(('工作中' if s.get('active') else '等待目标游戏到前台') if run else '—', TEXT if s.get('active') else AMBER)
            info['丢块'].set(str(s.get('late', 0)) if run else '—', MAG if s.get('late', 0) > 50 else TEXT)
            ww = s.get('ww')
            if s.get('profile') and ww:
                note = f'截图 {ww[1]} 帧' + (f'  错误 {ww[2]}' if ww[2] else '')
                hexes.update_vals(ww[0], note)
            else:
                hexes.update_vals(None, '切到鸣潮后自动识别技能栏' if run else '未启动')
            fxl = s.get('fx_last')
            if fxl and fxl != ev_state['last']:
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
        if mirror['m'] is not None:
            mirror['m'].stop()
        if runner['t']:
            runner['t'].join(8)          # takeover needs a moment to un-hide the real pad
        root.destroy()
    root.protocol('WM_DELETE_WINDOW', close); pump()
    add_log('就绪。PS5 手柄请用 USB 有线连接，然后点右上角「启动」。')
    root.after(800, lambda: check_system(auto=True))
    return root
