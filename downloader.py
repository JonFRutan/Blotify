"""
Blotify download job manager.

Environment variables (set by Electron main process in packaged builds):
  SPOTDL_BIN      – path to the bundled spotdl binary  (default: 'spotdl' in PATH)
  BLOTIFY_FFMPEG  – path to the bundled ffmpeg binary  (default: 'ffmpeg' in PATH)
"""

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Set

from fastapi import WebSocket


# ── Resolved binary paths ──────────────────────────────────────────────────

_SPOTDL_BIN = os.environ.get('SPOTDL_BIN', 'spotdl')
_FFMPEG_BIN  = os.environ.get('BLOTIFY_FFMPEG', 'ffmpeg')


class JobStatus(str, Enum):
    PENDING      = 'pending'
    RUNNING      = 'running'
    RATE_LIMITED = 'rate_limited'
    COMPLETED    = 'completed'
    FAILED       = 'failed'
    CANCELLED    = 'cancelled'


@dataclass
class LogEntry:
    time: str
    level: str   # info | success | warn | error
    message: str

    def to_dict(self):
        return {'time': self.time, 'level': self.level, 'message': self.message}


@dataclass
class DownloadJob:
    id:              str
    url:             str
    output_dir:      str
    format:          str  = 'mp3'
    bitrate:         str  = '320k'
    # Folder structure choices:
    #   organized  →  {artists}/{album}/{title}.{ext}
    #   playlist   →  {list-name}/{artists} - {title}.{ext}
    #   flat       →  {title}.{ext}
    structure:       str  = 'organized'
    threads:         int  = 4
    overwrite:       str  = 'skip'      # skip | force | metadata
    generate_lrc:    bool = False
    cookie_file:     str  = ''
    max_retries:     int  = 5
    initial_backoff: int  = 30          # seconds

    # Runtime state
    status:       JobStatus            = JobStatus.PENDING
    progress:     int                  = 0
    total:        int                  = 0
    current_song: str                  = ''
    log:          List[LogEntry]       = field(default_factory=list)
    created_at:   str                  = field(default_factory=lambda: datetime.now().isoformat())
    process:      Optional[asyncio.subprocess.Process] = None
    cancelled:    bool                 = False
    retry_count:  int                  = 0
    backoff_until: Optional[str]       = None

    def to_dict(self) -> dict:
        return {
            'id':            self.id,
            'url':           self.url,
            'output_dir':    self.output_dir,
            'format':        self.format,
            'bitrate':       self.bitrate,
            'structure':     self.structure,
            'status':        self.status,
            'progress':      self.progress,
            'total':         self.total,
            'current_song':  self.current_song,
            'log':           [e.to_dict() for e in self.log[-120:]],
            'created_at':    self.created_at,
            'retry_count':   self.retry_count,
            'backoff_until': self.backoff_until,
        }

    def add_log(self, level: str, message: str):
        self.log.append(LogEntry(
            time=datetime.now().strftime('%H:%M:%S'),
            level=level,
            message=message,
        ))


class JobManager:
    def __init__(self):
        self.jobs: Dict[str, DownloadJob] = {}
        self._ws: Set[WebSocket] = set()

    # ── WebSocket broadcast ────────────────────────────────────────────────

    def add_ws_client(self, ws: WebSocket):
        self._ws.add(ws)

    def remove_ws_client(self, ws: WebSocket):
        self._ws.discard(ws)

    async def broadcast(self, job: DownloadJob):
        dead: Set[WebSocket] = set()
        msg = json.dumps({'type': 'job_update', 'job': job.to_dict()})
        for ws in self._ws:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        self._ws -= dead

    # ── Job management ─────────────────────────────────────────────────────

    def add_job(self, job: DownloadJob):
        self.jobs[job.id] = job

    async def cancel_job(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job:
            return False
        job.cancelled = True
        if job.process:
            try:
                job.process.kill()
            except Exception:
                pass
        return True

    # ── Command builder ────────────────────────────────────────────────────

    def _output_template(self, job: DownloadJob) -> str:
        base = Path(job.output_dir)
        if job.structure == 'flat':
            return str(base / '{title}.{output-ext}')
        if job.structure == 'playlist':
            return str(base / '{list-name}' / '{artists} - {title}.{output-ext}')
        # organized (default)
        return str(base / '{artists}' / '{album}' / '{title}.{output-ext}')

    def _build_cmd(self, job: DownloadJob) -> List[str]:
        cmd = [
            _SPOTDL_BIN, 'download', job.url,
            '--output',   self._output_template(job),
            '--format',   job.format,
            '--threads',  str(job.threads),
            '--overwrite', job.overwrite,
            '--log-level', 'INFO',
        ]

        # Bitrate is only meaningful for lossy formats
        if job.format not in ('flac', 'wav') and job.bitrate != 'auto':
            cmd += ['--bitrate', job.bitrate]

        if job.generate_lrc:
            cmd.append('--generate-lrc')

        if job.cookie_file and Path(job.cookie_file).is_file():
            cmd += ['--cookie-file', job.cookie_file]

        # Use bundled/system ffmpeg if one was injected
        if _FFMPEG_BIN and _FFMPEG_BIN not in ('ffmpeg', 'ffmpeg.exe'):
            cmd += ['--ffmpeg', _FFMPEG_BIN]

        return cmd

    # ── Output parsing ─────────────────────────────────────────────────────

    _RATE_LIMIT_RE = re.compile(
        r'(429|too.?many.?requests|rate.?limit|quota.?exceeded|'
        r'try.?again.?later|max.?retries.?exceeded)',
        re.IGNORECASE,
    )
    _PROGRESS_RE = re.compile(r'(\d+)\s*/\s*(\d+)')
    _SONG_RE     = re.compile(r'(?:Downloaded|Downloading|Skipping)\s+"?([^"|\n]+)"?', re.IGNORECASE)

    def _classify(self, line: str) -> str:
        if self._RATE_LIMIT_RE.search(line):
            return 'error'
        ll = line.lower()
        if any(w in ll for w in ('error', 'failed', 'exception', 'traceback')):
            return 'error'
        if any(w in ll for w in ('warning', 'warn', 'skip')):
            return 'warn'
        if any(w in ll for w in ('downloaded', 'complete', 'success')):
            return 'success'
        return 'info'

    def _parse_progress(self, line: str, job: DownloadJob):
        m = self._PROGRESS_RE.search(line)
        if m:
            job.progress = int(m.group(1))
            job.total    = int(m.group(2))
        s = self._SONG_RE.search(line)
        if s:
            job.current_song = s.group(1).strip()

    # ── Runner ─────────────────────────────────────────────────────────────

    async def run_job(self, job_id: str):
        job = self.jobs.get(job_id)
        if not job:
            return

        Path(job.output_dir).mkdir(parents=True, exist_ok=True)
        job.status = JobStatus.RUNNING
        await self.broadcast(job)

        backoff = job.initial_backoff

        while not job.cancelled:
            cmd = self._build_cmd(job)
            job.add_log('info', '▶ ' + ' '.join(cmd))
            await self.broadcast(job)

            # ── Spawn spotdl ───────────────────────────────────────────
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                job.process = proc
            except FileNotFoundError:
                job.add_log('error', f'Binary not found: {cmd[0]}')
                job.status = JobStatus.FAILED
                await self.broadcast(job)
                return
            except Exception as exc:
                job.add_log('error', f'Spawn error: {exc}')
                job.status = JobStatus.FAILED
                await self.broadcast(job)
                return

            rate_limited = False

            assert proc.stdout is not None
            async for raw in proc.stdout:
                if job.cancelled:
                    proc.kill()
                    break

                line = raw.decode('utf-8', errors='replace').rstrip()
                if not line:
                    continue

                self._parse_progress(line, job)
                level = self._classify(line)
                job.add_log(level, line)

                if self._RATE_LIMIT_RE.search(line):
                    rate_limited = True
                    proc.kill()
                    break

                await self.broadcast(job)

            await proc.wait()

            # ── Cancelled ─────────────────────────────────────────────
            if job.cancelled:
                job.status = JobStatus.CANCELLED
                job.add_log('warn', 'Cancelled by user.')
                await self.broadcast(job)
                return

            # ── Rate limited — exponential backoff ────────────────────
            if rate_limited:
                if job.retry_count >= job.max_retries:
                    job.add_log('error', f'Max retries ({job.max_retries}) exceeded.')
                    job.status = JobStatus.FAILED
                    await self.broadcast(job)
                    return

                wait = min(backoff * (2 ** job.retry_count), 300)
                job.retry_count  += 1
                job.backoff_until = (datetime.now() + timedelta(seconds=wait)).isoformat()
                job.status        = JobStatus.RATE_LIMITED
                job.add_log('warn',
                    f'Rate limited — waiting {wait}s before retry '
                    f'{job.retry_count}/{job.max_retries}…')
                await self.broadcast(job)

                for _ in range(wait):
                    if job.cancelled:
                        break
                    await asyncio.sleep(1)

                job.backoff_until = None
                if not job.cancelled:
                    job.status = JobStatus.RUNNING
                    continue   # retry loop

            # ── Normal exit ───────────────────────────────────────────
            if proc.returncode == 0:
                job.status = JobStatus.COMPLETED
                job.add_log('success', '✓ All done!')
            else:
                job.status = JobStatus.FAILED
                job.add_log('error', f'spotdl exited with code {proc.returncode}')

            await self.broadcast(job)
            return
