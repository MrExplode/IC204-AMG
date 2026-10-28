"""Assemble dist/ic204amg-win32 (and its .zip): ic204amg.cmd and car.example.toml at the top,
a 32-bit embeddable CPython with udsoncan and ic204amg in python/.

Stdlib only, runs anywhere. The laptop then needs nothing installed: unzip and run ic204amg.cmd.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

PY_URL = "https://www.python.org/ftp/python/3.15.1/python-3.15.1-embed-win32.zip"
PY_SHA = "ef9ed8348e9b752c54503d494b8a42efd7cf115082ca88a778735bddba967187"
WHL_URL = "https://files.pythonhosted.org/packages/9b/99/5c79ce34af274fe20c4511ecf792ed2d868ad64a2406351f20df7f2b33fd/udsoncan-1.26.1-py3-none-any.whl"
WHL_SHA = "c1bd6cc54d6c1124c0eafeff2ffaab48bfeb7d0d2576959f21840c37e96dfb9a"

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "dist" / "cache"
OUT = ROOT / "dist" / "ic204amg-win32"
PY = OUT / "python"
ZIP = OUT.with_suffix(".zip")
# car.toml, logs/ and backups/ live next to the launcher whatever the caller's cwd is; setlocal
# gives the caller its own cwd back.
LAUNCHER = (
    b'@echo off\r\nsetlocal\r\ncd /d "%~dp0"\r\n'
    b'"%~dp0python\\python.exe" -m ic204amg %*\r\nexit /b %ERRORLEVEL%\r\n'
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch(url: str, sha: str) -> Path:
    dest = CACHE / url.rsplit("/", 1)[1]
    if dest.exists() and sha256(dest) == sha:
        return dest
    CACHE.mkdir(parents=True, exist_ok=True)
    print("downloading", url)
    with urllib.request.urlopen(url) as response, dest.open("wb") as f:
        shutil.copyfileobj(response, f)
    if (got := sha256(dest)) != sha:
        dest.unlink()
        sys.exit(f"sha256 mismatch for {url}: {got}")
    return dest


def main() -> None:
    py, whl = fetch(PY_URL, PY_SHA), fetch(WHL_URL, WHL_SHA)
    if OUT.exists():
        shutil.rmtree(OUT)
    PY.mkdir(parents=True)
    with zipfile.ZipFile(py) as z:
        z.extractall(PY)
    with zipfile.ZipFile(whl) as z:
        z.extractall(PY, [n for n in z.namelist() if n.startswith("udsoncan/")])
    shutil.copytree(
        ROOT / "ic204amg", PY / "ic204amg", ignore=shutil.ignore_patterns("__pycache__")
    )
    (OUT / "ic204amg.cmd").write_bytes(LAUNCHER)
    for name in ("LICENSE", "car.example.toml", "car.toml"):
        if (ROOT / name).exists():
            shutil.copy(ROOT / name, OUT / name)
    files = sorted(p for p in OUT.rglob("*") if p.is_file())
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, f"{OUT.name}/{p.relative_to(OUT).as_posix()}")
    size = sum(p.stat().st_size for p in files)
    print(f"bundle ready: {OUT} ({size / 1e6:.1f} MB), {ZIP} ({ZIP.stat().st_size / 1e6:.1f} MB)")
    print(f"deploy: unzip {ZIP.name} on the laptop, or scp -r {OUT} <laptop>:C:/work/amg/")


if __name__ == "__main__":
    main()
