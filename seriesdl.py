#!/usr/bin/env python3
"""
Series Getter: Smart TV Series Downloader from The Pirate Bay
Automates downloading entire TV series with intelligent fallback when no full-season packs exist.

Requires:
- qbittorrent-api (for qBittorrent integration)
- tpblite (optional, falls back to custom BeautifulSoup scraper)
- bs4 & requests (for HTML fallback scraping)
"""

import os
import sys
import re
import json
import time
import argparse
import logging
import urllib.parse
import requests
from bs4 import BeautifulSoup

# ==========================================
#               CONFIGURATION
# ==========================================
TPB_DOMAINS = [
    "https://thepiratebay.org",
    "https://tpb.party",
    "https://piratebay.party",
    "https://thepiratebay.cloud",
    "https://piratebay.live"
]

QBT_HOST = "localhost"
QBT_PORT = 8080
QBT_USER = "admin"
QBT_PASS = "adminadmin"

DOWNLOAD_DIR = "./downloads"
AUTO_START = True
MIN_SEEDERS = 5
MAX_CONCURRENT = 3

STATE_FILE = "series_getter_state.json"
LOG_FILE = "series_getter.log"

# ==========================================
#             OPTIONAL LIBRARIES
# ==========================================
try:
    import qbittorrentapi
    HAS_QBT = True
except ImportError:
    HAS_QBT = False

try:
    from tpblite import TPB
    HAS_TPB_LITE = True
except ImportError:
    HAS_TPB_LITE = False

# ==========================================
#             LOGGING & COLORS
# ==========================================
logging.basicConfig(
    filename=LOG_FILE,
    filemode='a',
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

def print_info(msg):
    print(f"{Colors.BLUE}[INFO]{Colors.ENDC} {msg}")

def print_success(msg):
    print(f"{Colors.GREEN}[SUCCESS]{Colors.ENDC} {Colors.BOLD}{msg}{Colors.ENDC}")

def print_warning(msg):
    print(f"{Colors.WARNING}[WARNING]{Colors.ENDC} {msg}")

def print_error(msg):
    print(f"{Colors.FAIL}[ERROR]{Colors.ENDC} {msg}")

def print_header(msg):
    print(f"\n{Colors.HEADER}{Colors.BOLD}=== {msg} ==={Colors.ENDC}\n")

# ==========================================
#          SIZE PARSING & DEDUPLICATION
# ==========================================
def parse_size_to_bytes(size_str):
    if not size_str:
        return 0
    size_str = size_str.upper().strip()
    match = re.search(r'(\d+(?:\.\d+)?|\.\d+)\s*([GMK]I?B|[GMK]B|BYTES|B)', size_str)
    if not match:
        match_num = re.search(r'(\d+(?:\.\d+)?|\.\d+)', size_str)
        if match_num:
            return int(float(match_num.group(1)))
        return 0
    
    val = float(match.group(1))
    unit = match.group(2)
    
    if 'G' in unit:
        return int(val * 1024 * 1024 * 1024)
    elif 'M' in unit:
        return int(val * 1024 * 1024)
    elif 'K' in unit:
        return int(val * 1024)
    else:
        return int(val)

def extract_size_from_desc(desc_text):
    if not desc_text:
        return 0, "Unknown"
    # Search for e.g. "Size 1.45 GiB" or "Size: 1.45 GB"
    match = re.search(r'(?:Size|Size:)\s*(\d+(?:\.\d+)?|\.\d+)\s*([a-zA-Z]+)', desc_text, re.IGNORECASE)
    if match:
        size_val = match.group(1)
        size_unit = match.group(2)
        return parse_size_to_bytes(f"{size_val} {size_unit}"), f"{size_val} {size_unit}"
    
    # Generic "1.45 GiB" search
    match = re.search(r'(\d+(?:\.\d+)?|\.\d+)\s*(GiB|MiB|KiB|GB|MB|KB|B)', desc_text, re.IGNORECASE)
    if match:
        return parse_size_to_bytes(match.group(0)), match.group(0)
        
    return 0, "Unknown"

class SimpleTorrent:
    def __init__(self, title, magnet, seeds, leechs, size_bytes, size_str):
        self.title = title
        self.magnet = magnet
        self.seeds = seeds
        self.leechs = leechs
        self.size_bytes = size_bytes
        self.size_str = size_str

# ==========================================
#          SCRAPER & SEARCH MODULES
# ==========================================
def parse_tpb_html(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    torrents = []
    
    table = soup.find('table', id='searchResult')
    rows = table.find_all('tr') if table else soup.find_all('tr')
        
    for row in rows:
        magnet_tag = row.find('a', href=lambda x: x and x.startswith('magnet:'))
        if not magnet_tag:
            continue
            
        magnet = magnet_tag['href']
        
        # Resolve title
        title = None
        det_name_div = row.find('div', class_='detName')
        if det_name_div:
            title_tag = det_name_div.find('a')
            if title_tag:
                title = title_tag.get_text(strip=True)
        
        if not title:
            # Fallback: check standard <a> tags in current row
            a_tags = row.find_all('a')
            for a in a_tags:
                href = a.get('href', '')
                if 'magnet:' not in href and (href.startswith('/torrent') or 'detName' in a.get('class', [])):
                    title = a.get_text(strip=True)
                    break
            if not title:
                title = row.get_text(strip=True).split('\n')[0]
                
        if not title:
            continue
            
        # Parse seeders and leechers
        seeds = 0
        leechs = 0
        tds = row.find_all('td')
        if len(tds) >= 4:
            try:
                seeds = int(tds[2].get_text(strip=True).replace(',', ''))
                leechs = int(tds[3].get_text(strip=True).replace(',', ''))
            except ValueError:
                pass
                
        # Parse size
        size_bytes = 0
        size_str = "Unknown"
        det_desc_font = row.find('font', class_='detDesc')
        if det_desc_font:
            desc_text = det_desc_font.get_text(strip=True)
            size_bytes, size_str = extract_size_from_desc(desc_text)
        else:
            row_text = row.get_text(strip=True)
            size_bytes, size_str = extract_size_from_desc(row_text)
            
        torrents.append(SimpleTorrent(
            title=title,
            magnet=magnet,
            seeds=seeds,
            leechs=leechs,
            size_bytes=size_bytes,
            size_str=size_str
        ))
        
    return torrents

def bs4_search(domain, query):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36'
    }
    query_quoted = urllib.parse.quote(query)
    urls_to_try = [
        f"{domain.rstrip('/')}/search/{query_quoted}/1/99/0",
        f"{domain.rstrip('/')}/s/?q={query_quoted}",
        f"{domain.rstrip('/')}/search.php?q={query_quoted}"
    ]
    
    last_error = None
    for url in urls_to_try:
        try:
            logging.info(f"Scraping TPB from: {url}")
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code == 200:
                torrents = parse_tpb_html(r.text)
                if torrents:
                    return torrents
            else:
                logging.warning(f"HTTP {r.status_code} for {url}")
        except Exception as e:
            last_error = e
            logging.warning(f"Failed to scrape {url}: {e}")
            
    if last_error:
        raise last_error
    return []

def search_tpb(query):
    last_err = None
    for domain in TPB_DOMAINS:
        print_info(f"Searching query '{query}' on mirror {domain}...")
        logging.info(f"Attempting search on {domain} for: {query}")
        
        # 1. Try with tpblite wrapper first if available
        if HAS_TPB_LITE:
            try:
                t = TPB(domain)
                results = t.search(query)
                torrents = []
                for res in results:
                    size_bytes = 0
                    if hasattr(res, 'byte_size'):
                        size_bytes = res.byte_size
                    elif hasattr(res, 'filesize'):
                        size_bytes = parse_size_to_bytes(res.filesize) if isinstance(res.filesize, str) else res.filesize
                    
                    torrents.append(SimpleTorrent(
                        title=res.title,
                        magnet=res.magnetlink,
                        seeds=res.seeds,
                        leechs=res.leeches,
                        size_bytes=size_bytes,
                        size_str=getattr(res, 'filesize', 'Unknown')
                    ))
                if torrents:
                    print_info(f"Found {len(torrents)} results via tpblite.")
                    return torrents
            except Exception as e:
                logging.warning(f"tpblite failed on domain {domain}: {e}")
                
        # 2. Try custom BeautifulSoup fallback scraper
        try:
            torrents = bs4_search(domain, query)
            if torrents:
                print_info(f"Found {len(torrents)} results via BeautifulSoup scraper.")
                return torrents
        except Exception as e:
            last_err = e
            print_warning(f"Domain {domain} failed: {e}")
            logging.error(f"Domain {domain} error: {e}")
            
    if last_err:
        print_error(f"All domains failed. Last error: {last_err}")
    return []

# ==========================================
#        CLEANING, PARSING & MATCHING
# ==========================================
def clean_title_for_comparison(title):
    title_clean = re.sub(r'\[.*?\]', '', title)
    title_clean = re.sub(r'\(.*?\)', '', title_clean)
    
    garbage_words = [
        'rartv', 'ettv', 'tgx', 'eztv', 'galaxytv', 'yts', 'psa', 'qxr', 
        'x264', 'x265', 'h264', 'h265', 'hevc', '1080p', '720p', '480p', 
        'web-dl', 'webrip', 'hdtv', 'bluray', 'brrip', 'dvdrip', 'dd5.1', 
        'dd+5.1', 'aac2.0', 'aac', 'mp3', 'dts'
    ]
    
    title_clean = title_clean.lower()
    for word in garbage_words:
        title_clean = re.sub(rf'\b{word}\b', '', title_clean)
        
    title_clean = re.sub(r'[\._\-]', ' ', title_clean)
    return ' '.join(title_clean.split()).strip()

def parse_torrent_info(title):
    title_lower = title.lower()
    
    # 1. S01E01-E03 / S01E01-03 multi-episode range
    match = re.search(r's(\d+)e(\d+)[-~]e?(\d+)', title_lower)
    if match:
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
        
    match = re.search(r's(\d+)e(\d+)[-~](\d+)', title_lower)
    if match:
        return int(match.group(1)), int(match.group(2)), int(match.group(3))

    # 2. S01E01
    match = re.search(r's(\d+)e(\d+)', title_lower)
    if match:
        return int(match.group(1)), int(match.group(2)), None
        
    # 3. 1x01
    match = re.search(r'(\d+)x(\d+)', title_lower)
    if match:
        return int(match.group(1)), int(match.group(2)), None
        
    # 4. Season 1 Episode 1
    match = re.search(r'season\s+(\d+)\s+episode\s+(\d+)', title_lower)
    if match:
        return int(match.group(1)), int(match.group(2)), None
        
    # 5. - 101 - or [101] or - 0101 - surround checks
    match = re.search(r'(?:[-_\[\s])(\d{1,2})(\d{2})(?:[-_\]\s])', title_lower)
    if match:
        return int(match.group(1)), int(match.group(2)), None

    return None, None, None

def is_season_pack(title, season):
    title_lower = title.lower()
    season_str_02 = f"s{season:02d}"
    season_str_simple = f"season {season}"
    
    if season_str_02 not in title_lower and season_str_simple not in title_lower:
        return False
        
    # Exclude single episode files unless it matches a range (e.g. S01E01-E10)
    if re.search(r'e\d+|x\d+|ep\d+|episode \d+', title_lower):
        if re.search(r'e\d+[-~]e?\d+|e\d+[-~]\d+', title_lower):
            return True
        return False
        
    return True

# ==========================================
#     QUALITY AND SIZE PRIORITIZATION
# ==========================================
def select_best_torrent(torrents, quality_filter=None):
    valid_torrents = [t for t in torrents if t.seeds >= MIN_SEEDERS]
    if not valid_torrents:
        # Smart Fallback: If all results have 0 seeds, the proxy mirror is likely hiding peer counts.
        # Bypass the threshold to ensure the download still functions.
        if torrents and all(t.seeds == 0 for t in torrents):
            logging.warning("All torrents returned 0 seeds. Mirror proxy might be hiding peer counts. Bypassing MIN_SEEDERS threshold.")
            valid_torrents = torrents
        else:
            return None

    # Filter strictly by quality if specified
    if quality_filter:
        q_lower = quality_filter.lower()
        valid_torrents = [t for t in valid_torrents if q_lower in t.title.lower()]
        if not valid_torrents:
            return None

    seen_magnets = set()
    deduped = []
    for t in valid_torrents:
        if t.magnet not in seen_magnets:
            seen_magnets.add(t.magnet)
            deduped.append(t)
    valid_torrents = deduped

    # If quality is explicitly selected by filter, choose smallest within that quality tier
    if quality_filter:
        valid_torrents.sort(key=lambda x: (x.size_bytes if x.size_bytes > 0 else float('inf'), -x.seeds))
        return valid_torrents[0]

    # Rule: prioritize 720p with the SMALLEST size
    torrents_720p = [t for t in valid_torrents if "720p" in t.title.lower()]
    if torrents_720p:
        torrents_720p.sort(key=lambda x: (x.size_bytes if x.size_bytes > 0 else float('inf'), -x.seeds))
        return torrents_720p[0]

    # Rule: fallback to 1080p with the SMALLEST size
    torrents_1080p = [t for t in valid_torrents if "1080p" in t.title.lower()]
    if torrents_1080p:
        torrents_1080p.sort(key=lambda x: (x.size_bytes if x.size_bytes > 0 else float('inf'), -x.seeds))
        return torrents_1080p[0]

    # Ultimate fallback: Sort other qualities by size (smallest first)
    valid_torrents.sort(key=lambda x: (x.size_bytes if x.size_bytes > 0 else float('inf'), -x.seeds))
    return valid_torrents[0]

# ==========================================
#           RESUME STATE TRACKING
# ==========================================
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Failed to load state: {e}")
    return {}

def save_state(state):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=4)
    except Exception as e:
        logging.error(f"Failed to save state: {e}")

def mark_episode_queued(state, show, season, episode):
    show_key = show.lower()
    if show_key not in state:
        state[show_key] = {"season_packs": [], "episodes": {}}
    
    episodes_dict = state[show_key]["episodes"]
    season_str = str(season)
    if season_str not in episodes_dict:
        episodes_dict[season_str] = []
    
    if episode not in episodes_dict[season_str]:
        episodes_dict[season_str].append(episode)
    save_state(state)

def mark_season_pack_queued(state, show, season):
    show_key = show.lower()
    if show_key not in state:
        state[show_key] = {"season_packs": [], "episodes": {}}
    
    if season not in state[show_key]["season_packs"]:
        state[show_key]["season_packs"].append(season)
    save_state(state)

def is_episode_queued(state, show, season, episode):
    show_key = show.lower()
    if show_key not in state:
        return False
    episodes_dict = state[show_key].get("episodes", {})
    return episode in episodes_dict.get(str(season), [])

def is_season_pack_queued(state, show, season):
    show_key = show.lower()
    if show_key not in state:
        return False
    return season in state[show_key].get("season_packs", [])

# ==========================================
#         QBITTORRENT INTEGRATION
# ==========================================
def get_qbt_client():
    if not HAS_QBT:
        raise ImportError("qbittorrent-api is not installed.")
    conn_info = {
        "host": QBT_HOST,
        "port": QBT_PORT,
        "username": QBT_USER,
        "password": QBT_PASS
    }
    client = qbittorrentapi.Client(**conn_info)
    client.auth_log_in()
    return client

def get_active_torrents(client, category, tag):
    all_torrents = client.torrents_info(category=category, tag=tag)
    return [t for t in all_torrents if t.state in ['downloading', 'stalledDL', 'metaDL', 'checkingDL']]

def add_torrent_to_qbt(client, magnet, save_path, category, tag):
    active = get_active_torrents(client, category, tag)
    is_paused = False
    if len(active) >= MAX_CONCURRENT:
        is_paused = True
        print_info(f"Active slot threshold ({MAX_CONCURRENT}) reached. Queueing in paused state.")
    else:
        print_info(f"Slot open. Adding in active state.")
        
    retries = 2
    for attempt in range(retries + 1):
        try:
            client.torrents_add(
                urls=magnet,
                save_path=save_path,
                category=category,
                tags=tag,
                is_paused=is_paused
            )
            print_success(f"Torrent successfully sent to qBittorrent!")
            return True
        except Exception as e:
            if attempt < retries:
                print_warning(f"Error sending to qBittorrent (Attempt {attempt+1}/{retries+1}). Retrying... Error: {e}")
                time.sleep(3)
            else:
                print_error(f"Failed to add torrent to qBittorrent after retries. Error: {e}")
                logging.error(f"qBittorrent add failure: {e}")
                return False

def monitor_downloads(client, category, tag):
    print_header("Monitoring Active Downloads (Press Ctrl+C to stop)")
    try:
        while True:
            all_torrents = client.torrents_info(category=category, tag=tag)
            if not all_torrents:
                print_info("No torrents found in active monitor queue.")
                break
                
            active = [t for t in all_torrents if t.state in ['downloading', 'stalledDL', 'metaDL', 'checkingDL']]
            waiting = [t for t in all_torrents if t.state in ['pausedDL']]
            completed = [t for t in all_torrents if t.state in ['pausedUP', 'checkingUP', 'stalledUP', 'uploading', 'checkingResumeData']]
            
            # Queue management: Resume next paused when active slots free up
            slots_available = MAX_CONCURRENT - len(active)
            if slots_available > 0 and waiting:
                waiting.sort(key=lambda x: x.get('added_on', 0))
                to_resume = waiting[:slots_available]
                for wt in to_resume:
                    print_success(f"Slot opened! Resuming torrent: {wt.name}")
                    client.torrents_resume(hashes=wt.hash)
                    
            # Clear terminal & print progress dashboard
            print("\033[H\033[J", end="") 
            print(f"{Colors.BOLD}{Colors.HEADER}=== TV Series Getter Queue Monitor ==={Colors.ENDC}")
            print(f"Active Slots: {Colors.CYAN}{len(active)}/{MAX_CONCURRENT}{Colors.ENDC} running | {Colors.WARNING}{len(waiting)} queued{Colors.ENDC} | {Colors.GREEN}{len(completed)} completed{Colors.ENDC}\n")
            
            print(f"{Colors.BOLD}{'Name':<50} | {'Progress':<10} | {'Size':<10} | {'Speed':<12} | {'Status':<15}{Colors.ENDC}")
            print("-" * 105)
            
            all_completed = True
            for t in all_torrents:
                progress_pct = t.progress * 100
                speed_str = f"{t.dlspeed / 1024 / 1024:.2f} MB/s" if t.dlspeed > 0 else "0.00 B/s"
                size_mb = f"{t.size / 1024 / 1024:.1f} MB"
                
                status = t.state
                if status == 'pausedDL':
                    status_color = Colors.WARNING
                    status_lbl = "Queued"
                elif status in ['downloading', 'stalledDL', 'metaDL', 'checkingDL']:
                    status_color = Colors.CYAN
                    status_lbl = "Downloading"
                elif progress_pct >= 100.0 or status in ['uploading', 'stalledUP', 'pausedUP']:
                    status_color = Colors.GREEN
                    status_lbl = "Complete"
                else:
                    status_color = Colors.FAIL
                    status_lbl = status
                    
                if progress_pct < 100.0 and status != 'pausedUP':
                    all_completed = False
                    
                name_trimmed = t.name[:47] + "..." if len(t.name) > 50 else t.name
                print(f"{name_trimmed:<50} | {progress_pct:>8.1f}% | {size_mb:>10} | {speed_str:>12} | {status_color}{status_lbl:<15}{Colors.ENDC}")
                
            print("-" * 105)
            
            if all_completed:
                print_success("\nAll downloads have completed successfully!")
                break
                
            time.sleep(5)
            
    except KeyboardInterrupt:
        print_warning("\nMonitoring stopped. Downloads will continue running in qBittorrent background.")

# ==========================================
#             CLI INTERACTIVE MODE
# ==========================================
def interactive_select(torrents, item_name):
    print_header(f"Curated list for: {item_name}")
    for idx, t in enumerate(torrents[:10], 1):
        color = Colors.CYAN
        if "720p" in t.title.lower():
            color = Colors.GREEN
        elif "1080p" in t.title.lower():
            color = Colors.BLUE
            
        print(f"[{idx}] {color}{t.title}{Colors.ENDC}")
        print(f"    Seeds: {t.seeds} | Leechs: {t.leechs} | Size: {t.size_str}")
        
    print(f"[s] Skip this download")
    print(f"[c] Cancel script")
    
    while True:
        choice = input(f"\nEnter choice (1-{min(10, len(torrents))}, s, c): ").strip().lower()
        if choice == 's':
            return None
        elif choice == 'c':
            print_info("Exiting script cleanly.")
            sys.exit(0)
        try:
            idx = int(choice)
            if 1 <= idx <= min(10, len(torrents)):
                return torrents[idx-1]
        except ValueError:
            pass
        print_error("Invalid selection. Please try again.")

# ==========================================
#               MAIN PROCESS
# ==========================================
def main():
    parser = argparse.ArgumentParser(
        description="Automated TV Series Downloader from The Pirate Bay with smart fallback."
    )
    parser.add_argument("show_name", type=str, nargs="?", help="Name of the TV Show.")
    parser.add_argument("--dry-run", action="store_true", help="Search and display matches, do not download.")
    parser.add_argument("--interactive", action="store_true", help="Interactively select matching torrents.")
    parser.add_argument("--quality", type=str, default=None, help="Filter torrents strictly by quality (e.g. 1080p, 720p).")
    parser.add_argument("--seasons", type=str, default=None, help="Season range (e.g. '1-3') or single season ('2').")
    parser.add_argument("--force", action="store_true", help="Ignore state tracking file.")
    parser.add_argument("--skip-episodes", type=str, default=None,
                        help="Episodes to skip. Comma-separated numbers or ranges (e.g. '1,2,3' or '1-3' or '1-3,7,10-12').")
    args = parser.parse_args()

    # Parse skip-episodes into a set
    skip_episodes = set()
    if args.skip_episodes:
        for part in args.skip_episodes.split(','):
            part = part.strip()
            if '-' in part:
                try:
                    start, end = map(int, part.split('-'))
                    skip_episodes.update(range(start, end + 1))
                except ValueError:
                    print_error(f"Invalid skip range: '{part}'")
                    sys.exit(1)
            else:
                try:
                    skip_episodes.add(int(part))
                except ValueError:
                    print_error(f"Invalid skip episode number: '{part}'")
                    sys.exit(1)
        print_info(f"Will skip episodes: {sorted(skip_episodes)}")

    show_name = args.show_name
    if not show_name:
        print_header("Smart TV Series Downloader")
        show_name = input("Enter TV Show Name: ").strip()
        if not show_name:
            print_error("Show name cannot be empty.")
            sys.exit(1)

    # Initialize connection
    qbt = None
    if not args.dry_run:
        if not HAS_QBT:
            print_error("qbittorrent-api is not installed! Run 'pip install qbittorrent-api' or run with --dry-run.")
            sys.exit(1)
        try:
            qbt = get_qbt_client()
            print_success(f"Connected to qBittorrent WebUI. Version: {qbt.app.version}")
        except Exception as e:
            print_error(f"Failed to connect to qBittorrent WebUI: {e}")
            print_warning("Ensure qBittorrent is running locally with WebUI enabled at port 8080.")
            sys.exit(1)

    state = load_state()
    torrents_queued_count = 0

    # Determine Seasons Range
    if args.seasons:
        if '-' in args.seasons:
            try:
                start, end = map(int, args.seasons.split('-'))
                seasons = list(range(start, end + 1))
            except ValueError:
                print_error(f"Invalid season range: {args.seasons}")
                sys.exit(1)
        elif ',' in args.seasons:
            try:
                seasons = list(map(int, args.seasons.split(',')))
            except ValueError:
                print_error(f"Invalid season list: {args.seasons}")
                sys.exit(1)
        else:
            try:
                seasons = [int(args.seasons)]
            except ValueError:
                print_error(f"Invalid season: {args.seasons}")
                sys.exit(1)
    else:
        seasons = list(range(1, 100))  # Scan sequentially

    category = "series-getter"
    tag = show_name.lower().replace(" ", "-")
    dry_run_list = []

    try:
        for s in seasons:
            print_header(f"Processing Season {s:02d}")
            
            # Check if season pack is already processed in state
            if not args.force and is_season_pack_queued(state, show_name, s):
                print_info(f"Season {s:02d} pack already queued in state. Skipping.")
                continue

            # 1. Season Pack Search & Evaluation
            print_info(f"Searching for Season {s:02d} pack torrents...")
            pack_queries = [
                f"{show_name} S{s:02d} complete",
                f"{show_name} Season {s} complete",
                f"{show_name} S{s:02d}",
                f"{show_name} Season {s}"
            ]
            
            pack_results = []
            for query in pack_queries:
                results = search_tpb(query)
                for r in results:
                    if is_season_pack(r.title, s):
                        pack_results.append(r)
                if pack_results:
                    break

            best_pack = None
            if pack_results:
                if args.interactive:
                    best_pack = interactive_select(pack_results, f"Season {s:02d} Complete Pack")
                else:
                    best_pack = select_best_torrent(pack_results, args.quality)

            if best_pack:
                print_success(f"Selected Season Pack: {best_pack.title} (Seeds: {best_pack.seeds}, Size: {best_pack.size_str})")
                if args.dry_run:
                    print_info(f"[DRY-RUN] Would queue magnet for: {best_pack.title}")
                    dry_run_list.append({
                        "type": "Season Pack",
                        "id": f"Season {s:02d}",
                        "title": best_pack.title,
                        "size": best_pack.size_str,
                        "seeds": best_pack.seeds
                    })
                    mark_season_pack_queued(state, show_name, s)
                else:
                    save_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, show_name, f"Season {s:02d}"))
                    os.makedirs(save_path, exist_ok=True)
                    if add_torrent_to_qbt(qbt, best_pack.magnet, save_path, category, tag):
                        mark_season_pack_queued(state, show_name, s)
                        # Complete all episodes in state to avoid episode-by-episode scans
                        for ep in range(1, 100):
                            mark_episode_queued(state, show_name, s, ep)
                        torrents_queued_count += 1
                continue

            # 2. Episode-by-Episode Fallback
            print_warning(f"No complete Season {s:02d} packs found. Falling back to episode-by-episode download.")
            
            ep = 1
            consecutive_failures = 0
            
            while True:
                if ep in skip_episodes:
                    print_info(f"Episode S{s:02d}E{ep:02d} skipped by --skip-episodes.")
                    ep += 1
                    consecutive_failures = 0
                    continue

                if not args.force and is_episode_queued(state, show_name, s, ep):
                    print_info(f"Episode S{s:02d}E{ep:02d} already downloaded/queued. Skipping.")
                    ep += 1
                    consecutive_failures = 0
                    continue

                ep_query = f"{show_name} S{s:02d}E{ep:02d}"
                ep_results = search_tpb(ep_query)
                
                if not ep_results:
                    consecutive_failures += 1
                    print_info(f"No results found for S{s:02d}E{ep:02d} (Failure {consecutive_failures}/3).")
                    if consecutive_failures >= 3:
                        print_info(f"Three consecutive episode misses. Concluding Season {s:02d} sweep.")
                        break
                    ep += 1
                    continue

                consecutive_failures = 0
                selected_ep = None
                if args.interactive:
                    selected_ep = interactive_select(ep_results, f"S{s:02d}E{ep:02d}")
                else:
                    selected_ep = select_best_torrent(ep_results, args.quality)

                if selected_ep:
                    print_success(f"Selected S{s:02d}E{ep:02d}: {selected_ep.title} (Seeds: {selected_ep.seeds}, Size: {selected_ep.size_str})")
                    if args.dry_run:
                        print_info(f"[DRY-RUN] Would queue episode magnet: {selected_ep.title}")
                        dry_run_list.append({
                            "type": "Episode",
                            "id": f"S{s:02d}E{ep:02d}",
                            "title": selected_ep.title,
                            "size": selected_ep.size_str,
                            "seeds": selected_ep.seeds
                        })
                        mark_episode_queued(state, show_name, s, ep)
                    else:
                        save_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, show_name, f"Season {s:02d}"))
                        os.makedirs(save_path, exist_ok=True)
                        if add_torrent_to_qbt(qbt, selected_ep.magnet, save_path, category, tag):
                            mark_episode_queued(state, show_name, s, ep)
                            # Handle multi-episode torrents matching range S01E01-E03
                            ep_season, ep_start, ep_end = parse_torrent_info(selected_ep.title)
                            if ep_end and ep_end > ep_start:
                                for extra_ep in range(ep_start + 1, ep_end + 1):
                                    mark_episode_queued(state, show_name, s, extra_ep)
                                ep = ep_end  # Fast-forward episode loop
                            torrents_queued_count += 1
                else:
                    print_warning(f"No valid/seeding torrent matches found for S{s:02d}E{ep:02d}.")

                ep += 1

            # If season list was auto-scanned and we couldn't find a season pack AND no episode 1, stop auto season iteration
            if not args.seasons and ep == 1 + consecutive_failures:
                print_info(f"No files or packs found at all for Season {s:02d}. Ending TV series scan.")
                break

    except KeyboardInterrupt:
        print_warning("\nProcess interrupted by user gracefully. Saving progress state.")

    # Concurrency and Live Monitor Loop
    if not args.dry_run and torrents_queued_count > 0 and qbt is not None:
        try:
            monitor_downloads(qbt, category, tag)
        except Exception as e:
            print_error(f"Error in background monitor process: {e}")

    # Print Dry-Run Summary if applicable
    if args.dry_run:
        if dry_run_list:
            print_header("Dry-Run Download Plan Summary")
            print(f"{Colors.BOLD}{'Type':<12} | {'ID':<8} | {'Selected Torrent Name':<55} | {'Size':<10} | {'Seeds':<6}{Colors.ENDC}")
            print("-" * 100)
            for item in dry_run_list:
                title_trimmed = item['title'][:52] + "..." if len(item['title']) > 55 else item['title']
                type_color = Colors.CYAN if item['type'] == "Episode" else Colors.GREEN
                print(f"{type_color}{item['type']:<12}{Colors.ENDC} | {item['id']:<8} | {title_trimmed:<55} | {item['size']:>10} | {item['seeds']:>6}")
            print("-" * 100)
            print_success(f"Total matching items that would be downloaded: {len(dry_run_list)}")
        else:
            print_warning("\nDry-run finished. No matching seasons/episodes found to download.")

    print_success("\nSeries Getter task completed.")

if __name__ == "__main__":
    main()
