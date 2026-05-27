"""
Blotify — FastAPI backend
"""

import asyncio
import os
import sys
import uuid
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from downloader import DownloadJob, JobManager


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
        threads=int(body.get('threads', 4)),
        overwrite=body.get('overwrite', 'skip'),
        generate_lrc=bool(body.get('generate_lrc', False)),
        cookie_file=body.get('cookie_file', ''),
        max_retries=int(body.get('max_retries', 5)),
        initial_backoff=int(body.get('initial_backoff', 30)),
    )

    job_manager.add_job(job)
    asyncio.create_task(job_manager.run_job(job_id))
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
