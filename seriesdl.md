# Smart TV Series Downloader - User Guide

This directory contains `series_getter.py`, an automated TV series downloader script that scrapes Pirate Bay mirrors and integrates seamlessly with qBittorrent to manage downloads, directories, and queue limits.

---

## 📋 Prerequisites & Setup

### 1. Enable qBittorrent Web UI
The script controls qBittorrent using its Web API. You must enable it:
1. Open your **qBittorrent** client.
2. Go to **Tools** -> **Preferences** -> **Web UI**.
3. Check the box for **Web User Interface (Remote control)**.
4. Keep the Port set to `8080` (or update it in the script's configuration).
5. Set the Username to `admin` and Password to `adminadmin` (default).

### 2. Install Required Python Libraries
Open PowerShell or Command Prompt and install the dependencies:
```bash
pip install qbittorrent-api requests beautifulsoup4 tpblite
```

---

## 🚀 How to Run the Script

Always navigate to this directory before running the commands:
```bash
cd "C:\Users\hazookaa\.gemini\antigravity\scratch\tv_downloader"
```

### 1. Simple Interactive Mode (Highly Recommended)
If you run the script without any arguments, it will prompt you for the show name:
```bash
python series_getter.py
```

### 2. Dry-Run Mode (Test Search results)
Use `--dry-run` to see which torrents would be chosen and how sizes are parsed without actually sending anything to qBittorrent. 
At the end of execution, the script displays a **Dry-Run Download Plan Summary**—a beautiful, colorized ASCII table showing every complete season pack or individual episode that would be queued, its parsed file size, and seeder count:
```bash
python series_getter.py "Cosmos" --seasons "1" --dry-run
```

### 3. Curated Selection Mode (Pick Manually)
Use `--interactive` to display a menu of the top 10 torrents found for each season or episode, allowing you to select your preferred choice manually:
```bash
python series_getter.py "Breaking Bad" --seasons "1" --interactive
```

### 4. Direct Quality Filtering
Strictly force a specific resolution tag to be present (e.g. `1080p` instead of preferring `720p`):
```bash
python series_getter.py "Chernobyl" --quality "1080p"
```

### 5. Custom Season Ranges
Specify a range of seasons (e.g., seasons 1 to 3) or individual seasons separated by commas:
```bash
python series_getter.py "Game of Thrones" --seasons "1-3"
python series_getter.py "Sherlock" --seasons "1,3"
```

---

## ⚙️ How it Works Under the Hood

- **Smart Resolution & Size Prioritization**: The script prioritizes **720p** resolution, selecting the **smallest file size** (to save bandwidth and disk space). If no 720p is found, it falls back to **1080p** (again, selecting the **smallest file size**).
- **Auto-Sweep Loop**: If a complete season pack is not found, the script switches to episode-by-episode sweeps, sequentially downloading `E01`, `E02`, etc., until **3 consecutive episode numbers** return 0 results (safely concluding the season has ended).
- **Automatic Resume Tracking**: The script tracks queued episodes in `series_getter_state.json`. If you stop and restart, it skips already-downloaded items unless you specify the `--force` flag.
- **Dynamic Queue Management**: The script will only let 3 torrents download concurrently (defined by `MAX_CONCURRENT`). Extra torrents are added as `paused` and are automatically resumed in order as active slots free up.
- **Smart Seeder Bypass**: If a proxy mirror hides peer counts (reporting `0` seeds for everything), the script automatically bypasses the minimum seeder check so downloads do not fail.
