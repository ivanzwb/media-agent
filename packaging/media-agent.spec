# PyInstaller spec — UNTESTED scaffold. Build in your own environment:
#   pip install pyinstaller
#   pyinstaller packaging/media-agent.spec
# Produces dist/media-agent(.exe). Bundles templates/static/public_key and the
# dynamic imports of the FastAPI/uvicorn/trafilatura/lxml/cryptography stack.
# You will likely need to tweak hiddenimports/datas for your platform.
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [
    ("app/web/templates", "app/web/templates"),
    ("app/web/static", "app/web/static"),
    ("app/licensing/public_key.b64", "app/licensing"),
    ("packaging/cosyvoice_worker.py", "packaging"),
]
hiddenimports = []
d, _b, h = collect_all("app")
datas += d
hiddenimports += h
hiddenimports += collect_submodules("uvicorn")
for pkg in ("trafilatura", "lxml", "cryptography", "readability",
            "feedparser", "fastapi", "starlette", "frontmatter", "resvg_py"):
    d, _b, h = collect_all(pkg)
    datas += d
    hiddenimports += h

a = Analysis(
    ["packaging/run_app.py"],
    pathex=["."],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    excludes=["pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="media-agent",
    console=True,          # keep a console for logs; set False for windowed
    upx=False,
)
