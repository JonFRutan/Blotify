#!/usr/bin/env python3
"""
Builds the Blotify FastAPI server into a standalone binary using PyInstaller.
Output goes to resources/bin/{platform}/server[.exe]

Run via:  python scripts/build-server.py
Or:       npm run build-server
"""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
PLAT = sys.platform           # linux | darwin | win32
EXT  = '.exe' if PLAT == 'win32' else ''
OUT_DIR = ROOT / 'resources' / 'bin' / PLAT


def run(cmd, **kw):
    print('$', ' '.join(str(c) for c in cmd))
    subprocess.run(cmd, check=True, **kw)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Ensure PyInstaller is available in the active environment
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print('PyInstaller not found — installing…')
        run([sys.executable, '-m', 'pip', 'install', 'pyinstaller'])

    # Separator is ; on Windows, : elsewhere
    sep = ';' if PLAT == 'win32' else ':'

    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--noconfirm',
        '--clean',
        '--onedir',                                  # faster startup than --onefile
        '--name', 'server',
        '--distpath', str(ROOT / 'build' / 'pyinst'),
        '--workpath', str(ROOT / 'build' / 'pyinst_work'),
        '--specpath', str(ROOT / 'build'),
        # Bundle static/ directory so the server works without external files
        f'--add-data=static{sep}static',
        # FastAPI / uvicorn hidden imports
        '--hidden-import=uvicorn.logging',
        '--hidden-import=uvicorn.loops',
        '--hidden-import=uvicorn.loops.none',
        '--hidden-import=uvicorn.loops.auto',
        '--hidden-import=uvicorn.protocols',
        '--hidden-import=uvicorn.protocols.http',
        '--hidden-import=uvicorn.protocols.http.auto',
        '--hidden-import=uvicorn.protocols.http.h11_impl',
        '--hidden-import=uvicorn.protocols.http.httptools_impl',
        '--hidden-import=uvicorn.protocols.websockets',
        '--hidden-import=uvicorn.protocols.websockets.auto',
        '--hidden-import=uvicorn.protocols.websockets.websockets_impl',
        '--hidden-import=uvicorn.protocols.websockets.wsproto_impl',
        '--hidden-import=uvicorn.lifespan',
        '--hidden-import=uvicorn.lifespan.on',
        '--hidden-import=uvicorn.lifespan.off',
        '--hidden-import=anyio._backends._asyncio',
        '--hidden-import=starlette.routing',
        '--hidden-import=starlette.middleware.cors',
        '--hidden-import=starlette.staticfiles',
        '--hidden-import=starlette.websockets',
        str(ROOT / 'main.py'),
    ]

    run(cmd, cwd=ROOT)

    # Move the output directory to resources/bin/{platform}/server/
    src = ROOT / 'build' / 'pyinst' / 'server'
    dst = OUT_DIR / 'server'

    if dst.exists():
        shutil.rmtree(dst)
    shutil.move(str(src), str(dst))

    # Rename the executable itself for clarity
    exe_src = dst / f'server{EXT}'
    if not exe_src.exists():
        # PyInstaller sometimes names it after the spec
        for candidate in dst.iterdir():
            if candidate.stem == 'server':
                candidate.rename(exe_src)
                break

    print(f'\n✓ Server binary at: {dst / f"server{EXT}"}')
    print(f'  Now run:  npm run dist:{PLAT.replace("darwin","mac")}')


if __name__ == '__main__':
    main()
