#!/usr/bin/env python3
"""
navidrome-star-to-lidarr (star-sync)
─────────────────────────────────────
Polls Navidrome for newly starred tracks, finds the artist in MusicBrainz,
adds them to Lidarr as unmonitored, then monitors and searches for the
specific album containing the starred track.

Configuration is read from config.yaml (mounted into the container).
Any setting can be overridden by setting the corresponding environment variable.

Run with DRY_RUN=true (or dry_run: true in config.yaml) to preview actions
without touching Lidarr.
"""

import os
import re
import json
import time
import signal
import logging
import threading
from pathlib import Path

import requests
import yaml

try:
    import mutagen
    from mutagen.id3 import ID3
    from mutagen.flac import FLAC
    from mutagen.mp4 import MP4
    MUTAGEN_AVAILABLE = True
except ImportError:
    MUTAGEN_AVAILABLE = False

# ══════════════════════════════════════════════════════════════════════════════
# Config loading — YAML first, environment variables override
# ══════════════════════════════════════════════════════════════════════════════

CONFIG_FILE = os.getenv("CONFIG_FILE", "/config/config.yaml")


def load_config() -> dict:
    """Load configuration from config.yaml. Environment variables override."""
    config = {}
    path = Path(CONFIG_FILE)
    if path.exists():
        with open(path) as f:
            config = yaml.safe_load(f) or {}
    else:
        logging.warning(f"Config file not found at {CONFIG_FILE} — using environment variables only")
    return config


def cfg(config: dict, key: str, env_var: str, default=None):
    """Resolve a config value. Priority: env var > config.yaml > default."""
    if env_var in os.environ:
        return os.environ[env_var]
    return config.get(key, default)


def cfg_int(config: dict, key: str, env_var: str, default: int) -> int:
    return int(cfg(config, key, env_var, default))


def cfg_float(config: dict, key: str, env_var: str, default: float) -> float:
    return float(cfg(config, key, env_var, default))


def cfg_bool(config: dict, key: str, env_var: str, default: bool) -> bool:
    val = cfg(config, key, env_var, default)
    if isinstance(val, bool):
        return val
    return str(val).lower() in ("true", "1", "yes")


def require(name: str, env_var: str, value) -> str:
    """Fail fast if a required config value is missing or empty."""
    if not value:
        raise RuntimeError(
            f"Missing required config: '{name}'. "
            f"Set it in config.yaml or as environment variable {env_var}."
        )
    return str(value)


# ══════════════════════════════════════════════════════════════════════════════
# Bootstrap — load config before anything else
# ══════════════════════════════════════════════════════════════════════════════

_config = load_config()

NAVIDROME_URL               = cfg      (_config, "navidrome_url",               "NAVIDROME_URL",               "http://navidrome:4533")
NAVIDROME_USER              = cfg      (_config, "navidrome_user",              "NAVIDROME_USER",              "")
NAVIDROME_PASS              = cfg      (_config, "navidrome_pass",              "NAVIDROME_PASS",              "")
NAVIDROME_FLOWS_LIBRARY_ID  = cfg      (_config, "navidrome_flows_library_id",  "NAVIDROME_FLOWS_LIBRARY_ID",  "")

LIDARR_URL                  = cfg      (_config, "lidarr_url",                  "LIDARR_URL",                  "http://lidarr:8686")
LIDARR_API_KEY              = cfg      (_config, "lidarr_api_key",              "LIDARR_API_KEY",              "")
LIDARR_ROOT_FOLDER          = cfg      (_config, "lidarr_root_folder",          "LIDARR_ROOT_FOLDER",          "")
LIDARR_QUALITY_PROFILE_ID   = cfg_int  (_config, "lidarr_quality_profile_id",   "LIDARR_QUALITY_PROFILE_ID",   1)
LIDARR_METADATA_PROFILE_ID  = cfg_int  (_config, "lidarr_metadata_profile_id",  "LIDARR_METADATA_PROFILE_ID",  1)

DOWNLOADS_PATH              = cfg      (_config, "downloads_path",              "DOWNLOADS_PATH",              "/downloads")
STATE_FILE                  = cfg      (_config, "state_file",                  "STATE_FILE",                  "/data/state.json")
PENDING_FILE                = cfg      (_config, "pending_file",                "PENDING_FILE",                "/data/pending.yaml")
PENDING_MAX_RETRIES         = cfg_int  (_config, "pending_max_retries",         "PENDING_MAX_RETRIES",         5)
RESCUE_PATH                 = cfg      (_config, "rescue_path",                  "RESCUE_PATH",                 "/rescued")
POLL_INTERVAL               = cfg_int  (_config, "poll_interval",               "POLL_INTERVAL",               300)
MB_RATE_LIMIT               = cfg_float(_config, "mb_rate_limit",               "MB_RATE_LIMIT",               1.2)
ARTIST_WAIT_TIMEOUT         = cfg_int  (_config, "artist_wait_timeout",         "ARTIST_WAIT_TIMEOUT",         120)
ALBUM_WAIT_TIMEOUT          = cfg_int  (_config, "album_wait_timeout",          "ALBUM_WAIT_TIMEOUT",          120)
PROCESS_MAIN_LIBRARY_STARS  = cfg_bool (_config, "process_main_library_stars",  "PROCESS_MAIN_LIBRARY_STARS",  False)
DRY_RUN                     = cfg_bool (_config, "dry_run",                     "DRY_RUN",                     False)
LOG_LEVEL                   = cfg      (_config, "log_level",                   "LOG_LEVEL",                   "INFO").upper()

AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".m4a", ".opus", ".aac", ".wav"}

# ══════════════════════════════════════════════════════════════════════════════
# Logging
# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("star-sync")


# ══════════════════════════════════════════════════════════════════════════════
# Validate required config — fail fast before the main loop starts
# ══════════════════════════════════════════════════════════════════════════════

def validate_config() -> None:
    """Raise RuntimeError immediately if any required value is missing."""
    require("navidrome_user",     "NAVIDROME_USER",     NAVIDROME_USER)
    require("navidrome_pass",     "NAVIDROME_PASS",     NAVIDROME_PASS)
    require("lidarr_api_key",     "LIDARR_API_KEY",     LIDARR_API_KEY)
    require("lidarr_root_folder", "LIDARR_ROOT_FOLDER", LIDARR_ROOT_FOLDER)


# ══════════════════════════════════════════════════════════════════════════════
# HTTP helpers — separate sessions per service + retry/backoff
# ══════════════════════════════════════════════════════════════════════════════

_navidrome_session  = requests.Session()
_lidarr_session     = requests.Session()
_mb_session         = requests.Session()
_mb_session.headers.update({"User-Agent": "navidrome-star-to-lidarr/1.0 (self-hosted)"})


def _request_with_retry(session: requests.Session, method: str, url: str,
                         retries: int = 3, backoff: float = 2.0, **kwargs) -> requests.Response:
    """
    Make an HTTP request with automatic retry and exponential backoff.
    Retries on 429 (rate limit), 500, 502, 503, 504.
    On final failure, includes the response body in the error for easier debugging.
    """
    RETRYABLE = {429, 500, 502, 503, 504}
    last_exc  = None
    last_resp = None

    for attempt in range(1, retries + 1):
        try:
            resp = session.request(method, url, **kwargs)
            if resp.status_code in RETRYABLE:
                wait = backoff ** attempt
                log.warning(f"  HTTP {resp.status_code} from {url} — retrying in {wait:.0f}s "
                            f"(attempt {attempt}/{retries})")
                last_resp = resp
                time.sleep(wait)
                continue
            return resp
        except (requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout) as exc:
            last_exc  = exc
            last_resp = None
            wait = backoff ** attempt
            log.warning(f"  {type(exc).__name__} — retrying in {wait:.0f}s "
                        f"(attempt {attempt}/{retries}): {exc}")
            time.sleep(wait)

    # All retries exhausted — include response body if available
    body_hint = ""
    if last_resp is not None:
        body_hint = f" — response: {last_resp.text[:500]}"
    raise RuntimeError(
        f"Request to {url} failed after {retries} attempts{body_hint}"
        if not last_exc else
        f"Request to {url} failed after {retries} attempts: {last_exc}"
    )


def _nd_get(path: str, **params) -> requests.Response:
    """GET request to Navidrome Subsonic API."""
    base_params = {"u": NAVIDROME_USER, "p": NAVIDROME_PASS,
                   "v": "1.16.0", "c": "star-sync", "f": "json"}
    return _request_with_retry(
        _navidrome_session, "GET", f"{NAVIDROME_URL}{path}",
        params={**base_params, **params}, timeout=15,
    )


def _lidarr_get(path: str, **params) -> requests.Response:
    return _request_with_retry(
        _lidarr_session, "GET", f"{LIDARR_URL}{path}",
        headers={"X-Api-Key": LIDARR_API_KEY},
        params=params, timeout=15,
    )


def _lidarr_post(path: str, payload: dict) -> requests.Response:
    return _request_with_retry(
        _lidarr_session, "POST", f"{LIDARR_URL}{path}",
        headers={"X-Api-Key": LIDARR_API_KEY},
        json=payload, timeout=15,
    )


def _lidarr_put(path: str, payload: dict) -> requests.Response:
    return _request_with_retry(
        _lidarr_session, "PUT", f"{LIDARR_URL}{path}",
        headers={"X-Api-Key": LIDARR_API_KEY},
        json=payload, timeout=15,
    )


def _mb_get(path: str, **params) -> requests.Response:
    return _request_with_retry(
        _mb_session, "GET", f"https://musicbrainz.org/ws/2{path}",
        params={**params, "fmt": "json"}, timeout=15,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Startup validation — confirm Lidarr is reachable and config is valid
# ══════════════════════════════════════════════════════════════════════════════

def validate_lidarr() -> None:
    """
    Confirm Lidarr is reachable, the API key works, and the configured
    root folder and profile IDs actually exist.
    """
    log.info("  Validating Lidarr connection…")

    resp = _lidarr_get("/api/v1/rootfolder")
    resp.raise_for_status()
    root_folders = [rf["path"] for rf in resp.json()]
    if LIDARR_ROOT_FOLDER not in root_folders:
        raise RuntimeError(
            f"lidarr_root_folder '{LIDARR_ROOT_FOLDER}' not found in Lidarr. "
            f"Available: {root_folders}"
        )

    resp = _lidarr_get("/api/v1/qualityprofile")
    resp.raise_for_status()
    quality_ids = [p["id"] for p in resp.json()]
    if LIDARR_QUALITY_PROFILE_ID not in quality_ids:
        raise RuntimeError(
            f"lidarr_quality_profile_id {LIDARR_QUALITY_PROFILE_ID} not found. "
            f"Available IDs: {quality_ids}"
        )

    resp = _lidarr_get("/api/v1/metadataprofile")
    resp.raise_for_status()
    metadata_ids = [p["id"] for p in resp.json()]
    if LIDARR_METADATA_PROFILE_ID not in metadata_ids:
        raise RuntimeError(
            f"lidarr_metadata_profile_id {LIDARR_METADATA_PROFILE_ID} not found. "
            f"Available IDs: {metadata_ids}"
        )

    log.info("  Lidarr connection OK ✓")


# ══════════════════════════════════════════════════════════════════════════════
# State management
# ══════════════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    path = Path(STATE_FILE)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception as exc:
            log.warning(f"Could not read state file ({exc}) — starting fresh")
    return {"processed_ids": [], "skipped_ids": []}


def save_state(state: dict) -> None:
    """Write state atomically — temp file then rename to avoid corruption."""
    path = Path(STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


# ══════════════════════════════════════════════════════════════════════════════
# Pending interventions file
# ══════════════════════════════════════════════════════════════════════════════

PENDING_FILE_HEADER = """# ─────────────────────────────────────────────────────────────────────────────
# star-sync pending interventions
# ─────────────────────────────────────────────────────────────────────────────
# These are songs that star-sync could not automatically match to a Lidarr
# album. To resolve an item:
#
#   1. Find the MusicBrainz release group for the album:
#      Go to https://musicbrainz.org and search for the artist + album name.
#      Copy the UUID from the URL, e.g.:
#        https://musicbrainz.org/release-group/87f8f3b6-476e-40b0-8f5f-ea2ebc1743a2
#                                              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
#   2. Paste the UUID into mb_release_group_id for that item below
#   3. Save the file — star-sync will pick it up on the next poll
#
# Do not change any other fields.
# Once an item is successfully processed it will be removed from this file.
# ─────────────────────────────────────────────────────────────────────────────

"""


def load_pending() -> list:
    """Load pending intervention items from the pending file."""
    path = Path(PENDING_FILE)
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text())
        if data and isinstance(data.get("items"), list):
            return data["items"]
    except Exception as exc:
        log.warning(f"Could not read pending file ({exc})")
    return []


def save_pending(items: list) -> None:
    """
    Write pending items back to the pending file atomically.
    Creates the file with the instructions header if it doesn't exist yet.
    Writes the header as a comment block, then a clean yaml items list.
    """
    path = Path(PENDING_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")

    with open(tmp, "w", encoding="utf-8") as f:
        # Write the instructions header (no items: key — yaml.dump adds it below)
        f.write(PENDING_FILE_HEADER)
        # yaml.dump({"items": items}) writes the full "items:\n- ..." structure
        yaml_str = yaml.dump(
            {"items": items},
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )
        f.write(yaml_str)

    tmp.replace(path)


def add_to_pending(song: dict, lidarr_artist_id: int, note: str, file_path: Path | None = None) -> None:
    """Add a song to the pending interventions file."""
    items = load_pending()

    song_id = song.get("id", "")

    # Don't add duplicates
    if any(item.get("song_id") == song_id for item in items):
        log.debug(f"  Already in pending file: {song.get('title')}")
        return

    item = {
        "song_id":             song_id,
        "artist":              song.get("artist", ""),
        "album":               song.get("album", ""),
        "title":               song.get("title", ""),   # fallback for find_file_in_downloads
        "path":                song.get("path", ""),    # fallback for find_file_in_downloads
        # Full container path as star-sync sees it — faster and exact.
        # Stored at add time when the file is confirmed to exist.
        "file_path":           str(file_path) if file_path else "",
        "lidarr_artist_id":    lidarr_artist_id,
        "note":                note,
        "retry_count":         0,
        "mb_release_group_id": "",
    }
    items.append(item)
    save_pending(items)
    log.info(f"  Added to pending file: {PENDING_FILE}")
    log.info(f"  Find the MusicBrainz release group at:")
    log.info(f"    https://musicbrainz.org/search?query={song.get('artist', '').replace(' ', '+')}+{song.get('album', '').replace(' ', '+')}&type=release_group")


def rescue_file(item: dict, file_path: Path) -> bool:
    """
    Copy a file to the rescue folder when Lidarr cannot index it.
    Organises it as Artist/Album/Track - Title.ext so Navidrome can pick it up.
    Returns True if the copy succeeded.
    """
    if DRY_RUN:
        log.info(f"  [DRY RUN] Would rescue file to: {RESCUE_PATH}")
        return True

    rescue_root = Path(RESCUE_PATH)
    artist      = item.get("artist", "Unknown Artist").strip()
    album       = item.get("album",  "Unknown Album").strip()

    # Sanitise folder names — remove characters that cause filesystem issues
    def sanitise(name: str) -> str:
        for ch in r'<>:"/\|?*':
            name = name.replace(ch, "_")
        return name.strip(". ")

    dest_dir = rescue_root / sanitise(artist) / sanitise(album)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_file = dest_dir / file_path.name

    if dest_file.exists():
        log.info(f"  Rescue file already exists: {dest_file}")
        return True

    try:
        import shutil
        shutil.copy2(file_path, dest_file)
        log.info(f"  ✓ File rescued to: {dest_file}")
        return True
    except Exception as exc:
        log.error(f"  Failed to rescue file: {exc}", exc_info=True)
        return False


def navidrome_trigger_scan(library_name: str = "Rescued Library") -> None:
    """Trigger a Navidrome library scan via the Subsonic API."""
    if DRY_RUN:
        log.info(f"  [DRY RUN] Would trigger Navidrome scan")
        return
    try:
        resp = _nd_get("/rest/startScan.view")
        resp.raise_for_status()
        log.info(f"  Triggered Navidrome scan")
    except Exception as exc:
        log.warning(f"  Could not trigger Navidrome scan: {exc}")


def process_pending_items() -> None:
    """
    Check the pending file for items that have been filled in with a
    mb_release_group_id and process them.
    """
    items = load_pending()
    if not items:
        return

    ready = [i for i in items if i.get("mb_release_group_id", "").strip()]
    if not ready:
        log.debug(f"  Pending file has {len(items)} item(s) awaiting manual intervention")
        return

    log.info(f"  Found {len(ready)} pending item(s) with MusicBrainz IDs to process")
    remaining = [i for i in items if not i.get("mb_release_group_id", "").strip()]

    for item in ready:
        artist_id    = item.get("lidarr_artist_id")
        rg_id        = item.get("mb_release_group_id", "").strip()
        artist_name  = item.get("artist", "")
        album_name   = item.get("album", "")
        retry_count  = item.get("retry_count", 0)

        log.info(f"  Processing pending: {artist_name} — {album_name} "
                 f"(rg={rg_id}, attempt {retry_count + 1}/{PENDING_MAX_RETRIES})")

        # If retry limit reached, rescue the file and stop retrying
        if retry_count >= PENDING_MAX_RETRIES and item.get("status") != "rescued":
            log.warning(f"  Retry limit ({PENDING_MAX_RETRIES}) reached for: {artist_name} — {album_name}")
            log.warning(f"  Lidarr cannot index this release automatically.")
            log.warning(f"  Most likely cause: incomplete MusicBrainz metadata for release group {rg_id}")
            log.warning(f"  Attempting to rescue file to: {RESCUE_PATH}")

            # Use the stored container path first (fast, exact).
            # Fall back to search if the path wasn't stored or file moved.
            stored_path = item.get("file_path", "").strip()
            if stored_path and Path(stored_path).exists():
                file_path = Path(stored_path)
                log.debug(f"  Using stored file path: {file_path}")
            else:
                log.debug(f"  Stored path missing or gone — searching downloads folder")
                file_path = find_file_in_downloads(item)
            if file_path:
                rescued = rescue_file(item, file_path)
                if rescued:
                    item["status"] = "rescued"
                    item["rescued_to"] = str(Path(RESCUE_PATH) / item.get("artist", "") / item.get("album", ""))
                    navidrome_trigger_scan()
                    log.info(f"  File rescued successfully. Add the rescue folder as a Navidrome library")
                    log.info(f"  if you haven't already: {RESCUE_PATH}")
                    log.warning(f"  To fix properly: https://musicbrainz.org/release-group/{rg_id}")
                else:
                    log.error(f"  Rescue failed — file may already be gone from Aurral flows.")
                    log.warning(f"  To fix: https://musicbrainz.org/release-group/{rg_id}")
            else:
                log.error(f"  File not found in downloads — Aurral may have already rotated it out.")
                log.warning(f"  To fix: https://musicbrainz.org/release-group/{rg_id}")
                item["status"] = "file_gone"

            remaining.append(item)
            continue

        # Already rescued — just keep in pending as a record, no more retrying
        if item.get("status") == "rescued":
            log.debug(f"  Already rescued: {artist_name} — {album_name} → {item.get('rescued_to', RESCUE_PATH)}")
            remaining.append(item)
            continue

        # Search Lidarr albums for this artist and find the one matching the
        # MusicBrainz release group ID
        try:
            log.info(f"  Refreshing Lidarr metadata for artist id={artist_id}…")
            lidarr_refresh_artist(artist_id)
            log.info(f"  Waiting 15s for Lidarr to refresh artist metadata…")
            time.sleep(15)

            albums = lidarr_get_albums(artist_id)
            album  = next(
                (a for a in albums if a.get("foreignAlbumId", "") == rg_id),
                None
            )

            if not album:
                item["retry_count"] = retry_count + 1
                if item["retry_count"] >= PENDING_MAX_RETRIES:
                    log.warning(f"  Release group {rg_id} still not found after {item['retry_count']} attempts.")
                    log.warning(f"  Retry limit will be reached — no more refresh cycles after next poll.")
                    log.warning(f"  Check MusicBrainz data at: https://musicbrainz.org/release-group/{rg_id}")
                else:
                    log.warning(f"  Release group {rg_id} not found after refresh "
                                f"(attempt {item['retry_count']}/{PENDING_MAX_RETRIES}) — will retry next poll")
                remaining.append(item)
                continue

            log.info(f"  Matched via MusicBrainz release group: {album.get('title')} (id={album['id']})")
            lidarr_monitor_album(album["id"], album.get("title", ""))
            lidarr_search_album(album["id"])
            log.info(f"  ✓ Pending resolved: {artist_name} — {album.get('title')} queued for download")

        except Exception as exc:
            log.error(f"  Error processing pending item: {exc}", exc_info=True)
            remaining.append(item)

    save_pending(remaining)


# ══════════════════════════════════════════════════════════════════════════════
# Navidrome  (Subsonic-compatible API)
# ══════════════════════════════════════════════════════════════════════════════

def get_starred_songs() -> list:
    """Return every song the user has starred in Navidrome, optionally filtered by library."""
    params = {}
    if NAVIDROME_FLOWS_LIBRARY_ID:
        params["musicFolderId"] = NAVIDROME_FLOWS_LIBRARY_ID
    resp = _nd_get("/rest/getStarred2.view", **params)
    resp.raise_for_status()
    body = resp.json().get("subsonic-response", {})
    if body.get("status") != "ok":
        raise RuntimeError(f"Navidrome API error: {body.get('error', body)}")
    return body.get("starred2", {}).get("song", [])


# ══════════════════════════════════════════════════════════════════════════════
# MusicBrainz
# ══════════════════════════════════════════════════════════════════════════════

# Standard MusicBrainz UUID format: 8-4-4-4-12 hex characters
_MBID_PATTERN = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
    re.IGNORECASE
)


def extract_first_valid_mbid(raw) -> str | None:
    """Extract first valid MusicBrainz UUID from a string.
    Handles concatenated IDs by finding the first UUID-shaped substring.
    """
    if not raw:
        return None
    match = _MBID_PATTERN.search(str(raw))
    return match.group(0) if match else None


# Common multi-artist separators in file tags
_ARTIST_SEPARATORS = re.compile(r'[;/\\]|\s+&\s+|\s+feat\.?\s+|\s+ft\.?\s+|\s+x\s+', re.IGNORECASE)


def _mb_search_artist(artist_name: str) -> str | None:
    """
    Search MusicBrainz for a single artist name.
    Returns MBID of best match or None.
    """
    time.sleep(MB_RATE_LIMIT)
    resp = _mb_get("/artist/", query=f'artist:"{artist_name}"', limit=5)
    resp.raise_for_status()
    artists = resp.json().get("artists", [])

    if not artists:
        return None

    for a in artists:
        if a.get("name", "").lower() == artist_name.lower():
            log.debug(f"  MusicBrainz: exact match '{artist_name}' → {a['id']} (score {a.get('score')})")
            return a["id"]

    best = max(artists, key=lambda a: int(a.get("score", 0)))
    log.debug(f"  MusicBrainz: best match for '{artist_name}' → "
              f"'{best.get('name')}' {best['id']} (score {best.get('score')})")
    return best["id"]


def mb_find_artist_from_recording(recording_id: str) -> str | None:
    """
    Look up a MusicBrainz recording by ID and return the primary artist MBID.
    This is faster and more accurate than text search — no ambiguity.
    """
    time.sleep(MB_RATE_LIMIT)
    resp = _mb_get(f"/recording/{recording_id}", inc="artists")
    resp.raise_for_status()
    data = resp.json()

    artist_credits = data.get("artist-credit", [])
    for credit in artist_credits:
        if isinstance(credit, dict) and "artist" in credit:
            mbid = credit["artist"].get("id")
            name = credit["artist"].get("name", "?")
            if mbid:
                log.info(f"  MusicBrainz: recording {recording_id} → artist '{name}' ({mbid})")
                return mbid

    log.warning(f"  MusicBrainz: no artist found in recording {recording_id}")
    return None


def mb_find_artist_mbid(artist_name: str) -> str | None:
    """
    Search MusicBrainz for an artist by name.
    Handles multi-artist strings (e.g. "Artist A; Artist B", "A & B", "A feat. B")
    by trying the full string first, then falling back to the primary artist only.
    """
    # Try full name first
    mbid = _mb_search_artist(artist_name)
    if mbid:
        return mbid

    # Try splitting on common multi-artist separators and use the first part
    parts = [p.strip() for p in _ARTIST_SEPARATORS.split(artist_name) if p.strip()]
    if len(parts) > 1:
        primary = parts[0]
        log.info(f"  MusicBrainz: no result for '{artist_name}' — trying primary artist '{primary}'")
        mbid = _mb_search_artist(primary)
        if mbid:
            return mbid

    log.warning(f"  MusicBrainz: no artist found for '{artist_name}' (tried {len(parts)} variant(s))")
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Lidarr API
# ══════════════════════════════════════════════════════════════════════════════

# Cache of all Lidarr artists, refreshed once per poll cycle via
# prime_artist_cache(). Avoids fetching the full list for every song.
_artist_cache: list = []


def prime_artist_cache() -> None:
    """Fetch all Lidarr artists once at the start of each poll cycle."""
    global _artist_cache
    try:
        resp = _lidarr_get("/api/v1/artist")
        resp.raise_for_status()
        _artist_cache = resp.json()
        log.debug(f"  Artist cache primed: {len(_artist_cache)} artists")
    except Exception as exc:
        log.warning(f"  Could not prime artist cache: {exc} — will use empty cache")
        _artist_cache = []


def lidarr_find_artist(mbid: str) -> dict | None:
    """Look up an artist by MusicBrainz ID using the poll-scoped cache."""
    for artist in _artist_cache:
        if artist.get("foreignArtistId") == mbid:
            return artist
    return None


def lidarr_add_artist(artist_name: str, mbid: str) -> dict | None:
    payload = {
        "artistName":           artist_name,
        "foreignArtistId":      mbid,
        "rootFolderPath":       LIDARR_ROOT_FOLDER,
        "qualityProfileId":     LIDARR_QUALITY_PROFILE_ID,
        "metadataProfileId":    LIDARR_METADATA_PROFILE_ID,
        "monitored":            False,
        "albumFolder":          True,
        "addOptions": {
            "monitor":                  "none",
            "searchForMissingAlbums":   False,
        },
    }
    if DRY_RUN:
        log.info(f"  [DRY RUN] Would add artist to Lidarr: {artist_name} ({mbid})")
        return None
    resp = _lidarr_post("/api/v1/artist", payload)
    if resp.status_code == 400:
        # A 400 can mean "already exists" OR a validation failure.
        # Only treat it as a harmless duplicate if the body says so.
        body = resp.text.lower()
        if "already" in body or "exist" in body or "duplicate" in body:
            log.info(f"  Artist already in Lidarr: {artist_name}")
            return None
        # Otherwise it's a real validation error — log and raise
        log.error(f"  Lidarr rejected artist add for {artist_name}: {resp.text[:300]}")
        resp.raise_for_status()
    resp.raise_for_status()
    log.info(f"  Added artist to Lidarr: {artist_name} ({mbid})")
    return resp.json()


def lidarr_wait_for_artist(mbid: str) -> dict | None:
    deadline = time.time() + ARTIST_WAIT_TIMEOUT
    while time.time() < deadline:
        artist = lidarr_find_artist(mbid)
        if artist and artist.get("id"):
            return artist
        log.debug(f"  Waiting for Lidarr to index artist {mbid}…")
        time.sleep(5)
    log.warning(f"  Timed out waiting for Lidarr to index artist {mbid}")
    return None


def lidarr_get_albums(artist_id: int) -> list:
    resp = _lidarr_get("/api/v1/album", artistId=artist_id)
    resp.raise_for_status()
    return resp.json()


def lidarr_wait_for_albums(artist_id: int) -> list:
    deadline = time.time() + ALBUM_WAIT_TIMEOUT
    while time.time() < deadline:
        albums = lidarr_get_albums(artist_id)
        if albums:
            log.debug(f"  Lidarr loaded {len(albums)} album(s)")
            return albums
        log.debug(f"  Waiting for Lidarr to load albums…")
        time.sleep(5)
    log.warning(f"  Timed out waiting for Lidarr to load albums")
    return []


def lidarr_find_matching_album(albums: list, album_name: str) -> dict | None:
    """
    Find the best matching album by name.
    Does NOT fall back to a random album — returns None if no confident match
    to avoid accidentally monitoring the wrong album.
    """
    album_name_lower = album_name.lower().strip()

    for album in albums:
        if album.get("title", "").lower().strip() == album_name_lower:
            return album

    for album in albums:
        title = album.get("title", "").lower().strip()
        if album_name_lower in title or title in album_name_lower:
            return album

    log.warning(f"  No confident album match for '{album_name}' — skipping rather than guessing")
    return None


def lidarr_monitor_album(album_id: int, album_title: str) -> bool:
    if DRY_RUN:
        log.info(f"  [DRY RUN] Would monitor album: {album_title} (id={album_id})")
        return True
    resp = _lidarr_put("/api/v1/album/monitor", {"albumIds": [album_id], "monitored": True})
    resp.raise_for_status()
    log.info(f"  Monitoring album: {album_title} (id={album_id})")
    return True


def lidarr_refresh_artist(artist_id: int) -> None:
    """Trigger a metadata refresh for an artist in Lidarr."""
    if DRY_RUN:
        log.debug(f"  [DRY RUN] Would refresh artist metadata for id={artist_id}")
        return
    resp = _lidarr_post("/api/v1/command",
                        {"name": "RefreshArtist", "artistId": artist_id})
    resp.raise_for_status()
    log.debug(f"  Triggered metadata refresh for artist id={artist_id}")


def lidarr_search_album(album_id: int) -> bool:
    if DRY_RUN:
        log.info(f"  [DRY RUN] Would trigger search for album id={album_id}")
        return True
    resp = _lidarr_post("/api/v1/command", {"name": "AlbumSearch", "albumIds": [album_id]})
    resp.raise_for_status()
    log.info(f"  Triggered search for album id={album_id}")
    return True


# ══════════════════════════════════════════════════════════════════════════════
# Tag reading — extract MusicBrainz IDs directly from audio file metadata
# ══════════════════════════════════════════════════════════════════════════════

def read_tags_from_file(file_path: Path) -> dict:
    """
    Read embedded tags from an audio file using mutagen.
    Returns a dict with any of: mbid_artist, mbid_release, artist, album, title.
    Returns empty dict if mutagen is unavailable or tags cannot be read.
    """
    if not MUTAGEN_AVAILABLE:
        return {}

    try:
        suffix = file_path.suffix.lower()

        if suffix == ".flac":
            tags = FLAC(file_path)
            return {
                "mbid_artist":  extract_first_valid_mbid(tags.get("musicbrainz_artistid",  [None])[0]),
                "mbid_release_group": extract_first_valid_mbid(tags.get("musicbrainz_albumid",   [None])[0]),  # Note: MusicBrainz Album Id tags are often release IDs, not release-group IDs. Not currently used for Lidarr matching.
                "artist":       tags.get("artist",                 [None])[0],
                "album":        tags.get("album",                  [None])[0],
                "title":        tags.get("title",                  [None])[0],
            }

        elif suffix in (".mp3",):
            tags = ID3(file_path)
            return {
                "mbid_artist":  extract_first_valid_mbid(str(tags["TXXX:MusicBrainz Artist Id"])) if "TXXX:MusicBrainz Artist Id" in tags else None,
                "mbid_release_group": extract_first_valid_mbid(str(tags["TXXX:MusicBrainz Album Id"]))  if "TXXX:MusicBrainz Album Id"  in tags else None,  # Note: often a release ID, not release-group ID. Not currently used for Lidarr matching.
                "artist":       str(tags["TPE1"]) if "TPE1" in tags else None,
                "album":        str(tags["TALB"]) if "TALB" in tags else None,
                "title":        str(tags["TIT2"]) if "TIT2" in tags else None,
            }

        elif suffix in (".m4a", ".aac"):
            tags = MP4(file_path)
            return {
                "mbid_artist":  extract_first_valid_mbid(tags.get("----:com.apple.iTunes:MusicBrainz Artist Id",  [None])[0]),
                "mbid_release_group": extract_first_valid_mbid(tags.get("----:com.apple.iTunes:MusicBrainz Album Id",   [None])[0]),  # Note: often a release ID, not release-group ID. Not currently used for Lidarr matching.
                "artist":       str(tags.get("\xa9ART", [None])[0]) if tags.get("\xa9ART") else None,
                "album":        str(tags.get("\xa9alb", [None])[0]) if tags.get("\xa9alb") else None,
                "title":        str(tags.get("\xa9nam", [None])[0]) if tags.get("\xa9nam") else None,
            }

    except Exception as exc:
        log.debug(f"  Could not read tags from {file_path}: {exc}")

    return {}


# ══════════════════════════════════════════════════════════════════════════════
# File discovery
# ══════════════════════════════════════════════════════════════════════════════

def find_file_in_downloads(song: dict) -> Path | None:
    downloads = Path(DOWNLOADS_PATH)
    song_path = song.get("path", "")
    filename  = Path(song_path).name
    title     = song.get("title", "").lower().strip()

    if filename:
        direct = downloads / filename
        if direct.exists():
            return direct
        for f in downloads.rglob(filename):
            if f.is_file():
                return f

    if title:
        for f in downloads.rglob("*"):
            if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS:
                if title in f.stem.lower():
                    return f

    return None


# ══════════════════════════════════════════════════════════════════════════════
# Per-song processing pipeline
# ══════════════════════════════════════════════════════════════════════════════

def process_song(song: dict) -> tuple[bool, bool]:
    """
    Full pipeline for one starred song.

    Returns (success, skipped) where:
      success=True  — song was handled and should be marked as processed
      skipped=True  — song was intentionally skipped (main library, settings off)
                      and should be tracked separately so it can be reconsidered
                      if settings change later
    """
    artist_name = song.get("artist", "").strip()
    title       = song.get("title",  "").strip()
    album_name  = song.get("album",  "").strip()

    log.info(f"── Processing: {artist_name} — {title} (album: {album_name})")

    # ── Guard: require artist and title ─────────────────────────────────────
    if not artist_name or not title:
        log.warning(f"  Missing artist or title — skipping")
        return False, False

    # ── Step 1: determine library source ────────────────────────────────────
    file_path   = find_file_in_downloads(song)
    from_aurral = file_path is not None

    if from_aurral:
        log.info(f"  Source: Aurral flows library  ({file_path})")
        # Guard: require album name for Aurral tracks since we need it for matching
        if not album_name:
            log.warning(f"  Missing album name — cannot safely match album in Lidarr. Skipping.")
            return False, False
    else:
        if PROCESS_MAIN_LIBRARY_STARS:
            log.info(f"  Source: main library  (will ensure artist is in Lidarr)")
        else:
            log.info(f"  Source: main library — skipping "
                     f"(set process_main_library_stars: true in config to process)")
            # Return skipped=True so this can be reconsidered if the setting changes
            return False, True

    # ── Step 2: get MusicBrainz artist ID ──────────────────────────────────
    # Priority:
    #   1. Recording ID from Navidrome → MusicBrainz recording lookup (fastest, most accurate)
    #   2. Artist MBID from file tags → use directly (mutagen)
    #   3. albumArtists[0] text search → avoids multi-artist string splitting
    #   4. artist field text search → last resort with multi-artist fallback

    mbid = None
    recording_id = song.get("musicBrainzId", "").strip()

    if recording_id:
        log.info(f"  Using MusicBrainz recording ID from Navidrome: {recording_id}")
        mbid = mb_find_artist_from_recording(recording_id)

    if not mbid and file_path:
        file_tags = read_tags_from_file(file_path)
        mbid = file_tags.get("mbid_artist")
        if mbid:
            log.info(f"  MusicBrainz artist ID from file tags: {mbid}")

    if not mbid:
        # Prefer albumArtists over artists — albumArtists is who owns the album
        # e.g. "Yeah! feat. Lil Jon" → albumArtist=Usher, not Lil Jon
        album_artists = song.get("albumArtists", [])
        search_name = album_artists[0].get("name", "").strip() if album_artists else ""
        if not search_name:
            search_name = artist_name
        if search_name != artist_name:
            log.info(f"  Using album artist '{search_name}' instead of track artist '{artist_name}'")
        mbid = mb_find_artist_mbid(search_name)

    if not mbid:
        log.warning(f"  Cannot find '{artist_name}' in MusicBrainz — skipping.")
        return False, False

    # ── Step 3: add artist to Lidarr if not already present ─────────────────
    artist = lidarr_find_artist(mbid)
    if artist:
        log.info(f"  Artist already in Lidarr (id={artist['id']})")
    else:
        lidarr_add_artist(artist_name, mbid)
        if not DRY_RUN:
            artist = lidarr_wait_for_artist(mbid)
            if not artist:
                log.error(f"  Artist never appeared in Lidarr — aborting.")
                return False, False

    if not from_aurral:
        log.info(f"  ✓ Done (main library — artist ensured in Lidarr): {artist_name}")
        return True, False

    if DRY_RUN:
        log.info(f"  [DRY RUN] Would find album '{album_name}', monitor it, and trigger search")
        return True, False

    # ── Step 4: find matching album ──────────────────────────────────────────
    artist_id = artist["id"]
    albums    = lidarr_wait_for_albums(artist_id)
    if not albums:
        log.error(f"  No albums found in Lidarr for {artist_name} — aborting.")
        return False, False

    album = lidarr_find_matching_album(albums, album_name)
    if not album:
        log.warning(f"  Could not match album '{album_name}' for {artist_name}.")
        add_to_pending(song, artist["id"],
                       f"Album name '{album_name}' not matched in Lidarr — may be a single or different title",
                       file_path=file_path)
        # Return True so this song is marked as processed and won't be retried
        # via normal processing — the pending file is now managing it
        return True, False

    log.info(f"  Matched album: {album.get('title')} (id={album.get('id')})")

    # ── Step 5: monitor the album ────────────────────────────────────────────
    # Small delay to give Lidarr time to fully settle album metadata
    # before accepting monitor updates
    log.debug(f"  Waiting 10s for Lidarr to settle before monitoring...")
    time.sleep(10)
    lidarr_monitor_album(album["id"], album.get("title", ""))

    # Verify the monitor call actually stuck
    albums_check  = lidarr_get_albums(artist_id)
    matched_check = next((a for a in albums_check if a["id"] == album["id"]), None)
    if matched_check and not matched_check.get("monitored"):
        log.warning(f"  Album does not appear monitored after update — retrying once...")
        time.sleep(5)
        lidarr_monitor_album(album["id"], album.get("title", ""))

    # ── Step 6: trigger search ───────────────────────────────────────────────
    lidarr_search_album(album["id"])

    log.info(f"  ✓ Done: {artist_name} — {album.get('title')} queued for download")
    return True, False


# ══════════════════════════════════════════════════════════════════════════════
# Main loop
# ══════════════════════════════════════════════════════════════════════════════

def run_once(poll_count: int) -> None:
    log.info(f"─ Poll #{poll_count} {'(dry run) ' if DRY_RUN else ''}─────────────────────────────────────────")

    # Prime the artist cache once for this poll — avoids one API call per song
    if not DRY_RUN:
        prime_artist_cache()

    # Check pending interventions first
    if not DRY_RUN:
        process_pending_items()

    state      = load_state()
    processed  = set(state.get("processed_ids", []))
    skipped    = set(state.get("skipped_ids",   []))

    # Songs to consider: not yet processed, and not skipped
    # (unless process_main_library_stars was just enabled — in that case
    # skipped songs will be reconsidered on the next poll automatically
    # because we only skip them when the setting is off)
    try:
        starred = get_starred_songs()
    except Exception as exc:
        log.error(f"Failed to fetch starred songs from Navidrome: {exc}", exc_info=True)
        log.info(f"─ Poll #{poll_count} complete — next poll in {POLL_INTERVAL}s ─")
        return

    # If process_main_library_stars is now enabled, clear the skipped list
    # so previously-skipped main library songs get reconsidered
    if PROCESS_MAIN_LIBRARY_STARS and skipped:
        log.info(f"  process_main_library_stars is enabled — reconsidering "
                 f"{len(skipped)} previously skipped song(s)")
        skipped = set()
        state["skipped_ids"] = []

    new_songs = [s for s in starred
                 if s.get("id") not in processed
                 and s.get("id") not in skipped]

    if not new_songs:
        log.info(f"  No new starred songs — {len(processed)} processed, {len(skipped)} skipped")
        log.info(f"─ Poll #{poll_count} complete — next poll in {POLL_INTERVAL}s ─")
        return

    log.info(f"  Found {len(new_songs)} new starred song(s) to process")

    for song in new_songs:
        song_id = song.get("id")
        try:
            success, song_skipped = process_song(song)
        except Exception as exc:
            log.error(f"Unexpected error on '{song.get('title')}': {exc}", exc_info=True)
            success, song_skipped = False, False

        if success:
            processed.add(song_id)
            state["processed_ids"] = list(processed)
        elif song_skipped:
            skipped.add(song_id)
            state["skipped_ids"] = list(skipped)

        if success or song_skipped:
            save_state(state)

    log.info(f"─ Poll #{poll_count} complete — next poll in {POLL_INTERVAL}s ─")


def main() -> None:
    stop_event = threading.Event()

    def handle_shutdown(signum, frame):
        log.info("Shutdown signal received — stopping cleanly…")
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    # Validate config and connections before starting the main loop
    try:
        validate_config()
        validate_lidarr()
    except RuntimeError as exc:
        logging.critical(f"Startup validation failed: {exc}")
        raise SystemExit(1)

    log.info("═" * 60)
    log.info("navidrome-star-to-lidarr  starting up")
    log.info(f"  Config    : {CONFIG_FILE}")
    log.info(f"  Navidrome : {NAVIDROME_URL}")
    log.info(f"  Lidarr    : {LIDARR_URL}")
    log.info(f"  Downloads : {DOWNLOADS_PATH}")
    log.info(f"  Poll      : every {POLL_INTERVAL}s")
    log.info(f"  Dry run   : {DRY_RUN}")
    log.info("═" * 60)

    poll_count = 0
    while not stop_event.is_set():
        poll_count += 1
        try:
            run_once(poll_count)
        except Exception as exc:
            log.error(f"Unhandled error in main loop: {exc}", exc_info=True)
        stop_event.wait(timeout=POLL_INTERVAL)

    log.info("star-sync stopped.")


if __name__ == "__main__":
    main()