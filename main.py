"""
Blotify — FastAPI backend
"""

import asyncio
import json
import os
import platform
import sys
import uuid
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from downloader import DownloadJob, JobManager


# ── Persistent settings file (survives random port changes) ───────────────

def _settings_file() -> Path:
    system = platform.system()
    if system == 'Windows':
        base = Path(os.environ.get('APPDATA', Path.home() / 'AppData' / 'Roaming'))
    elif system == 'Darwin':
        base = Path.home() / 'Library' / 'Application Support'
    else:
        base = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
    return base / 'Blotify' / 'settings.json'

SETTINGS_FILE = _settings_file()


# ── Static-file path (works both normally and inside PyInstaller bundle) ───

def _static_dir() -> Path:
    if getattr(sys, 'frozen', False):
        # Running inside a PyInstaller onedir bundle
        return Path(sys._MEIPASS) / 'static'
    return Path(__file__).parent / 'static'


STATIC = _static_dir()

# ── App ────────────────────────────────────────────────────────────────────

app = FastAPI(title='Blotify')
job_manager = JobManager()

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['*'],
    allow_headers=['*'],
)

app.mount('/static', StaticFiles(directory=str(STATIC)), name='static')


# ── Pages ──────────────────────────────────────────────────────────────────

@app.get('/', response_class=HTMLResponse)
async def root():
    return (STATIC / 'index.html').read_text(encoding='utf-8')


# ── Filesystem browser ─────────────────────────────────────────────────────

@app.get('/api/browse')
async def browse(path: str = ''):
    p = Path(path).resolve() if path else Path.home()
    if not p.is_dir():
        p = p.parent if p.parent.is_dir() else Path.home()

    entries = []
    try:
        for item in sorted(p.iterdir()):
            if item.is_dir() and not item.name.startswith('.'):
                entries.append({
                    'name': item.name,
                    'path': str(item),
                    'accessible': os.access(str(item), os.R_OK),
                })
    except PermissionError:
        pass

    return {
        'current': str(p),
        'parent': str(p.parent) if p.parent != p else None,
        'entries': entries,
    }


# ── Jobs ───────────────────────────────────────────────────────────────────

@app.post('/api/jobs')
async def create_job(body: dict):
    job_id = str(uuid.uuid4())[:8]

    output_dir = body.get('output_dir') or str(Path.home() / 'Music' / 'Blotify')
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    job = DownloadJob(
        id=job_id,
        url=body['url'],
        output_dir=output_dir,
        format=body.get('format', 'mp3'),
        bitrate=body.get('bitrate', '320k'),
        structure=body.get('structure', 'organized'),
        audio_provider=body.get('audio_provider', 'youtube-music'),
        threads=int(body.get('threads', 4)),
        overwrite=body.get('overwrite', 'skip'),
        generate_lrc=bool(body.get('generate_lrc', False)),
        cookie_file=body.get('cookie_file', ''),
        max_retries=int(body.get('max_retries', 5)),
        initial_backoff=int(body.get('initial_backoff', 30)),
        error_threshold=int(body.get('error_threshold', 5)),
        error_window=float(body.get('error_window', 90.0)),
        reverse_on_retry=bool(body.get('reverse_on_retry', True)),
    )

    await job_manager.enqueue_job(job)
    return job.to_dict()


@app.get('/api/jobs')
async def list_jobs():
    return [j.to_dict() for j in reversed(list(job_manager.jobs.values()))]


@app.delete('/api/jobs/{job_id}')
async def cancel_job(job_id: str):
    return {'success': await job_manager.cancel_job(job_id)}


@app.delete('/api/jobs')
async def clear_finished():
    done = [jid for jid, j in job_manager.jobs.items()
            if j.status in ('completed', 'failed', 'cancelled')]
    for jid in done:
        del job_manager.jobs[jid]
    return {'cleared': len(done)}


@app.get('/api/settings')
async def get_settings():
    try:
        if SETTINGS_FILE.exists():
            return json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
    except Exception:
        pass
    return {}


@app.post('/api/settings')
async def save_settings(body: dict):
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding='utf-8')
        return {'ok': True}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


@app.get('/api/filetree')
async def filetree(path: str = '', depth: int = 2):
    root = Path(path).resolve() if path else Path.home()
    if not root.is_dir():
        return {'error': 'not a directory', 'path': str(root)}

    def scan(directory: Path, remaining: int) -> list:
        items = []
        try:
            for entry in sorted(directory.iterdir()):
                if entry.name.startswith('.'):
                    continue
                item = {'name': entry.name, 'path': str(entry), 'is_dir': entry.is_dir()}
                if entry.is_dir() and remaining > 1:
                    item['children'] = scan(entry, remaining - 1)
                items.append(item)
        except PermissionError:
            pass
        return items

    return {'path': str(root), 'children': scan(root, depth)}


@app.get('/api/output-dir')
async def output_dir():
    """Return the most-recently-used output directory (used by Electron menu)."""
    dirs = [j.output_dir for j in job_manager.jobs.values() if j.output_dir]
    return {'dir': dirs[-1] if dirs else str(Path.home() / 'Music' / 'Blotify')}


# ── WebSocket ──────────────────────────────────────────────────────────────

@app.websocket('/ws')
async def ws_endpoint(websocket: WebSocket):
    import json
    await websocket.accept()
    job_manager.add_ws_client(websocket)
    try:
        await websocket.send_text(json.dumps({
            'type': 'initial',
            'jobs': [j.to_dict() for j in reversed(list(job_manager.jobs.values()))],
        }))
        while True:
            data = await websocket.receive_text()
            if data == 'ping':
                await websocket.send_text('pong')
    except WebSocketDisconnect:
        job_manager.remove_ws_client(websocket)


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import uvicorn

    host = '127.0.0.1'           # localhost-only; Electron proxies via http
    port = int(os.environ.get('PORT', 8080))
    reload = os.environ.get('BLOTIFY_RELOAD', '0') == '1'

    uvicorn.run('main:app', host=host, port=port, reload=reload)
