# Blotify — Project Guide for Claude

## What this is
Blotify is an Electron desktop app that wraps a local FastAPI server to provide a GUI for [SpotDL](https://github.com/spotDL/spotify-downloader). Users paste Spotify/YouTube URLs and download audio locally with full metadata.

## Architecture

```
Electron (electron/main.js)
  └── spawns → FastAPI server (main.py, port chosen at runtime)
                  ├── serves static/index.html (the UI)
                  ├── /api/* REST endpoints
                  ├── /ws   WebSocket (real-time job progress)
                  └── spawns → spotdl subprocess per download job
                                  └── uses bundled yt-dlp internally
```

**Key principle:** Electron is purely a shell. All logic (downloads, settings, filesystem browsing) lives in the Python server. The frontend talks to it via fetch/WebSocket — not Electron IPC. This keeps the app fully usable in a plain browser too (`uv run python main.py` → open localhost:8080).

## File map

| File | Purpose |
|------|---------|
| `electron/main.js` | Electron entry: finds free port, spawns server, creates window, app menu |
| `electron/preload.js` | Exposes `window.blotify` to renderer (open-folder IPC, platform, isElectron flag) |
| `main.py` | FastAPI app: routes, WebSocket endpoint, static file serving, PyInstaller path handling |
| `downloader.py` | `DownloadJob` dataclass + `JobManager`: subprocess lifecycle, output parsing, exponential backoff |
| `static/index.html` | Single-file SPA: all HTML/CSS/JS inline, no build step |
| `scripts/download-bins.js` | Downloads platform spotdl binary from GitHub releases into `resources/bin/{platform}/` |
| `scripts/build-server.py` | Runs PyInstaller on `main.py` → `resources/bin/{platform}/server/` |
| `electron-builder.yml` | Packaging config: extraResources, targets per platform, asarUnpack for ffmpeg-static |
| `pyproject.toml` | Python deps (fastapi, uvicorn, spotdl, aiofiles) + dev dep (pyinstaller) managed by uv |
| `package.json` | npm/Electron entry, build scripts |

## Dev workflow

```bash
# First time setup
uv sync --group dev        # Python venv + all deps incl. PyInstaller
npm install                # Electron + electron-builder + ffmpeg-static

# Run as Electron app (recommended)
npm start                  # spawns uv run python main.py automatically

# Run server only (browser dev)
uv run python main.py      # → http://127.0.0.1:8080
PORT=9000 uv run python main.py   # custom port
```

`npm start` uses `app.isPackaged === false`, so it runs the server via `uv run` and opens DevTools automatically.

## Environment variables (server reads these)

| Var | Set by | Default | Purpose |
|-----|--------|---------|---------|
| `PORT` | Electron main.js | `8080` | FastAPI listen port |
| `SPOTDL_BIN` | Electron main.js (prod only) | `spotdl` (PATH) | Path to bundled spotdl binary |
| `BLOTIFY_FFMPEG` | Electron main.js | `ffmpeg` (PATH) | Path to ffmpeg binary (ffmpeg-static in prod) |

In dev mode `SPOTDL_BIN` is never set, so the venv's `spotdl` is used.

## Job lifecycle

```
POST /api/jobs → DownloadJob created (status: pending)
  → asyncio.create_task(run_job())
  → status: running, spotdl subprocess spawned
  → stdout parsed line by line → progress/current_song updated
  → each update broadcast to all WebSocket clients
  → on rate-limit: status: rate_limited, exponential backoff, then retry
  → on exit 0: status: completed
  → on exit non-0: status: failed
```

Job state is **in-memory only** — it does not survive server restart.

## WebSocket protocol

All messages are JSON. Server → client:
```json
{ "type": "initial", "jobs": [...] }          // on connect
{ "type": "job_update", "job": { ... } }      // on any state change
```
Client → server: `"ping"` → server replies `"pong"` (keepalive).

## Folder structure options (downloader.py `_output_template`)

| Option | spotdl `--output` template |
|--------|---------------------------|
| `organized` | `{output_dir}/{artists}/{album}/{title}.{output-ext}` |
| `playlist` | `{output_dir}/{list-name}/{artists} - {title}.{output-ext}` |
| `flat` | `{output_dir}/{title}.{output-ext}` |

## Build pipeline (for distribution)

```bash
npm run download-bins      # → resources/bin/{platform}/spotdl[.exe]
npm run build-server       # → resources/bin/{platform}/server/ (PyInstaller onedir)
npm run dist:linux         # → dist/*.AppImage, dist/*.deb
npm run dist:mac           # → dist/*.dmg
npm run dist:win           # → dist/*.exe (NSIS)
```

**PyInstaller note:** `main.py` detects `sys.frozen` to find `static/` inside the bundle (`sys._MEIPASS/static/`). Never remove that check.

**ffmpeg:** Bundled via the `ffmpeg-static` npm package. `electron-builder.yml` marks it `asarUnpack` so the OS can execute it. The path is passed to the server as `BLOTIFY_FFMPEG`.

**spotdl binary:** Downloaded from GitHub releases (v4.5.0). It is a full PyInstaller bundle that includes yt-dlp internally — no separate yt-dlp binary needed.

## Adding your logo

1. Place `icon.png` (512×512) and `icon.icns` (macOS) in `assets/`
2. Uncomment `icon:` in `electron-builder.yml`
3. Set `<img id="logo-img" src="/static/icon.png">` in `static/index.html` — the JS already hides the `♫` fallback once the image loads

## Rate-limit backoff

Configured per job via the Advanced panel. Defaults: 5 retries, 30 s initial backoff, doubling each time (30→60→120→240→300 s max). Detected by regex on spotdl stdout: `429 | too many requests | rate limit | quota exceeded`.

## CI / GitHub Actions

`.github/workflows/build.yml` — matrix build across ubuntu/macos/windows. On `v*` tag push, a GitHub Release is created automatically with all three platform installers attached.
