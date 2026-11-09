# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = []
hiddenimports += collect_submodules('scipy.ndimage')
hiddenimports += collect_submodules('dxcam') + ['comtypes', 'comtypes.client', 'onnxruntime', 'ww_ocr', 'audio_default', 'ui_scifi', 'proc_loopback', 'deps', 'sysfix', 'covers', 'PIL.ImageTk', 'app_icon', 'audio_sessions', 'pad_speaker', 'about', 'updater', 'i18n', 'audio_volume', 'button_haptics', 'pad_input']
import os, onnxocr
_ocr = os.path.join(os.path.dirname(onnxocr.__file__), 'models', 'ppocrv5')
ocr_datas = [(os.path.join(_ocr, 'rec', 'rec.onnx'), 'onnxocr/models/ppocrv5/rec'),
             (os.path.join(_ocr, 'ppocrv5_dict.txt'), 'onnxocr/models/ppocrv5')]


a = Analysis(
    ['ps5_haptics_app.py'],
    pathex=[],
    binaries=[],
    datas=ocr_datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PS5Haptics',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon='release/app.ico',
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PS5Haptics',
)
