#!/usr/bin/env python3
"""
navidrome-star-to-lidarr (star-sync)
─────────────────────────────────────
Polls Navidrome for newly starred tracks, finds them in the Aurral downloads
folder, and imports them into Lidarr — with the artist added as unmonitored
so no whole-discography downloads are triggered.

All behaviour is controlled via environment variables (see Configuration block).
Run with DRY_RUN=true to preview actions without touching Lidarr.
"""

import os
import json
import time
import logging
from pathlib import Path

import requests

# ══════════════════════════════════════════════════════════════════════════════
# Configuration  ── every value comes from an environment variable
# ══════════════════════════════════════════════════════════════════════════════

NAVIDROME_URL               = os.getenv("NAVIDROME_URL",                "http://navidrome:4533")
NAVIDROME_USER              = os.getenv("NAVIDROME_USER",               "admin")
NAVIDROME_PASS              = os.getenv("NAVIDROME_PASS",               "")

LIDARR_URL                  = os.getenv("LIDARR_URL",                   "http://lidarr:8686")
LIDARR_API_KEY              = os.getenv("LIDARR_API_KEY",               "")
LIDARR_ROOT_FOLDER          = os.getenv("LIDARR_ROOT_FOLDER",           "/music/library")
LIDARR_QUALITY_PROFILE_ID   = int(os.getenv("LIDARR_QUALITY_PROFILE_ID",  "1"))
LIDARR_METADATA_PROFILE_ID  = int(os.getenv("LIDARR_METADATA_PROFILE_ID", "1"))

# Path to Aurral's downloads folder, mounted into this container
DOWNLOADS_PATH              = os.getenv("DOWNLOADS_PATH",               "/downloads")

# Persistent state file — tracks which Navidrome song IDs we've already handled
STATE_FILE                  = os.getenv("STATE_FILE",                   "/data/state.json")

# How long to sleep between polls (seconds)
POLL_INTERVAL               = int(os.getenv("POLL_INTERVAL",            "300"))

# MusicBrainz requires ≤ 1 req/sec — leave a small buffer
MB_RATE_LIMIT               = float(os.getenv("MB_RATE_LIMIT",          "1.2"))

# How long to wait for Lidarr to finish indexing a newly added artist (seconds)
ARTIST_WAIT_TIMEOUT         = int(os.getenv("ARTIST_WAIT_TIMEOUT",      "90"))

# Set to "true" to log all planned actions without writing anything to Lidarr
DRY_RUN                     = os.getenv("DRY_RUN", "false").lower() == "true"

LOG_LEVEL                   = os.getenv("LOG_LEVEL", "INFO").upper()

AUDIO_EXTENSIONS            = {".mp3", ".flac", ".ogg", ".m4a", ".opus", ".aac", ".wav"}

# If true, also handle stars on songs already in your main library.
# These won't be imported (they're already there) but the artist will be
# added to Lidarr as unmonitored if not already present.
# If false (default), main-library stars are silently skipped.
PROCESS_MAIN_LIBRARY_STARS  = os.getenv("PROCESS_MAIN_LIBRARY_STARS", "false").lower() == "true"

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
    """Return every song the user has starred in Navidrome."""
    resp = requests.get(
        f"{NAVIDROME_URL}/rest/getStarred2.view",
        params=_nd_params(),
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
    """
    Search MusicBrainz for an artist by name.
    Returns the top-ranked MBID, or None if nothing found.
    Respects the MusicBrainz 1 req/sec rate limit.
    """
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

    # Prefer an exact name match if one is present
    for a in artists:
        if a.get("name", "").lower() == artist_name.lower():
            log.debug(f"  MusicBrainz: exact match '{artist_name}' → {a['id']}")
            return a["id"]

    # Fall back to the top-ranked result
    mbid = artists[0]["id"]
    log.debug(f"  MusicBrainz: top result for '{artist_name}' → {mbid}")
    return mbid


# ══════════════════════════════════════════════════════════════════════════════
# Lidarr API
# ══════════════════════════════════════════════════════════════════════════════

def _lidarr_headers() -> dict:
    return {"X-Api-Key": LIDARR_API_KEY}


def lidarr_find_artist(mbid: str) -> dict | None:
    """Return the Lidarr artist object matching a MusicBrainz ID, or None."""
    resp = requests.get(
        f"{LIDARR_URL}/api/v1/artist",
        headers=_lidarr_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    for artist in resp.json():
        if artist.get("foreignArtistId") == mbid:
            return artist
    return None


def lidarr_add_artist(artist_name: str, mbid: str) -> dict | None:
    """
    Add an artist to Lidarr with Monitor = None.
    This means Lidarr knows about the artist but will not search for or
    download any albums automatically.
    Returns the created artist object, or None on dry-run / already-exists.
    """
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

    resp = requests.post(
        f"{LIDARR_URL}/api/v1/artist",
        headers=_lidarr_headers(),
        json=payload,
        timeout=15,
    )

    # Lidarr returns 400 if the artist already exists
    if resp.status_code == 400:
        log.info(f"  Artist already in Lidarr: {artist_name}")
        return None

    resp.raise_for_status()
    log.info(f"  Added artist to Lidarr: {artist_name} ({mbid})")
    return resp.json()


def lidarr_wait_for_artist(mbid: str) -> dict | None:
    """
    Poll Lidarr until it has finished indexing a newly added artist's metadata,
    or until ARTIST_WAIT_TIMEOUT seconds have elapsed.
    """
    deadline = time.time() + ARTIST_WAIT_TIMEOUT
    while time.time() < deadline:
        artist = lidarr_find_artist(mbid)
        if artist and artist.get("id"):
            return artist
        log.debug(f"  Waiting for Lidarr to index artist {mbid}…")
        time.sleep(5)
    log.warning(f"  Timed out waiting for Lidarr to index artist {mbid}")
    return None


def lidarr_get_import_candidates(folder: str, artist_id: int) -> list:
    """
    Ask Lidarr to scan a folder and return potential import candidates.
    Lidarr does the heavy lifting of matching audio files to MusicBrainz releases.
    """
    resp = requests.get(
        f"{LIDARR_URL}/api/v1/manualimport",
        headers=_lidarr_headers(),
        params={
            "folder":               folder,
            "artistId":             artist_id,
            "filterExistingFiles":  "false",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def lidarr_execute_import(candidates: list) -> None:
    """Instruct Lidarr to import the given candidates into the library."""
    if DRY_RUN:
        log.info(f"  [DRY RUN] Would import {len(candidates)} file(s) into Lidarr:")
        for c in candidates:
            artist = c.get("artist", {}).get("artistName", "?")
            album  = c.get("album",  {}).get("title", "?")
            log.info(f"    → {c.get('path')}  ({artist} / {album})")
        return

    resp = requests.post(
        f"{LIDARR_URL}/api/v1/manualimport",
        headers=_lidarr_headers(),
        json=candidates,
        timeout=30,
    )
    resp.raise_for_status()
    log.info(f"  Imported {len(candidates)} file(s) into Lidarr")


# ══════════════════════════════════════════════════════════════════════════════
# File discovery
# ══════════════════════════════════════════════════════════════════════════════

def find_file_in_downloads(song: dict) -> Path | None:
    """
    Locate the audio file for a starred song inside DOWNLOADS_PATH.

    Strategy (most-specific to least-specific):
      1. Exact filename match using the path Navidrome reported
      2. Walk the downloads tree looking for the same filename
      3. Fuzzy match: file whose stem contains the track title
    """
    downloads = Path(DOWNLOADS_PATH)
    song_path = song.get("path", "")
    filename  = Path(song_path).name
    title     = song.get("title", "").lower().strip()

    # 1 — exact filename hit
    if filename:
        direct = downloads / filename
        if direct.exists():
            return direct

        # 2 — recursive filename search
        for f in downloads.rglob(filename):
            if f.is_file():
                return f

    # 3 — fuzzy title search (last resort)
    if title:
        for f in downloads.rglob("*"):
            if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS:
                if title in f.stem.lower():
                    log.debug(f"  Fuzzy match: {f}")
                    return f

    return None


# ══════════════════════════════════════════════════════════════════════════════
# Per-song processing pipeline
# ══════════════════════════════════════════════════════════════════════════════

def process_song(song: dict) -> bool:
    """
    Full pipeline for one starred song.
    Returns True if the song was handled successfully (or in dry-run mode).

    Library differentiation
    ───────────────────────
    We check whether the file lives in DOWNLOADS_PATH (Aurral flows library)
    or not (main library, already managed by Lidarr).

    Flows track   → full pipeline: add artist + import file into permanent library
    Main library  → if PROCESS_MAIN_LIBRARY_STARS=true, add artist to Lidarr
                    if not already there (no import needed, file is already there)
                  → if PROCESS_MAIN_LIBRARY_STARS=false (default), skip silently
    """
    artist_name = song.get("artist", "").strip()
    title       = song.get("title",  "").strip()

    log.info(f"── Processing: {artist_name} — {title}")

    # ── Step 1: determine which library this song is from ───────────────────
    file_path   = find_file_in_downloads(song)
    from_aurral = file_path is not None

    if from_aurral:
        log.info(f"  Source: Aurral flows library  ({file_path})")
    else:
        # Not in the downloads folder — it's a main library track
        if PROCESS_MAIN_LIBRARY_STARS:
            log.info(f"  Source: main library  (no import needed — will ensure artist is in Lidarr)")
        else:
            log.info(f"  Source: main library — skipping "
                     f"(set PROCESS_MAIN_LIBRARY_STARS=true to add artist to Lidarr anyway)")
            # Mark as processed so we don't log this every poll cycle
            return True

    # ── Step 2: look up the artist in MusicBrainz ───────────────────────────
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

    # ── Main library track: nothing left to do ───────────────────────────────
    if not from_aurral:
        log.info(f"  ✓ Done (main library — artist ensured in Lidarr): {artist_name}")
        return True

    if DRY_RUN:
        log.info(f"  [DRY RUN] Would trigger manual import for: {file_path}")
        return True

    # ── Step 4: ask Lidarr to scan the file's folder ────────────────────────
    folder     = str(file_path.parent)
    artist_id  = artist["id"]
    candidates = lidarr_get_import_candidates(folder, artist_id)

    if not candidates:
        log.warning(f"  Lidarr found no importable files in {folder}")
        return False

    # Prefer the specific file we matched; fall back to all files in the folder
    matched = [c for c in candidates if Path(c.get("path", "")) == file_path]
    if not matched:
        log.debug(f"  Specific file not in candidates — importing all found in folder")
        matched = candidates

    # ── Step 5: import ───────────────────────────────────────────────────────
    lidarr_execute_import(matched)
    log.info(f"  ✓ Done (Aurral → permanent library): {artist_name} — {title}")
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

        # Save state after each song so a crash doesn't cause re-processing
        if success:
            processed.add(song_id)
            state["processed_ids"] = list(processed)
            save_state(state)


def main() -> None:
    log.info("═" * 60)
    log.info("navidrome-star-to-lidarr  starting up")
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
