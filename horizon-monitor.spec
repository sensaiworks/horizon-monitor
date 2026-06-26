# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — build a portable Windows .exe.

    .venv\\Scripts\\Activate.ps1
    pip install pyinstaller
    pyinstaller horizon-monitor.spec        # output: dist\\horizon-monitor.exe

The result is a single windowed .exe. It reads config.toml / .env from beside itself
(auto-creating config.toml from the bundled template on first run). The build EXCLUDES
torch / sentence-transformers, so RAG embeddings use Voyage (set VOYAGE_API_KEY); local
offline embeddings remain a run-from-source option.
"""

from PyInstaller.utils.hooks import collect_all, collect_submodules

# Templates bundled so the app can seed config.toml on first run.
datas = [
    ("config.example.toml", "."),
    (".env.example", "."),
    ("README.md", "."),
]
binaries = []
hiddenimports = []

# chromadb loads many submodules + data files lazily — gather the whole package.
_d, _b, _h = collect_all("chromadb")
datas += _d
binaries += _b
hiddenimports += _h
hiddenimports += collect_submodules("chromadb")

hiddenimports += [
    "voyageai",
    "anthropic",
    "mcp",
    "imagehash",
    "plyer.platforms.win.notification",
    "pystray._win32",
]

# Heavy libs we deliberately don't ship (trims well over a GB). The exe uses Voyage
# embeddings; local sentence-transformers/torch stay a run-from-source path.
excludes = [
    "torch", "torchvision", "torchaudio",
    "sentence_transformers", "transformers",
    "scipy", "matplotlib", "tensorflow",
    "IPython", "notebook", "pandas",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="horizon-monitor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,            # windowed: no console flash for the tray/GUI app
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
