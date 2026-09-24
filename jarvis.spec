# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_data_files

BASE = Path(r"C:\Users\DK_ART\Documents\MultiTool\HomeChats\Chat-12")
VOSK_MODEL = Path(r"C:\Users\DK_ART\AppData\Local\Temp\gigatool\vosk-model\vosk-model-small-ru-0.22")
SPK_MODEL = Path(r"C:\Users\DK_ART\AppData\Local\Temp\gigatool\vosk-model-spk-0.4")

block_cipher = None

# Vosk: собрать DLL (libvosk.dll) и данные как реальные файлы на диск (_internal\vosk),
# иначе os.add_dll_directory() в vosk падает при запуске из-под PyInstaller.
vosk_binaries, vosk_datas, vosk_hidden = collect_dynamic_libs("vosk"), collect_data_files("vosk"), []
# Модель Vosk — как дерево файлов в бандл.
vosk_tree = Tree(str(VOSK_MODEL), prefix="vosk-model-small-ru-0.22")
# Модель говорящего (spk) — включается в бандл только если скачана локально.
spk_tree = Tree(str(SPK_MODEL), prefix="vosk-model-spk-0.4") if SPK_MODEL.is_dir() else []

a = Analysis(
    [str(BASE / "jarvis_bridge.py")],
    pathex=[],
    binaries=vosk_binaries,
    datas=vosk_datas + [
        (str(BASE / "jarvis_avatar.py"), "."),
        (str(BASE / "dim.ps1"), "."),
        (str(BASE / "jarvis_avatar_3d.html"), "."),
        (str(BASE / "three147.min.js"), "."),
        (str(BASE / "GLTFLoader147.js"), "."),
        (str(BASE / "robot.glb"), "."),
        (str(BASE / "*.jpg"), "."),
        (str(BASE / "*.png"), "."),
    ],
    hiddenimports=[
        "vosk",
        "sounddevice",
        "pyautogui",
        "pygetwindow",
        "pyperclip",
        "edge_tts",
        "mss",
        "PIL",
        "PIL._imaging",
        "numpy",
        "pycaw",
        "comtypes",
    ],
    excludes=[
        "tkinter", "matplotlib", "scipy", "pandas",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas + vosk_tree + spk_tree,
    [],
    name="Jarvis",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
)