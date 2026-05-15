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
import json
import time
import logging
from pathlib import Path

import requests
import yaml

# ══════════════════════════════════════════════════════════════════════════════
# Config loading — YAML first, environment variables override
# ══════════════════════════════════════════════════════════════════════════════

CONFIG_FILE = os.getenv("CONFIG_FILE", "/config/config.yaml")


def load_config() -> dict:
    """
    Load configuration from config.yaml, then apply any environment variable
    overrides on top. Environment variables always win.
    """
    config = {}
    path = Path(CONFIG_FILE)
    if path.exists():
        with open(path) as f:
            config = yaml.safe_load(f) or {}
    else:
        logging.warning(f"Config file not found at {CONFIG_FILE} — using environment variables only")
    return config


def cfg(config: dict, key: str, env_var: str, default=None):
    """
    Resolve a config value. Priority: env var > config.yaml > default.
    """
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


# ══════════════════════════════════════════════════════════════════════════════
# Bootstrap — load config before anything else
# ══════════════════════════════════════════════════════════════════════════════

_config = load_config()

NAVIDROME_URL               = cfg      (_config, "navidrome_url",               "NAVIDROME_URL",               "http://navidrome:4533")
NAVIDROME_USER              = cfg      (_config, "navidrome_user",              "NAVIDROME_USER",              "admin")
NAVIDROME_PASS              = cfg      (_config, "navidrome_pass",              "NAVIDROME_PASS",              "")
NAVIDROME_FLOWS_LIBRARY_ID  = cfg      (_config, "navidrome_flows_library_id",  "NAVIDROME_FLOWS_LIBRARY_ID",  "")

LIDARR_URL                  = cfg      (_config, "lidarr_url",                  "LIDARR_URL",                  "http://lidarr:8686")
LIDARR_API_KEY              = cfg      (_config, "lidarr_api_key",              "LIDARR_API_KEY",              "")
LIDARR_ROOT_FOLDER          = cfg      (_config, "lidarr_root_folder",          "LIDARR_ROOT_FOLDER",          "/music/library")
LIDARR_QUALITY_PROFILE_ID   = cfg_int  (_config, "lidarr_quality_profile_id",   "LIDARR_QUALITY_PROFILE_ID",   1)
LIDARR_METADATA_PROFILE_ID  = cfg_int  (_config, "lidarr_metadata_profile_id",  "LIDARR_METADATA_PROFILE_ID",  1)

DOWNLOADS_PATH              = cfg      (_config, "downloads_path",              "DOWNLOADS_PATH",              "/downloads")
STATE_FILE                  = cfg      (_config, "state_file",                  "STATE_FILE",                  "/data/state.json")
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
# State management
# ══════════════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    path = Path(STATE_FILE)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception as exc:
            log.warning(f"Could not read state file ({exc}) — starting fresh")
    return {"processed_ids": []}


def save_state(state: dict) -> None:
    path = Path(STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


# ══════════════════════════════════════════════════════════════════════════════
# Navidrome  (Subsonic-compatible API)
# ══════════════════════════════════════════════════════════════════════════════

def _nd_params(**extra) -> dict:
    return {"u": NAVIDROME_USER, "p": NAVIDROME_PASS,
            "v": "1.16.0", "c": "star-sync", "f": "json", **extra}


def get_starred_songs() -> list:
    """Return every song the user has starred in Navidrome, optionally filtered by library."""
    params = _nd_params()
    if NAVIDROME_FLOWS_LIBRARY_ID:
        params["musicFolderId"] = NAVIDROME_FLOWS_LIBRARY_ID
    resp = requests.get(
        f"{NAVIDROME_URL}/rest/getStarred2.view",
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json().get("subsonic-response", {})
    if body.get("status") != "ok":
        raise RuntimeError(f"Navidrome API error: {body.get('error', body)}")
    return body.get("starred2", {}).get("song", [])


# ══════════════════════════════════════════════════════════════════════════════
# MusicBrainz
# ══════════════════════════════════════════════════════════════════════════════

_MB_HEADERS = {"User-Agent": "navidrome-star-to-lidarr/1.0 (self-hosted)"}


def mb_find_artist_mbid(artist_name: str) -> str | None:
    time.sleep(MB_RATE_LIMIT)
    resp = requests.get(
        "https://musicbrainz.org/ws/2/artist/",
        params={"query": f'artist:"{artist_name}"', "fmt": "json", "limit": 5},
        headers=_MB_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    artists = resp.json().get("artists", [])
    if not artists:
        log.warning(f"  MusicBrainz: no artist found for '{artist_name}'")
        return None
    for a in artists:
        if a.get("name", "").lower() == artist_name.lower():
            return a["id"]
    return artists[0]["id"]


# ══════════════════════════════════════════════════════════════════════════════
# Lidarr API
# ══════════════════════════════════════════════════════════════════════════════

def _lidarr_headers() -> dict:
    return {"X-Api-Key": LIDARR_API_KEY}


def lidarr_find_artist(mbid: str) -> dict | None:
    resp = requests.get(f"{LIDARR_URL}/api/v1/artist", headers=_lidarr_headers(), timeout=15)
    resp.raise_for_status()
    for artist in resp.json():
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
    resp = requests.post(f"{LIDARR_URL}/api/v1/artist", headers=_lidarr_headers(), json=payload, timeout=15)
    if resp.status_code == 400:
        log.info(f"  Artist already in Lidarr: {artist_name}")
        return None
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
    resp = requests.get(
        f"{LIDARR_URL}/api/v1/album",
        headers=_lidarr_headers(),
        params={"artistId": artist_id},
        timeout=15,
    )
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
    album_name_lower = album_name.lower().strip()
    for album in albums:
        if album.get("title", "").lower().strip() == album_name_lower:
            return album
    for album in albums:
        if album_name_lower in album.get("title", "").lower():
            return album
    if albums:
        log.debug(f"  No album name match for '{album_name}' — using first album")
        return albums[0]
    return None


def lidarr_monitor_album(album_id: int, album_title: str) -> bool:
    if DRY_RUN:
        log.info(f"  [DRY RUN] Would monitor album: {album_title} (id={album_id})")
        return True
    resp = requests.put(
        f"{LIDARR_URL}/api/v1/album/monitor",
        headers=_lidarr_headers(),
        json={"albumIds": [album_id], "monitored": True},
        timeout=15,
    )
    resp.raise_for_status()
    log.info(f"  Monitoring album: {album_title} (id={album_id})")
    return True


def lidarr_search_album(album_id: int) -> bool:
    if DRY_RUN:
        log.info(f"  [DRY RUN] Would trigger search for album id={album_id}")
        return True
    resp = requests.post(
        f"{LIDARR_URL}/api/v1/command",
        headers=_lidarr_headers(),
        json={"name": "AlbumSearch", "albumIds": [album_id]},
        timeout=15,
    )
    resp.raise_for_status()
    log.info(f"  Triggered search for album id={album_id}")
    return True


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

def process_song(song: dict) -> bool:
    artist_name = song.get("artist", "").strip()
    title       = song.get("title",  "").strip()
    album_name  = song.get("album",  "").strip()

    log.info(f"── Processing: {artist_name} — {title} (album: {album_name})")

    # ── Step 1: determine library source ────────────────────────────────────
    file_path   = find_file_in_downloads(song)
    from_aurral = file_path is not None

    if from_aurral:
        log.info(f"  Source: Aurral flows library  ({file_path})")
    else:
        if PROCESS_MAIN_LIBRARY_STARS:
            log.info(f"  Source: main library  (will ensure artist is in Lidarr)")
        else:
            log.info(f"  Source: main library — skipping "
                     f"(set process_main_library_stars: true in config to process)")
            return True

    # ── Step 2: look up artist in MusicBrainz ───────────────────────────────
    mbid = mb_find_artist_mbid(artist_name)
    if not mbid:
        log.warning(f"  Cannot find '{artist_name}' in MusicBrainz — skipping.")
        return False

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
                return False

    if not from_aurral:
        log.info(f"  ✓ Done (main library — artist ensured in Lidarr): {artist_name}")
        return True

    if DRY_RUN:
        log.info(f"  [DRY RUN] Would find album '{album_name}', monitor it, and trigger search")
        return True

    # ── Step 4: find matching album ──────────────────────────────────────────
    artist_id = artist["id"]
    albums    = lidarr_wait_for_albums(artist_id)
    if not albums:
        log.error(f"  No albums found in Lidarr for {artist_name} — aborting.")
        return False

    album = lidarr_find_matching_album(albums, album_name)
    if not album:
        log.error(f"  Could not match album '{album_name}' — aborting.")
        return False

    log.info(f"  Matched album: {album.get('title')} (id={album.get('id')})")

    # ── Step 5: monitor the album ────────────────────────────────────────────
    lidarr_monitor_album(album["id"], album.get("title", ""))

    # ── Step 6: trigger search ───────────────────────────────────────────────
    lidarr_search_album(album["id"])

    log.info(f"  ✓ Done: {artist_name} — {album.get('title')} queued for download")
    return True


# ══════════════════════════════════════════════════════════════════════════════
# Main loop
# ══════════════════════════════════════════════════════════════════════════════

def run_once() -> None:
    state     = load_state()
    processed = set(state.get("processed_ids", []))

    try:
        starred = get_starred_songs()
    except Exception as exc:
        log.error(f"Failed to fetch starred songs from Navidrome: {exc}")
        return

    new_songs = [s for s in starred if s.get("id") not in processed]
    if not new_songs:
        log.debug("No new starred songs.")
        return

    log.info(f"Found {len(new_songs)} new starred song(s) to process.")

    for song in new_songs:
        song_id = song.get("id")
        try:
            success = process_song(song)
        except Exception as exc:
            log.error(f"Unexpected error on '{song.get('title')}': {exc}")
            success = False

        if success:
            processed.add(song_id)
            state["processed_ids"] = list(processed)
            save_state(state)


def main() -> None:
    log.info("═" * 60)
    log.info("navidrome-star-to-lidarr  starting up")
    log.info(f"  Config    : {CONFIG_FILE}")
    log.info(f"  Navidrome : {NAVIDROME_URL}")
    log.info(f"  Lidarr    : {LIDARR_URL}")
    log.info(f"  Downloads : {DOWNLOADS_PATH}")
    log.info(f"  Poll      : every {POLL_INTERVAL}s")
    log.info(f"  Dry run   : {DRY_RUN}")
    log.info("═" * 60)

    while True:
        try:
            run_once()
        except Exception as exc:
            log.error(f"Unhandled error in main loop: {exc}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()