# Smart TV Series Downloader - User Guide

This directory contains `seriesdl.py`, an automated TV series downloader script that scrapes Pirate Bay mirrors and integrates seamlessly with qBittorrent to manage downloads, directories, and queue limits.

---

## Prerequisites & Setup

### 1. Enable qBittorrent Web UI
The script controls qBittorrent using its Web API. You must enable it:
1. Open your **qBittorrent** client.
2. Go to **Tools** -> **Preferences** -> **Web UI**.
3. Check the box for **Web User Interface (Remote control)**.
4. Keep the Port set to `8080` (or update it in the script's configuration).
5. Set the Username to `admin` and Password to `adminadmin` (default).

### 2. Install Required Python Libraries
```bash
pip install -r requirements.txt
```
(or: `pip install qbittorrent-api requests beautifulsoup4 tpblite`)

### 3. TMDB API Key (Optional but Recommended)
The script uses TMDB to discover how many seasons/episodes a show has, enabling the interactive episode picker.
1. Create a free account at [themoviedb.org](https://www.themoviedb.org)
2. Go to **Settings** -> **API** and generate a key
3. Either set it in the script config (`TMDB_API_KEY = "your_key"`) or pass it at runtime with `--api-key`

---

## How to Run the Script

### 1. Interactive Mode with TMDB Discovery (Recommended)
Run without arguments to get prompted, then use the episode picker:
```bash
python seriesdl.py
```
The script will:
1. Ask for the show name
2. Query TMDB to discover all seasons and episodes
3. Show an **interactive picker** where you select which seasons/episodes to download
4. Search TPB for your selections
5. Show a **review screen** where you can remove items before approving

### 2. Episode Picker Commands
After TMDB discovery, you'll see a menu like:
```
=== Episode Picker: Breaking Bad ===
#    Season     Episodes   Selected   Action
-------------------------------------------------------
1    Season 01     7 eps        3/7    [A]ll [N]one [S]elect
2    Season 02    13 eps        0/13   [A]ll [N]one [S]elect
```
- `1A` = Select all episodes in Season 1
- `2N` = Clear Season 2 selection
- `3S` = Type specific episodes for Season 3 (e.g. `1,3,5-8`)
- `ALL` = Select everything
- `PROCEED` = Go to review screen

### 3. Review Screen
After TPB search, you'll see a table of what was found:
```
=== Download Plan: Breaking Bad ===
#    Type           Season   Details                              Seeds  Size
-----------------------------------------------------------------------
1    Season Pack    S01      Breaking Bad S01 Complete...         45     3.2 GB
2    Episodes       S02      E01, E02, E03, E05                   120    1.4 GB
```
Commands:
- `R 2` = Remove item #2 from the plan
- `GO` = Approve and start downloading
- `ADD` = Go back to episode picker to add more
- `CANCEL` = Abort

### 4. Dry-Run Mode (Test without downloading)
```bash
python seriesdl.py "Cosmos" --seasons "1" --dry-run
```

### 5. Direct Quality Filtering
```bash
python seriesdl.py "Chernobyl" --quality "1080p"
```

### 6. Legacy Season Range Mode (No TMDB)
If you don't have a TMDB API key or pass `--seasons`, it falls back to the old auto-scan behavior:
```bash
python seriesdl.py "Game of Thrones" --seasons "1-3"
```

### 7. Specify TMDB Key at Runtime
```bash
python seriesdl.py "Breaking Bad" --api-key "YOUR_TMDB_KEY_HERE"
```

### 8. Additional CLI Options
```bash
python seriesdl.py "Chernobyl" --quality "1080p" --download-dir "E:\TV" --max-concurrent 2
python seriesdl.py "Cosmos" --seasons "1-2" --min-seeders 10 --no-monitor
python seriesdl.py "Cosmos" --seasons "1" --dry-run --domains "https://tpb.party"   # use only specific mirrors
python seriesdl.py "Cosmos" --seasons "1" --no-title-match                          # disable show-name filtering
python seriesdl.py --verify-domains                                                 # test which mirrors are up
```
Full list: `python seriesdl.py --help`.

Notable flags:
- `--verify-domains` – probes every configured mirror and reports which are reachable, then exits.
- `--domains "url1,url2"` – override the mirror list for a single run.
- `--download-dir` – root folder for downloads (default `./downloads`).
- `--max-concurrent` / `--min-seeders` – override the download-slot and seeder limits.
- `--category` / `--tag` – set the qBittorrent category/tag (default category `series-getter`, tag = show name).
- `--no-monitor` – skip the live download dashboard after queueing.
- `--no-title-match` – disable verification that results actually match the show name.
- `--qbt-host` / `--qbt-port` / `--qbt-user` / `--qbt-pass` – point at a non-default qBittorrent WebUI.

### 9. Best Practice: Check Your Mirrors First
TPB mirrors change frequently. Run `python seriesdl.py --verify-domains` first and pass the
working ones with `--domains` (e.g. `--domains "https://tpb.party,https://piratebay.party"`)
to cut down on timeouts from dead mirrors.

---

## How it Works Under the Hood

- **TMDB Discovery**: Fetches the real season/episode structure so you know exactly what exists before searching.
- **Interactive Episode Picker**: You choose exactly what to download — entire series, specific seasons, or cherry-picked episodes.
- **Review & Approval**: Full control before any downloading starts. Remove items, add more, or cancel.
- **Smart Resolution & Size Prioritization**: Prioritizes **720p** (smallest file), falls back to **1080p**.
- **Season Pack Detection**: Automatically tries to find complete season packs before falling back to individual episodes.
- **Automatic Resume Tracking**: Tracks queued episodes in `series_getter_state.json`. Restarts skip already-queued items unless `--force` is used. State is written atomically so a crash can't corrupt it.
- **Dynamic Queue Management**: Only 3 torrents download concurrently. Extra torrents are paused and auto-resumed as slots free up.
- **Smart Seeder Bypass**: If a proxy mirror hides peer counts (reporting `0` seeds), the script bypasses the minimum seeder check.
- **Show-Name Verification**: Search results are filtered to those that actually reference the requested show, avoiding similarly-titled content (disable with `--no-title-match`).
- **Query Cache**: Repeated searches (e.g. when re-entering the review screen) reuse cached mirror results instead of re-scraping.
- **Windows-Friendly Output**: Colors auto-enable in the console and degrade gracefully when piped, and the log auto-rotates.
