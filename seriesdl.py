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
import shutil
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

TMDB_API_KEY = ""  # Get free key from https://www.themoviedb.org/settings/api
TMDB_BASE_URL = "https://api.themoviedb.org/3"

STATE_FILE = "series_getter_state.json"
LOG_FILE = "series_getter.log"
LOG_MAX_BYTES = 5 * 1024 * 1024  # rotate log after ~5 MB

SEARCH_CACHE_MAX = 512  # cap cached TPB queries to keep memory bounded
TITLE_MATCH = True      # filter search results by show name to avoid false positives

REQUESTS_TIMEOUT = 15
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')

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
def rotate_log_if_needed():
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) >= LOG_MAX_BYTES:
            os.replace(LOG_FILE, LOG_FILE + ".old")
    except Exception:
        pass

rotate_log_if_needed()
logging.basicConfig(
    filename=LOG_FILE,
    filemode='a',
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Quiet noisy third-party loggers (urllib3 retries are handled by us anyway)
for noisy in ('urllib3', 'requests', 'qbittorrentapi'):
    logging.getLogger(noisy).setLevel(logging.WARNING)

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

def _enable_windows_colors():
    """Enable ANSI escape processing in the Windows console via VT mode."""
    if os.name != 'nt':
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass

_enable_windows_colors()

USE_COLORS = hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()

def ansi_strip(text):
    return re.sub(r'\x1b\[[0-9;]*m', '', text)

def pad_right(text, width):
    """Right-pad text to width, ignoring ANSI escape codes."""
    return text + ' ' * max(0, width - len(ansi_strip(text)))

def print_info(msg):
    if not USE_COLORS:
        print(f"[INFO] {msg}")
    else:
        print(f"{Colors.BLUE}[INFO]{Colors.ENDC} {msg}")

def print_success(msg):
    if not USE_COLORS:
        print(f"[SUCCESS] {msg}")
    else:
        print(f"{Colors.GREEN}[SUCCESS]{Colors.ENDC} {Colors.BOLD}{msg}{Colors.ENDC}")

def print_warning(msg):
    if not USE_COLORS:
        print(f"[WARNING] {msg}")
    else:
        print(f"{Colors.WARNING}[WARNING]{Colors.ENDC} {msg}")

def print_error(msg):
    if not USE_COLORS:
        print(f"[ERROR] {msg}")
    else:
        print(f"{Colors.FAIL}[ERROR]{Colors.ENDC} {msg}")

def print_header(msg):
    if not USE_COLORS:
        print(f"\n=== {msg} ===\n")
    else:
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
        return parse_size_to_bytes(match.group(0)), match.group(0).replace('\xa0', ' ')

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
_session = requests.Session()
_session.headers.update({
    'User-Agent': USER_AGENT,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.8',
})
SEARCH_CACHE = {}

def parse_tpb_html(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    torrents = []

    table = soup.find('table', id='searchResult')
    rows = table.find_all('tr') if table else []
    if not rows:
        # Fallback: any table row that actually contains a magnet link.
        rows = [r for r in soup.find_all('tr')
                if r.find('a', href=lambda x: x and x.startswith('magnet:'))]

    for row in rows:
        magnet_tag = row.find('a', href=lambda x: x and x.startswith('magnet:'))
        if not magnet_tag:
            continue

        magnet = magnet_tag['href']
        tds = row.find_all('td')

        # --- Title: prefer the anchor linking to /torrent/... ---
        title = None
        title_anchor = row.find('a', href=lambda x: x and '/torrent/' in x)
        if title_anchor:
            title = title_anchor.get_text(strip=True)
        if not title:
            det_name_div = row.find('div', class_='detName')
            if det_name_div:
                title_tag = det_name_div.find('a')
                if title_tag:
                    title = title_tag.get_text(strip=True)
        if not title:
            # Fallback: first non-magnet link in the row
            for a in row.find_all('a'):
                href = a.get('href', '')
                if href and 'magnet:' not in href and not href.startswith('/browse'):
                    title = a.get_text(strip=True)
                    break
        if not title:
            title = row.get_text(strip=True).split('\n')[0]
        if not title:
            continue

        # --- Size: first cell whose text contains a size value ---
        size_bytes = 0
        size_str = "Unknown"
        for td in tds:
            size_bytes, size_str = extract_size_from_desc(td.get_text(strip=True))
            if size_bytes > 0:
                break

        # --- Seeders / leechers: the last two numeric cells ---
        # (mirrors display them either as [seeds][leechs] or [size][seeds][leechs])
        seeds = 0
        leechs = 0
        numeric_cells = []
        for td in tds:
            text = td.get_text(strip=True).replace(',', '').replace(' ', '')
            if text.isdigit():
                numeric_cells.append(int(text))
        if len(numeric_cells) >= 2:
            leechs = numeric_cells[-1]
            seeds = numeric_cells[-2]
        elif len(numeric_cells) == 1:
            seeds = numeric_cells[0]

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
            r = _session.get(url, timeout=REQUESTS_TIMEOUT)
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

def cached_search(query, show_name=None):
    """Search TPB with an in-memory cache and optional show-name filtering.

    Reuses results when the same query is requested again (e.g. when the user
    re-enters the review/download loop), avoiding repeated mirror scraping.
    """
    key = (query, show_name or "")
    if key in SEARCH_CACHE:
        results = SEARCH_CACHE[key]
        print_info(f"Reusing cached results for '{query}' ({len(results)} items).")
        return results

    results = search_tpb(query)
    if show_name and TITLE_MATCH:
        results = [r for r in results if title_matches_show(r.title, show_name)]

    if len(SEARCH_CACHE) >= SEARCH_CACHE_MAX:
        SEARCH_CACHE.clear()
    SEARCH_CACHE[key] = results
    return results

def title_matches_show(title, show_name):
    """Return True if a torrent title plausibly refers to the requested show.

    Compares significant words (length >= 3) of the cleaned show name against
    the cleaned torrent title. Used to avoid queueing torrents for similarly
    named shows. Disable with --no-title-match.
    """
    if not show_name:
        return True
    tokens = [w for w in re.sub(r'[^a-z0-9 ]', ' ', show_name.lower()).split() if len(w) >= 3]
    if not tokens:
        return True
    words = set(clean_title_for_comparison(title).split())
    return all(tok in words for tok in tokens)

def verify_domains(domains):
    """Probe every configured mirror and report reachability."""
    print_header("Mirror Availability Check")
    reachable = 0
    for domain in domains:
        try:
            r = _session.get(f"{domain.rstrip('/')}/", timeout=10)
            if r.status_code == 200:
                reachable += 1
                print_success(f"{pad_right(domain, 42)}OK")
            else:
                print_warning(f"{pad_right(domain, 42)}HTTP {r.status_code}")
        except Exception as e:
            print_error(f"{pad_right(domain, 42)}{type(e).__name__}: {e}")
    print_info(f"{reachable}/{len(domains)} mirror(s) reachable.")
    return reachable

# ==========================================
#         TMDB SERIES DISCOVERY
# ==========================================
def tmdb_search_show(query, api_key):
    url = f"{TMDB_BASE_URL}/search/tv"
    params = {"api_key": api_key, "query": query, "language": "en-US", "page": 1}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            return None
        best = results[0]
        return {
            "id": best["id"],
            "name": best["name"],
            "overview": best.get("overview", ""),
            "first_air_date": best.get("first_air_date", ""),
            "vote_average": best.get("vote_average", 0),
        }
    except Exception as e:
        logging.error(f"TMDB search failed: {e}")
        return None

def tmdb_get_seasons(show_id, api_key):
    url = f"{TMDB_BASE_URL}/tv/{show_id}"
    params = {"api_key": api_key, "language": "en-US"}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        seasons = []
        for s in data.get("seasons", []):
            if s["season_number"] == 0:
                continue  # skip specials
            seasons.append({
                "number": s["season_number"],
                "name": s.get("name", f"Season {s['season_number']}"),
                "episode_count": s.get("episode_count", 0),
                "air_date": s.get("air_date", ""),
            })
        return {
            "total_seasons": len(seasons),
            "total_episodes": sum(s["episode_count"] for s in seasons),
            "seasons": seasons,
        }
    except Exception as e:
        logging.error(f"TMDB season fetch failed: {e}")
        return None

def discover_series(show_name, api_key):
    print_info(f"Querying TMDB for series info: '{show_name}'...")
    show = tmdb_search_show(show_name, api_key)
    if not show:
        print_warning("TMDB search returned no results.")
        return None

    match_str = f"{show['name']} ({show.get('first_air_date', '?')[:4]}) [Score: {show['vote_average']}]"
    print_success(f"Best match: {match_str}")
    if show.get("overview"):
        overview = show["overview"][:150] + ("..." if len(show["overview"]) > 150 else "")
        print_info(f"  {overview}")

    info = tmdb_get_seasons(show["id"], api_key)
    if not info:
        print_warning("Could not fetch season details from TMDB.")
        return None

    print_success(f"Found {info['total_seasons']} seasons, {info['total_episodes']} episodes total.\n")
    for s in info["seasons"]:
        print(f"  Season {s['number']:02d} - {s['episode_count']:>2} episodes ({s['name']})")

    return {"show": show, "info": info}

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
        # Write to a temp file and atomically replace to avoid a corrupt
        # state file if the process is killed mid-write.
        tmp_path = STATE_FILE + ".tmp"
        with open(tmp_path, 'w') as f:
            json.dump(state, f, indent=4)
        os.replace(tmp_path, STATE_FILE)
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
        "password": QBT_PASS,
        "reqargs": {"timeout": REQUESTS_TIMEOUT},
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
                waiting.sort(key=lambda x: x.added_on)
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
#         INTERACTIVE EPISODE PICKER
# ==========================================
def parse_episode_input(text, max_ep):
    episodes = set()
    for part in text.split(','):
        part = part.strip()
        if '-' in part:
            try:
                start, end = part.split('-', 1)
                start, end = int(start.strip()), int(end.strip())
                episodes.update(range(max(1, start), min(end, max_ep) + 1))
            except ValueError:
                print_error(f"Invalid range: '{part}'")
        else:
            try:
                val = int(part)
                if 1 <= val <= max_ep:
                    episodes.add(val)
            except ValueError:
                print_error(f"Invalid episode number: '{part}'")
    return episodes

def interactive_episode_picker(discovery):
    seasons = discovery["info"]["seasons"]
    show_name = discovery["show"]["name"]
    selection = {}

    for s in seasons:
        selection[s["number"]] = set()

    while True:
        print_header(f"Episode Picker: {show_name}")
        print(f"  {'#':<4} {'Season':<12} {'Episodes':<10} {'Selected':<10} {'Action'}")
        print("  " + "-" * 55)
        for s in seasons:
            num = s["number"]
            ep_count = s["episode_count"]
            sel_count = len(selection[num])
            marker = Colors.GREEN if sel_count == ep_count else (Colors.WARNING if sel_count > 0 else Colors.FAIL)
            status = f"{marker}{sel_count}/{ep_count}{Colors.ENDC}"
            print(f"  {num:<4} Season {num:02d}   {ep_count:>3} eps    {pad_right(status, 16)}    [A]ll [N]one [S]elect")

        total_selected = sum(len(v) for v in selection.values())
        print(f"\n  {Colors.BOLD}Total selected: {total_selected} episodes{Colors.ENDC}")
        print(f"\n  Commands: season# + A/N/S (e.g. '1A', '2S', '3N')")
        print(f"  {Colors.GREEN}[PROCEED]{Colors.ENDC} to review  |  {Colors.FAIL}[CANCEL]{Colors.ENDC} to quit")
        print(f"  {Colors.CYAN}[ALL]{Colors.ENDC} select all seasons  |  {Colors.WARNING}[NONE]{Colors.ENDC} clear all")

        choice = input("\n  > ").strip().lower()

        if choice == 'proceed' or choice == 'p':
            if total_selected == 0:
                print_error("No episodes selected! Pick at least one.")
                continue
            return selection
        elif choice == 'cancel' or choice == 'c':
            print_info("Cancelled by user.")
            sys.exit(0)
        elif choice == 'all':
            for s in seasons:
                selection[s["number"]] = set(range(1, s["episode_count"] + 1))
            print_success("All episodes selected.")
        elif choice == 'none':
            for s in seasons:
                selection[s["number"]] = set()
            print_info("All selections cleared.")
        else:
            match = re.match(r'^(\d+)([ans])$', choice)
            if match:
                s_num = int(match.group(1))
                action = match.group(2)
                season_data = next((s for s in seasons if s["number"] == s_num), None)
                if not season_data:
                    print_error(f"Season {s_num} doesn't exist.")
                    continue
                max_ep = season_data["episode_count"]
                if action == 'a':
                    selection[s_num] = set(range(1, max_ep + 1))
                    print_success(f"Season {s_num}: all {max_ep} episodes selected.")
                elif action == 'n':
                    selection[s_num] = set()
                    print_info(f"Season {s_num}: cleared.")
                elif action == 's':
                    ep_input = input(f"  Enter episodes for Season {s_num} (e.g. 1,3,5-8): ").strip()
                    if ep_input:
                        selection[s_num] = parse_episode_input(ep_input, max_ep)
                        print_success(f"Season {s_num}: {len(selection[s_num])} episodes selected.")
            else:
                print_error("Invalid command. Use: <season#><A/N/S> (e.g. 1A, 2S, 3N)")
# ==========================================
#               MAIN PROCESS
# ==========================================
def try_find_season_pack(show_name, season, quality=None, interactive=False):
    """Search for a complete season pack torrent, returning the best one or None."""
    pack_queries = [
        f"{show_name} S{season:02d} complete",
        f"{show_name} Season {season} complete",
        f"{show_name} S{season:02d}",
        f"{show_name} Season {season}",
    ]
    pack_results = []
    for query in pack_queries:
        results = cached_search(query, show_name)
        for r in results:
            if is_season_pack(r.title, season):
                pack_results.append(r)
        if pack_results:
            break

    if not pack_results:
        return None
    if interactive:
        return interactive_select(pack_results, f"Season {season:02d} Complete Pack")
    return select_best_torrent(pack_results, quality)

def resolve_plan_entry(item, show_name, quality):
    s_num = item["season"]
    eps = item["episodes"]

    print_info(f"Searching TPB for Season {s_num:02d} ({len(eps)} episodes)...")

    # If all episodes for the season are selected, try season pack first
    season_data = item.get("season_episode_count", 0)
    all_eps = len(eps) == season_data and season_data > 0

    if all_eps:
        best_pack = try_find_season_pack(show_name, s_num, quality)
        if best_pack:
            item["type"] = "season_pack"
            item["title"] = best_pack.title
            item["size_str"] = best_pack.size_str
            item["seeds"] = best_pack.seeds
            item["magnet"] = best_pack.magnet
            item["all_episodes"] = True
            print_success(f"  Found pack: {best_pack.title}")
            return

    # Episode-by-episode for selected episodes
    item["type"] = "episodes"
    item["episode_torrents"] = {}
    for ep in sorted(eps):
        ep_query = f"{show_name} S{s_num:02d}E{ep:02d}"
        ep_results = cached_search(ep_query, show_name)
        if not ep_results:
            print_warning(f"  No results for S{s_num:02d}E{ep:02d}")
            continue
        best = select_best_torrent(ep_results, quality)
        if best:
            item["episode_torrents"][ep] = best
            print_success(f"  E{ep:02d}: {best.title}")
        else:
            print_warning(f"  No valid torrent for S{s_num:02d}E{ep:02d}")

def populate_plan_from_tp(plan, show_name, quality):
    print_header("Searching The Pirate Bay for selected episodes...")
    for item in plan:
        resolve_plan_entry(item, show_name, quality)

def display_plan(plan, show_name):
    total_eps = 0
    for item in plan:
        if item["type"] == "season_pack":
            total_eps += item.get("season_episode_count", len(item.get("episodes", [])))
        elif item["type"] == "episodes":
            total_eps += len(item.get("episode_torrents", {}))

    print_header(f"Download Plan: {show_name}")
    print(f"  {Colors.BOLD}{len(plan)} season(s), ~{total_eps} episode(s){Colors.ENDC}\n")

    print(f"  {'#':<4} {'Type':<14} {'Season':<8} {'Details':<40} {'Seeds':<6} {'Size'}")
    print("  " + "-" * 85)

    for idx, item in enumerate(plan, 1):
        s_num = item["season"]
        if item["type"] == "season_pack":
            title_short = (item.get("title") or "N/A")[:37] + "..." if len(item.get("title") or "") > 40 else item.get("title") or "N/A"
            print(f"  {idx:<4} {Colors.GREEN}Season Pack{Colors.ENDC}  S{s_num:02d}    {title_short:<40} {item.get('seeds', 0):<6} {item.get('size_str', '?')}")
        elif item["type"] == "episodes":
            torrents = item.get("episode_torrents", {})
            if not torrents:
                print(f"  {idx:<4} {Colors.FAIL}Episodes{Colors.ENDC}    S{s_num:02d}    {Colors.FAIL}No torrents found{Colors.ENDC}")
            else:
                ep_list = ", ".join(f"E{e:02d}" for e in sorted(torrents.keys()))
                total_seeds = sum(t.seeds for t in torrents.values())
                total_size = sum(t.size_bytes for t in torrents.values())
                size_str = f"{total_size / (1024*1024*1024):.2f} GB" if total_size > 0 else "?"
                print(f"  {idx:<4} {Colors.CYAN}Episodes{Colors.ENDC}     S{s_num:02d}    {ep_list:<40} {total_seeds:<6} {size_str}")
        else:
            print(f"  {idx:<4} {'Unknown':<14} S{s_num:02d}")

def review_loop(plan, show_name, discovery):
    while True:
        display_plan(plan, show_name)
        print(f"\n  Commands:")
        print(f"  {Colors.FAIL}[R #]{Colors.ENDC}       Remove a season entry from the plan")
        print(f"  {Colors.GREEN}[GO]{Colors.ENDC}        Approve and start downloading")
        print(f"  {Colors.WARNING}[CANCEL]{Colors.ENDC}    Abort everything")
        print(f"  {Colors.CYAN}[ADD]{Colors.ENDC}       Go back and add more episodes")

        choice = input("\n  > ").strip().lower()

        if choice == 'go' or choice == 'g':
            active = [item for item in plan if item.get("title") or item.get("episode_torrents")]
            if not active:
                print_error("No resolved torrents in plan! Add more or cancel.")
                continue
            return True
        elif choice == 'cancel' or choice == 'c':
            print_info("Download cancelled by user.")
            return False
        elif choice == 'add':
            return "add_more"
        elif choice.startswith('r '):
            try:
                idx = int(choice.split()[1]) - 1
                if 0 <= idx < len(plan):
                    removed = plan.pop(idx)
                    print_info(f"Removed Season {removed['season']:02d} from plan.")
                else:
                    print_error("Invalid item number.")
            except (ValueError, IndexError):
                print_error("Usage: R <number> (e.g. R 3)")
        else:
            print_error("Unknown command. Use GO, CANCEL, R <#>, or ADD")

def _main():
    global MIN_SEEDERS, MAX_CONCURRENT, DOWNLOAD_DIR, TPB_DOMAINS, TITLE_MATCH
    global QBT_HOST, QBT_PORT, QBT_USER, QBT_PASS

    parser = argparse.ArgumentParser(
        description="Automated TV Series Downloader from The Pirate Bay with smart fallback.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("show_name", type=str, nargs="?", help="Name of the TV Show.")
    parser.add_argument("--version", action="version", version="seriesdl 1.1.0")
    parser.add_argument("--dry-run", action="store_true", help="Search and display matches, do not download.")
    parser.add_argument("--interactive", action="store_true", help="Interactively select matching torrents.")
    parser.add_argument("--quality", type=str, default=None, help="Filter by quality (e.g. 1080p, 720p).")
    parser.add_argument("--seasons", type=str, default=None, help="Season range (e.g. '1-3') or single season ('2').")
    parser.add_argument("--force", action="store_true", help="Ignore state tracking file.")
    parser.add_argument("--skip-episodes", type=str, default=None,
                        help="Episodes to skip. Comma-separated numbers or ranges.")
    parser.add_argument("--api-key", type=str, default=None,
                        help="TMDB API key (overrides config). Get free key from themoviedb.org/settings/api.")
    parser.add_argument("--domains", type=str, default=None,
                        help="Comma-separated list of TPB mirrors to try (overrides config).")
    parser.add_argument("--verify-domains", action="store_true",
                        help="Check which TPB mirrors are reachable, then exit.")
    parser.add_argument("--download-dir", type=str, default=None,
                        help="Root folder to save downloads into (default: ./downloads).")
    parser.add_argument("--category", type=str, default=None,
                        help="qBittorrent category/label to use (default: series-getter).")
    parser.add_argument("--tag", type=str, default=None,
                        help="qBittorrent tag to apply. Defaults to the lowercase show name.")
    parser.add_argument("--max-concurrent", type=int, default=None,
                        help="How many torrents may download simultaneously (default: 3).")
    parser.add_argument("--min-seeders", type=int, default=None,
                        help="Minimum seeders a torrent must have (default: 5).")
    parser.add_argument("--no-monitor", action="store_true",
                        help="Skip the download dashboard monitor after queueing.")
    parser.add_argument("--no-title-match", action="store_true",
                        help="Disable show-name verification of search results.")
    parser.add_argument("--qbt-host", type=str, default=QBT_HOST, help="qBittorrent host.")
    parser.add_argument("--qbt-port", type=int, default=QBT_PORT, help="qBittorrent WebUI port.")
    parser.add_argument("--qbt-user", type=str, default=QBT_USER, help="qBittorrent WebUI username.")
    parser.add_argument("--qbt-pass", type=str, default=QBT_PASS, help="qBittorrent WebUI password.")
    args = parser.parse_args()

    # Apply runtime overrides to module-level settings used by other functions
    if args.domains:
        TPB_DOMAINS = [d.strip() for d in args.domains.split(',') if d.strip()]
    if args.download_dir:
        DOWNLOAD_DIR = args.download_dir
    if args.max_concurrent is not None:
        MAX_CONCURRENT = max(1, args.max_concurrent)
    if args.min_seeders is not None:
        MIN_SEEDERS = args.min_seeders
    if args.no_title_match:
        TITLE_MATCH = False
    QBT_HOST, QBT_PORT, QBT_USER, QBT_PASS = args.qbt_host, args.qbt_port, args.qbt_user, args.qbt_pass

    if args.verify_domains:
        verify_domains(TPB_DOMAINS)
        return

    api_key = args.api_key or TMDB_API_KEY

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

    qbt = None
    if not args.dry_run:
        if not HAS_QBT:
            print_error("qbittorrent-api is not installed! Run 'pip install qbittorrent-api' or run with --dry-run.")
            sys.exit(1)
        try:
            qbt = get_qbt_client()
            print_success(f"Connected to qBittorrent WebUI at {QBT_HOST}:{QBT_PORT}. Version: {qbt.app.version}")
        except Exception as e:
            print_error(f"Failed to connect to qBittorrent WebUI at {QBT_HOST}:{QBT_PORT}: {e}")
            print_warning("Ensure qBittorrent is running locally with WebUI enabled at that address.")
            sys.exit(1)

    state = load_state()
    category = args.category or "series-getter"
    tag = args.tag or show_name.lower().replace(" ", "-")

    discovery = None
    selection = None

    if api_key:
        discovery = discover_series(show_name, api_key)
    else:
        print_warning("No TMDB API key set. Skipping series discovery.")
        print_info("Set TMDB_API_KEY in config or use --api-key to enable discovery.")

    use_picker_flow = discovery is not None and not args.seasons

    if use_picker_flow:
        selection = interactive_episode_picker(discovery)

        while True:
            plan = []
            for s_num in sorted(selection.keys()):
                eps = sorted(selection[s_num])
                if not eps:
                    continue
                season_info = next((s for s in discovery["info"]["seasons"] if s["number"] == s_num), None)
                plan.append({
                    "type": "pending",
                    "season": s_num,
                    "episodes": eps,
                    "season_episode_count": season_info["episode_count"] if season_info else 0,
                    "title": None,
                    "size_str": None,
                    "seeds": 0,
                    "magnet": None,
                })

            if not plan:
                print_error("No episodes in plan!")
                continue

            populate_plan_from_tp(plan, show_name, args.quality)

            # Filter out entries with no resolved torrents
            plan = [item for item in plan if item.get("title") or item.get("episode_torrents")]

            if not plan:
                print_error("No torrents found for any selected episodes!")
                retry = input("  Go back to picker? (y/n): ").strip().lower()
                if retry == 'y':
                    selection = interactive_episode_picker(discovery)
                    continue
                else:
                    sys.exit(0)

            result = review_loop(plan, show_name, discovery)
            if result is True:
                break
            elif result == "add_more":
                selection = interactive_episode_picker(discovery)
                continue
            else:
                sys.exit(0)

        # Execute downloads
        torrents_queued_count = 0
        for item in plan:
            s_num = item["season"]

            if item["type"] == "season_pack" and item.get("magnet"):
                if not args.force and is_season_pack_queued(state, show_name, s_num):
                    print_info(f"Season {s_num:02d} already queued. Skipping.")
                    continue
                if args.dry_run:
                    print_info(f"[DRY-RUN] Would queue pack: {item['title']}")
                else:
                    save_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, show_name, f"Season {s_num:02d}"))
                    os.makedirs(save_path, exist_ok=True)
                    if add_torrent_to_qbt(qbt, item["magnet"], save_path, category, tag):
                        mark_season_pack_queued(state, show_name, s_num)
                        for ep in item.get("episodes", []):
                            mark_episode_queued(state, show_name, s_num, ep)
                        torrents_queued_count += 1

            elif item["type"] == "episodes":
                for ep, torrent in item.get("episode_torrents", {}).items():
                    if ep in skip_episodes:
                        print_info(f"S{s_num:02d}E{ep:02d} skipped.")
                        continue
                    if not args.force and is_episode_queued(state, show_name, s_num, ep):
                        print_info(f"S{s_num:02d}E{ep:02d} already queued. Skipping.")
                        continue
                    if args.dry_run:
                        print_info(f"[DRY-RUN] Would queue: {torrent.title}")
                    else:
                        save_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, show_name, f"Season {s_num:02d}"))
                        os.makedirs(save_path, exist_ok=True)
                        if add_torrent_to_qbt(qbt, torrent.magnet, save_path, category, tag):
                            mark_episode_queued(state, show_name, s_num, ep)
                            ep_season, ep_start, ep_end = parse_torrent_info(torrent.title)
                            if ep_end and ep_end > ep_start:
                                for extra_ep in range(ep_start + 1, ep_end + 1):
                                    mark_episode_queued(state, show_name, s_num, extra_ep)
                            torrents_queued_count += 1

        if not args.dry_run and not args.no_monitor and torrents_queued_count > 0 and qbt is not None:
            try:
                monitor_downloads(qbt, category, tag)
            except Exception as e:
                print_error(f"Error in monitor: {e}")

        if args.dry_run:
            print_header("Dry-Run Summary")
            for item in plan:
                if item["type"] == "season_pack":
                    print_info(f"[DRY-RUN] Season Pack S{item['season']:02d}: {item['title']}")
                elif item["type"] == "episodes":
                    for ep, t in sorted(item.get("episode_torrents", {}).items()):
                        print_info(f"[DRY-RUN] S{item['season']:02d}E{ep:02d}: {t.title}")

    else:
        # Legacy fallback: no TMDB or explicit --seasons
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
            seasons = list(range(1, 100))

        dry_run_list = []
        torrents_queued_count = 0

        try:
            for s in seasons:
                print_header(f"Processing Season {s:02d}")

                if not args.force and is_season_pack_queued(state, show_name, s):
                    print_info(f"Season {s:02d} pack already queued in state. Skipping.")
                    continue

                print_info(f"Searching for Season {s:02d} pack torrents...")
                best_pack = try_find_season_pack(show_name, s, args.quality, args.interactive)

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
                    else:
                        save_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, show_name, f"Season {s:02d}"))
                        os.makedirs(save_path, exist_ok=True)
                        if add_torrent_to_qbt(qbt, best_pack.magnet, save_path, category, tag):
                            mark_season_pack_queued(state, show_name, s)
                            for ep in range(1, 100):
                                mark_episode_queued(state, show_name, s, ep)
                            torrents_queued_count += 1
                    continue

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
                    ep_results = cached_search(ep_query, show_name)

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
                        else:
                            save_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, show_name, f"Season {s:02d}"))
                            os.makedirs(save_path, exist_ok=True)
                            if add_torrent_to_qbt(qbt, selected_ep.magnet, save_path, category, tag):
                                mark_episode_queued(state, show_name, s, ep)
                                ep_season, ep_start, ep_end = parse_torrent_info(selected_ep.title)
                                if ep_end and ep_end > ep_start:
                                    for extra_ep in range(ep_start + 1, ep_end + 1):
                                        mark_episode_queued(state, show_name, s, extra_ep)
                                    ep = ep_end
                                torrents_queued_count += 1
                    else:
                        print_warning(f"No valid/seeding torrent matches found for S{s:02d}E{ep:02d}.")

                    ep += 1

                if not args.seasons and ep == 1 + consecutive_failures:
                    print_info(f"No files or packs found at all for Season {s:02d}. Ending TV series scan.")
                    break

        except KeyboardInterrupt:
            print_warning("\nProcess interrupted by user gracefully. Saving progress state.")

        if not args.dry_run and not args.no_monitor and torrents_queued_count > 0 and qbt is not None:
            try:
                monitor_downloads(qbt, category, tag)
            except Exception as e:
                print_error(f"Error in background monitor process: {e}")

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

def main():
    try:
        _main()
    except KeyboardInterrupt:
        print_warning("\nInterrupted by user. Progress has been saved to state.")
        try:
            sys.exit(130)
        except SystemExit:
            pass
    except Exception as e:
        logging.exception("Unhandled error in main")
        print_error(f"Unexpected error: {e}")
        raise

if __name__ == "__main__":
    main()
