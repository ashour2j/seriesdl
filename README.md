# Series Downloader

A sophisticated automated TV series torrent downloader that searches The Pirate Bay for complete season packs or individual episodes. Integrates with qBittorrent's WebUI API.

## Features

- TMDB-driven series discovery with an interactive season/episode picker
- Review & approval screen before anything is added to qBittorrent
- Season pack detection with per-episode fallback
- Smart quality/size prioritization (smallest 720p first, then 1080p)
- Resume tracking (skips already-queued items, atomic state writes)
- Dynamic queue control (pauses extras, auto-resumes as slots free up)
- Show-name verification, query caching, and mirror availability checks

## Install

```bash
pip install -r requirements.txt
```

Requires a running qBittorrent instance with the Web UI enabled (see `seriesdl.md`).

## Usage

```bash
python seriesdl.py                    # interactive mode with TMDB discovery
python seriesdl.py --verify-domains   # check which mirrors are reachable
python seriesdl.py "Chernobyl" --quality "1080p" --dry-run
```

See [seriesdl.md](seriesdl.md) for the full user guide.