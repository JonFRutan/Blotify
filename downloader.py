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
import sys
import tempfile
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Set

from fastapi import WebSocket


# ── Resolved binary paths ──────────────────────────────────────────────────

def _resolve_spotdl() -> List[str]:
    """Return the spotdl invocation prefix as a list of tokens.

    In packaged builds SPOTDL_BIN is set to the bundled binary path.
    In dev mode we run `python -m spotdl` with the current interpreter so
    we never depend on a script shebang pointing at a potentially stale path.
    """
    if 'SPOTDL_BIN' in os.environ:
        return [os.environ['SPOTDL_BIN']]
    return [sys.executable, '-m', 'spotdl']

_SPOTDL_BIN: List[str] = _resolve_spotdl()
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
    # Folder structure:
    #   organized  →  {artists}/{album}/{title}.{ext}
    #   playlist   →  {list-name}/{artists} - {title}.{ext}
    #   flat       →  {title}.{ext}
    structure:       str  = 'organized'
    audio_provider:  str  = 'youtube-music'
    threads:         int  = 4
    overwrite:       str  = 'skip'
    generate_lrc:    bool = False
    cookie_file:     str  = ''
    max_retries:     int  = 5
    initial_backoff: int  = 30
    # N consecutive AudioProviderErrors with no success/skip between them,
    # all occurring within error_window seconds, before we declare rate-limited.
    error_threshold: int  = 5
    error_window:    float = 90.0   # seconds; rate-limit bursts are fast, unavailable songs are slow
    # On first rate-limit: save the playlist as a .spotdl file and reverse the
    # song order so retries start from the END of the playlist (undownloaded songs)
    # rather than repeating the beginning.
    reverse_on_retry: bool = True

    # Runtime state
    status:        JobStatus      = JobStatus.PENDING
    progress:      int            = 0
    total:         int            = 0
    current_song:  str            = ''
    log:           List[LogEntry] = field(default_factory=list)
    created_at:    str            = field(default_factory=lambda: datetime.now().isoformat())
    process:       Optional[asyncio.subprocess.Process] = None
    cancelled:     bool           = False
    retry_count:   int            = 0
    backoff_until: Optional[str]  = None

    # Internal — not serialised; set on first retry when reverse_on_retry=True
    _reversed_path: Optional[str] = field(default=None, init=False, repr=False)

    def to_dict(self) -> dict:
        return {
            'id':               self.id,
            'url':              self.url,
            'output_dir':       self.output_dir,
            'format':           self.format,
            'bitrate':          self.bitrate,
            'structure':        self.structure,
            'audio_provider':   self.audio_provider,
            'reverse_on_retry': self.reverse_on_retry,
            'status':           self.status,
            'progress':         self.progress,
            'total':            self.total,
            'current_song':     self.current_song,
            'log':              [e.to_dict() for e in self.log[-120:]],
            'created_at':       self.created_at,
            'retry_count':      self.retry_count,
            'backoff_until':    self.backoff_until,
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

    # ── WebSocket ──────────────────────────────────────────────────────────

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
        return str(base / '{artists}' / '{album}' / '{title}.{output-ext}')

    def _build_cmd(self, job: DownloadJob, target: str) -> List[str]:
        """Build the spotdl command. `target` is either the original URL or a
        path to a reversed .spotdl save file used on retries."""
        cmd = [
            *_SPOTDL_BIN, 'download', target,
            '--output',    self._output_template(job),
            '--format',    job.format,
            '--threads',   str(job.threads),
            '--overwrite', job.overwrite,
            '--audio',     job.audio_provider,
            '--log-level', 'INFO',
        ]

        if job.format not in ('flac', 'wav') and job.bitrate != 'auto':
            cmd += ['--bitrate', job.bitrate]

        if job.generate_lrc:
            cmd.append('--generate-lrc')

        if job.cookie_file and Path(job.cookie_file).is_file():
            cmd += ['--cookie-file', job.cookie_file]

        if _FFMPEG_BIN and _FFMPEG_BIN not in ('ffmpeg', 'ffmpeg.exe'):
            cmd += ['--ffmpeg', _FFMPEG_BIN]

        return cmd

    # ── Playlist save + reverse ────────────────────────────────────────────

    async def _save_reversed_playlist(self, job: DownloadJob) -> Optional[str]:
        """
        Run `spotdl save <url>` to get the full song list, reverse it, write
        the reversed JSON to a temp .spotdl file, and return its path.

        On all subsequent retries we re-use this file so spotdl starts from
        the END of the original playlist — i.e. the songs most likely not yet
        downloaded — and fast-skips anything already done.
        """
        if job._reversed_path:
            return job._reversed_path  # already prepared

        tmp = Path(tempfile.gettempdir()) / f'blotify-{job.id}.spotdl'

        job.add_log('info', 'Fetching full song list so retries can start from the end of the playlist...')
        await self.broadcast(job)

        save_cmd = [
            *_SPOTDL_BIN, 'save', job.url,
            '--save-file', str(tmp),
            '--log-level', 'ERROR',
        ]
        if job.cookie_file and Path(job.cookie_file).is_file():
            save_cmd += ['--cookie-file', job.cookie_file]

        try:
            proc = await asyncio.create_subprocess_exec(
                *save_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            await proc.wait()
        except Exception as e:
            job.add_log('warn', f'Could not save playlist ({e}) — retrying from beginning instead')
            return None

        if not tmp.exists():
            job.add_log('warn', 'spotdl save produced no file — retrying from beginning instead')
            return None

        try:
            raw = tmp.read_text(encoding='utf-8')
            data = json.loads(raw)

            # SpotDL 4.x saves as a plain JSON array of song dicts
            if isinstance(data, list):
                n = len(data)
                data.reverse()
                tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            elif isinstance(data, dict) and 'songs' in data:
                n = len(data['songs'])
                data['songs'].reverse()
                tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            else:
                job.add_log('warn', 'Unrecognised .spotdl format — retrying from beginning instead')
                return None

            job.add_log(
                'info',
                f'Reversed {n} songs. Retry will start from the end of the playlist '
                f'and skip anything already downloaded.'
            )
            job._reversed_path = str(tmp)
            return str(tmp)

        except Exception as e:
            job.add_log('warn', f'Could not reverse playlist ({e}) — retrying from beginning instead')
            tmp.unlink(missing_ok=True)
            return None

    # ── Output patterns ────────────────────────────────────────────────────

    _HTTP_RATE_LIMIT_RE = re.compile(
        r'(429|too.?many.?requests|rate.?limit|quota.?exceeded|'
        r'try.?again.?later|max.?retries.?exceeded)',
        re.IGNORECASE,
    )
    _AUDIO_ERR_RE = re.compile(r'AudioProviderError', re.IGNORECASE)

    # Any line that shows spotdl *made progress* — download OR skip.
    # Either resets the consecutive-error streak so ordinary unavailable songs
    # (which are surrounded by other activity) never trigger the backoff.
    _PROGRESS_RE  = re.compile(r'(Downloaded|Skipping)\s+"', re.IGNORECASE)

    _SONG_NAME_RE = re.compile(
        r'(?:Downloaded|Downloading|Skipping)\s+"?([^"|\n]+)"?', re.IGNORECASE
    )
    _COUNT_RE     = re.compile(r'(\d+)\s*/\s*(\d+)')

    def _classify(self, line: str) -> str:
        ll = line.lower()
        if self._HTTP_RATE_LIMIT_RE.search(line):
            return 'error'
        if self._AUDIO_ERR_RE.search(line):
            return 'warn'
        if any(w in ll for w in ('error', 'failed', 'exception', 'traceback')):
            return 'error'
        if any(w in ll for w in ('warning', 'warn', 'skip')):
            return 'warn'
        if any(w in ll for w in ('downloaded', 'complete', 'success')):
            return 'success'
        return 'info'

    def _parse_progress(self, line: str, job: DownloadJob):
        m = self._COUNT_RE.search(line)
        if m:
            job.progress = int(m.group(1))
            job.total    = int(m.group(2))
        s = self._SONG_NAME_RE.search(line)
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
            # On retries, use the reversed save file if we have one
            target = job._reversed_path or job.url
            cmd    = self._build_cmd(job, target)
            job.add_log('info', '> ' + ' '.join(cmd))
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

            rate_limited      = False
            consecutive_errs  = 0        # errors since last success/skip
            streak_start: Optional[float] = None  # monotonic time of first error in streak

            assert proc.stdout is not None
            async for raw in proc.stdout:
                if job.cancelled:
                    proc.kill()
                    break

                line = raw.decode('utf-8', errors='replace').rstrip()
                if not line:
                    continue

                self._parse_progress(line, job)

                # ── Audio-error streak detection ───────────────────────
                if self._AUDIO_ERR_RE.search(line):
                    now = _time.monotonic()
                    if consecutive_errs == 0:
                        streak_start = now
                    consecutive_errs += 1
                    job.add_log('warn', line)

                    streak_secs = now - (streak_start or now)

                    # Trigger only when:
                    #  • N errors in a row with no progress between them, AND
                    #  • Those N errors all happened within error_window seconds
                    #    (rate-limit bursts are fast; unavailable songs are slow
                    #     because spotdl tries several providers per song)
                    if (consecutive_errs >= job.error_threshold
                            and streak_secs <= job.error_window):
                        job.add_log(
                            'warn',
                            f'{consecutive_errs} audio errors in {streak_secs:.0f}s '
                            f'— YouTube is rate-limiting. Pausing before retry…'
                        )
                        rate_limited = True
                        proc.kill()
                        break

                elif self._HTTP_RATE_LIMIT_RE.search(line):
                    job.add_log('error', line)
                    rate_limited = True
                    proc.kill()
                    break

                else:
                    # Any download or skip resets the streak — an isolated
                    # unavailable song surrounded by normal activity is not a
                    # rate-limit signal.
                    if self._PROGRESS_RE.search(line):
                        consecutive_errs = 0
                        streak_start     = None

                    level = self._classify(line)
                    job.add_log(level, line)

                await self.broadcast(job)

            await proc.wait()

            # ── Cancelled ─────────────────────────────────────────────
            if job.cancelled:
                job.status = JobStatus.CANCELLED
                job.add_log('warn', 'Cancelled by user.')
                await self.broadcast(job)
                return

            # ── Rate limited ───────────────────────────────────────────
            if rate_limited:
                if job.retry_count >= job.max_retries:
                    job.add_log('error', f'Max retries ({job.max_retries}) exceeded.')
                    job.status = JobStatus.FAILED
                    await self.broadcast(job)
                    return

                # On the very first backoff, save + reverse the playlist so
                # every subsequent retry starts from the tail of the list.
                # Already-downloaded songs are skipped instantly; we reach the
                # undownloaded songs at the end without churning through the top.
                if job.retry_count == 0 and job.reverse_on_retry and not job._reversed_path:
                    await self._save_reversed_playlist(job)

                wait = min(backoff * (2 ** job.retry_count), 300)
                job.retry_count  += 1
                job.backoff_until = (datetime.now() + timedelta(seconds=wait)).isoformat()
                job.status        = JobStatus.RATE_LIMITED
                job.add_log(
                    'warn',
                    f'Waiting {wait}s before retry {job.retry_count}/{job.max_retries}. '
                    f'Already-downloaded songs will be skipped automatically.'
                )
                await self.broadcast(job)

                for _ in range(wait):
                    if job.cancelled:
                        break
                    await asyncio.sleep(1)

                job.backoff_until = None
                if not job.cancelled:
                    job.status = JobStatus.RUNNING
                    continue

            # ── Normal exit ───────────────────────────────────────────
            if proc.returncode == 0:
                job.status = JobStatus.COMPLETED
                job.add_log('success', 'All done!')
            else:
                job.status = JobStatus.FAILED
                job.add_log('error', f'spotdl exited with code {proc.returncode}')

            await self.broadcast(job)
            return
