# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('frontend/dist', 'frontend/dist'), ('app/web/static', 'app/web/static'), ('app/licensing/public_key.b64', 'app/licensing'), ('packaging/python-embed-win64.zip', 'packaging'), ('packaging/get-pip.py', 'packaging')]
binaries = []
hiddenimports = ['uvicorn', 'uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.websockets.auto', 'fastapi', 'jinja2', 'cryptography', 'cryptography.fernet', 'apscheduler', 'apscheduler.triggers.cron', 'httpx']
tmp_ret = collect_all('app')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['app\\cli.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'torchvision', 'torchaudio', 'transformers', 'scipy', 'matplotlib', 'pandas', 'sklearn', 'scikit-learn', 'librosa', 'numba', 'soundfile', 'onnxruntime', 'tensorflow', 'pyarrow', 'IPython', 'jedi', 'parso', 'pytest', 'nbformat', 'jsonschema', 'lark', 'modelscope', 'lightning', 'hydra', 'altair'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='media-agent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
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
    name='media-agent',
)
