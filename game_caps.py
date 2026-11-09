"""Remembers, per game, whether it drives the controller speaker or adaptive triggers itself.

A game shows this only by using them, so we learn at the first use and step aside from then on,
including on later launches. Stored next to the program in game_caps.json.
"""
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else pathlib.Path(__file__).resolve().parent
NOT_GAMES = {'', 'explorer.exe', 'ps5haptics.exe', 'viiperhost.exe', 'python.exe', 'pythonw.exe', 'searchhost.exe',
             'shellexperiencehost.exe', 'startmenuexperiencehost.exe', 'applicationframehost.exe',
             'chrome.exe', 'msedge.exe', 'firefox.exe', 'steam.exe', 'steamwebhelper.exe', 'discord.exe'}   # often in front of a game


class GameCaps:
    def __init__(self, path=None):
        self.path = pathlib.Path(path) if path else ROOT / 'game_caps.json'
        self.data = {}
        try:
            loaded = json.loads(self.path.read_text('utf8'))
            if isinstance(loaded, dict):
                self.data = {str(k): set(v) for k, v in loaded.items() if isinstance(v, list)}
        except (OSError, ValueError):
            pass                         # first run or a damaged file: simply learn again

    def has(self, exe, cap):
        return cap in self.data.get(exe, ())

    def note(self, exe, cap):
        """True the first time this game is seen using cap."""
        if exe in NOT_GAMES or self.has(exe, cap):
            return False
        self.data.setdefault(exe, set()).add(cap)
        self._save()
        return True

    def _save(self):
        tmp = self.path.with_name(self.path.name + '.tmp')
        try:
            tmp.write_text(json.dumps({k: sorted(v) for k, v in self.data.items()}, ensure_ascii=False, indent=1), 'utf8')
            os.replace(tmp, self.path)
        except OSError:
            pass                         # read-only folder: it still works for this session

    def reset(self):
        self.data = {}
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass
