# SpotDL Web

A local web interface for [SpotDL](https://github.com/spotDL/spotify-downloader) — download Spotify playlists, albums, and tracks via a browser UI.

## Features

- 🎵 Supports Spotify & YouTube playlist / album / song URLs
- 📁 Visual filesystem browser for choosing the output directory
- 🎚️ Audio format: MP3, FLAC, M4A, OGG, Opus, WAV
- 📊 Bitrate: 128 / 192 / 256 / 320 kbps or auto
- 🏷️ Metadata checkboxes (cover art, lyrics, LRC sidecar, etc.)
- 📂 Organized (Artist/Album/) or flat folder structure
- ⏱️ Exponential backoff on rate-limiting (auto-retry up to N times)
- 📡 Real-time progress via WebSocket — no page refresh needed
- 🍪 Optional YouTube cookies file for age-restricted content

## Requirements

- Python ≥ 3.11
- [uv](https://github.com/astral-sh/uv) (`pip install uv` or see uv docs)
- `ffmpeg` in PATH

## Setup

```bash
cd SpotDLWeb

# Create venv and install dependencies
uv sync

# Run the server
uv run python main.py
```

Then open **http://localhost:8080** in your browser.

## Usage

1. **Set output directory** — click the folder icon or type a path directly.
2. **Paste a URL** — Spotify playlist/album/track or YouTube playlist/video.
3. **Choose options** — format, quality, metadata, folder structure.
4. **Click Download** — watch progress in real time.

### Rate limiting

SpotDL uses YouTube Music for audio. If you hit rate limits, the job automatically waits and retries with exponential backoff. Configure *Max retries* and *Initial backoff* in the Advanced panel.

### YouTube cookies

If downloads fail for age-restricted content, export your YouTube cookies to a `cookies.txt` file (Netscape format, e.g. with the [Get cookies.txt](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) extension) and set the path in Advanced settings.

## Development

```bash
uv run uvicorn main:app --reload --port 8080
```
