# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['app\\cli.py'],
    pathex=[],
    binaries=[],
    datas=[('app/web/templates', 'app/web/templates'), ('app/web/static', 'app/web/static'), ('app/licensing/public_key.b64', 'app/licensing')],
    hiddenimports=['uvicorn', 'uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.websockets.auto', 'fastapi', 'jinja2', 'cryptography', 'cryptography.fernet', 'apscheduler', 'apscheduler.triggers.cron', 'httpx'],
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
    name='agent-cli',
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
    name='agent-cli',
)
