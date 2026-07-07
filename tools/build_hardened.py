"""Hardening helper (UNTESTED scaffold) — obfuscate/compile the license core.

Client-side protection is best-effort (a determined attacker can still patch a
Python app). This raises the bar by compiling/obfuscating the modules that
enforce licensing so they aren't trivially editable.

Two options — run in YOUR build environment (needs a C compiler / tool):

  # A) PyArmor (recommended; simplest). Obfuscates the whole package.
  pip install pyarmor
  pyarmor gen -O dist_obf app/licensing app/web/server.py
  #   then package dist_obf/ instead of the plain sources.

  # B) Cython — compile the critical modules to native .pyd/.so.
  pip install cython
  python tools/build_hardened.py cythonize
  #   compiles app/licensing/{verify,manager,integrity}.py in place.

Always run `licctl keygen` first so integrity.py pins your public key before
compiling, otherwise the tamper check stays disabled.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_HARDEN = ["app/licensing/verify.py", "app/licensing/manager.py",
           "app/licensing/integrity.py"]


def cythonize() -> None:
    try:
        from Cython.Build import cythonize as _cy   # noqa: F401
    except ImportError:
        sys.exit("Cython not installed: pip install cython")
    # Delegate to a throwaway setup so the .py compile to native extensions.
    setup_py = _ROOT / "_hardened_setup.py"
    setup_py.write_text(
        "from setuptools import setup\n"
        "from Cython.Build import cythonize\n"
        f"setup(ext_modules=cythonize({_HARDEN!r}, language_level=3))\n",
        encoding="utf-8")
    subprocess.check_call([sys.executable, str(setup_py),
                           "build_ext", "--inplace"], cwd=str(_ROOT))
    print("cythonized:", ", ".join(_HARDEN))
    print("NOTE: verify the app still runs, then ship the .pyd/.so and remove "
          "the corresponding .py from the distribution.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "cythonize":
        cythonize()
    else:
        print(__doc__)
