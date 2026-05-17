# star-sync — Design Decisions & Development History

This document explains why star-sync exists, what approaches were tried during
development, what worked, what didn't, and why key architectural decisions were made.
It is intended as a reference for future maintenance.

---

## Why star-sync exists

The setup uses Navidrome for music playback, Lidarr for managing the permanent music
library, and Aurral for music discovery via rotating flow playlists. Aurral downloads
tracks from Soulseek into a temporary downloads folder and surfaces them in Navidrome
as playlists.

The problem: Aurral's flows rotate. If you hear a song you like, the file will
eventually be deleted when Aurral refreshes its playlists. There was no automated
way to say "I like this track, make it permanent" — you had to manually find the
artist in Lidarr and add them, which broke the discovery flow entirely.

The goal was: star a song in Navidrome → it ends up permanently in Lidarr and
downloaded to the music library, with no manual steps.

---

## What was tried and rejected before building star-sync

### Lidarr Import Lists
**Tried:** Spotify Playlist import list, Last.fm import list, Custom List (JSON).

**Why rejected:**
- Spotify import has a known bug where "monitor specific album" ends up monitoring
  the entire artist discography instead of just the album from the playlist.
- Last.fm integration was investigated but Navidrome does not send "love" events to
  Last.fm — it only scrobbles plays. So starring in Navidrome never populated Last.fm
  loved tracks, making the Last.fm → Lidarr pipeline impossible.
- Custom List only supports artist MusicBrainz IDs, not album or track IDs. There
  is no way to monitor a specific album via a Custom List.
- All import list approaches work at the artist level, not the album level.

### Beets integration for pre-tagging
**Tried:** Investigated calling the existing beets container via its httpshell plugin
(port 5555) to tag Aurral downloads before processing.

**Why rejected:** This would have made star-sync tightly coupled to a specific beets
configuration. Anyone else wanting to use star-sync would need beets running with
httpshell enabled, specific ports open, specific volume mounts, and specific config
flags (`copy: no`, `move: no`). The tool would no longer be self-contained or
replicatable. The problem was also solved more cleanly by reading MusicBrainz IDs
directly from Navidrome's API response.

### Running beets inside the star-sync container
**Tried:** Considered installing beets as a dependency inside the star-sync Alpine
container.

**Why rejected:** Beets has heavy dependencies — ffmpeg for replaygain, chromaprint
for acoustic fingerprinting, Genius API for lyrics, etc. Installing all of this would
bloat the image significantly and duplicate a setup that already exists in a separate
container. Not worth it when the MusicBrainz recording ID approach made it unnecessary.

### Manual Lidarr album addition
**Tried:** Investigated whether Lidarr has a way to manually add individual albums
or singles that don't appear in automatic indexing.

**Finding:** Lidarr does not support adding individual albums manually outside of
what it can find through MusicBrainz. If a release isn't properly indexed in
MusicBrainz with complete metadata (country, format), Lidarr will not index it and
there is no UI workaround. The "search" in Lidarr's Singles section returns results
from MusicBrainz — if the data is bad there, the search returns wrong results.

---

## Key architectural decisions

### Docker container with GitHub Actions CI
**Decision:** Build star-sync as a standalone Docker container published to
GitHub Container Registry (ghcr.io) via GitHub Actions.

**Why:** Keeps the tool self-contained and reproducible. Anyone can pull the image
without installing Python, dependencies, or cloning the repo. GitHub Actions builds
automatically on every push to main, so updates are a single `docker compose pull`
on the server.

**Alternative considered:** Running as a cron job or Unraid User Script. Rejected
because Docker is already the deployment model for everything else in the stack,
and a container is cleaner to manage, restart, and monitor.

### config.yaml with environment variable overrides
**Decision:** Primary configuration via a YAML file mounted into the container, with
environment variables able to override any setting.

**Why:** Most other containers in the stack use either a single .env file or a
config file. A YAML config is more readable than a flat .env for a tool with this
many settings, and the env var override capability means the config file can be
committed to a private repo without secrets (secrets go in the actual file on the
server, not the template in git).

**What was tried first:** A flat .env file with STAR_SYNC_ prefixed variables mapped
through the compose environment block. This was redundant — every variable appeared
twice (once in .env with a prefix, once in compose without). Replaced with the
config.yaml approach where the compose file just has a volume mount.

### Separate sessions for Navidrome, Lidarr, and MusicBrainz
**Decision:** Three separate `requests.Session()` objects — one per service.

**Why:** Initially a single session was shared between Navidrome and Lidarr. This
is technically wrong — sessions carry state (headers, cookies, connection pools)
and mixing two different services with different auth schemes in one session is
fragile. MusicBrainz has a different User-Agent requirement. Three sessions keeps
concerns separated.

### MusicBrainz artist lookup priority chain
**Decision:** Four-tier fallback for finding the MusicBrainz artist ID:
1. Recording ID from Navidrome's API → MusicBrainz recording lookup
2. Artist MBID from file tags → read with mutagen
3. albumArtists[0] text search (not artists[0])
4. Track artist text search with multi-artist splitting

**Why this order:**

The Navidrome `getStarred2` API response includes `musicBrainzId` (a recording ID)
for well-tagged files. Looking up a recording by ID via MusicBrainz returns the exact
artist with no ambiguity — no text search needed. This is the fastest and most
accurate path.

File tags are tried next because they may contain a MusicBrainz artist ID directly,
which is also unambiguous. `mutagen` reads these tags.

`albumArtists` is used for text search rather than `artists` because for collaborative
tracks, `artists` contains all performers while `albumArtists` contains who actually
owns the album. For example, "Yeah! feat. Lil Jon & Ludacris" has `artists` =
[Lil Jon, Ludacris, USHER] but `albumArtists` = [USHER]. Searching for Lil Jon
would find the wrong artist; searching for USHER finds Confessions correctly.

Multi-artist string splitting (tier 4) handles cases where a file has no MusicBrainz
recording ID, no embedded artist MBID, and the artist field contains a combined
string like "The Chainsmokers; Halsey". The regex splits on `;`, `/`, `&`, `feat.`,
`ft.`, `x` and tries the first part.

**What was tried and rejected:** Originally only text search on the `artist` field
was used. This failed for multi-artist tracks and returned wrong results when
the combined string wasn't in MusicBrainz. The recording ID approach eliminated
most of these failures.

### albumArtists instead of artists for text search
**Why:** Discovered through a real failure — "Yeah! feat. Lil Jon & Ludacris" was
being searched as "Lil Jon; Ludacris; USHER" because that was the `artist` field
value. This found Lil Jon in MusicBrainz but the album "Confessions" belongs to
USHER. Using `albumArtists[0]` ("USHER") correctly identified the album owner.

### No "first album fallback" in album matching
**Decision:** If album name matching fails, the script returns None rather than
defaulting to the first album in the artist's discography.

**Why:** Defaulting to the first album would silently monitor and download the wrong
release. It's better to fail explicitly and write to the pending file than to
accidentally queue a download for something the user didn't want.

### Atomic state file writes
**Decision:** State is written to a `.tmp` file and then renamed atomically.

**Why:** If the container crashes or is killed mid-write, a partial write would
corrupt `state.json` and cause the script to lose track of what was processed,
potentially re-processing songs and creating duplicate Lidarr entries. The rename
operation is atomic on Linux — either the old file exists or the new one does,
never a partial state.

### Separate processed_ids and skipped_ids in state
**Decision:** Songs skipped because `process_main_library_stars` is false go into
`skipped_ids`, not `processed_ids`.

**Why:** If a song is in `processed_ids`, it is never reconsidered regardless of
config changes. If it's in `skipped_ids`, it will be reconsidered when
`process_main_library_stars` is set to true — the script detects this and clears
the skipped list automatically on the next poll.

### Pending interventions file (pending.yaml)
**Decision:** When album matching fails, write the item to a YAML file that the user
can edit to provide a MusicBrainz release group ID, which the script reads and acts
on automatically.

**Why:** The alternative was just logging an error. But logs are ephemeral and easy
to miss. A persistent file gives the user a clear record of what needs attention and
a structured way to intervene. The user finds the MusicBrainz release group URL
(easy — just google "artist album musicbrainz"), copies the UUID, pastes it in, saves
— the script handles the rest on the next poll.

**Format decision:** YAML was chosen over JSON because it supports comments, which
are used for the instructions header. The header is written once on first file
creation and never overwritten, so the user's filled-in values are always preserved.

**What was tried and rejected:** A `status` field for tracking state. Replaced with
just checking whether `mb_release_group_id` has a value — simpler, fewer fields for
the user to understand, and the script manages its own retry tracking via `retry_count`.

**YAML writer fix:** The initial implementation used string surgery to strip the
`items:` key from `yaml.dump()` output because the header already ended with
`items:`. This was fragile and produced invalid YAML in edge cases. Fixed by removing
`items:` from the header and letting `yaml.dump({"items": items})` write the complete
structure including the key.

### Retry limit with rescue fallback
**Decision:** After `pending_max_retries` attempts (default 5), copy the file to a
rescue folder rather than continuing to retry indefinitely.

**Why:** Some releases have incomplete MusicBrainz metadata (missing country/format
fields) that causes Lidarr to never index them. Without a retry limit, the script
would refresh Lidarr's metadata for that artist every 5 minutes forever — wasteful
and noisy. After 5 attempts it's clear the problem is structural, not transient.

The rescue step exists because Aurral's flows rotate. Without rescue, a file that
Lidarr can't index would simply disappear when Aurral refreshes, with nothing to
show for the star. Copying it to a permanent folder preserves the track.

### Rescue folder location
**Decision:** A dedicated `/mnt/user/docker_media/rescued/` folder, separate from
both the Lidarr-managed music library and the Aurral downloads folder.

**Why Aurral downloads was rejected as rescue location:** Aurral manages its own
subdirectories (e.g. `aurral-weekly-flow/`) and rotates them. A `rescued/` subfolder
within the downloads directory would technically be safe from Aurral's rotation, but
keeping it separate makes the purpose clearer and easier to manage.

**Why Lidarr's root folder was rejected:** Lidarr would see files it has no record
of and its behaviour is unpredictable — it might try to delete, rename, or import
them incorrectly. Files in the rescue folder are explicitly outside Lidarr's
management.

**Why a separate Navidrome library:** The rescue folder is added as a distinct
Navidrome library ("Rescued Library") rather than being merged with the main library
or the Aurral flows library. This makes rescued tracks easy to identify — you know
a track in that library couldn't be handled automatically and may need attention
(e.g. fixing MusicBrainz data).

### Log rotation
**Decision:** Docker json-file logging with `max-size: 10m` and `max-file: 3`.

**Why:** Docker's default json-file driver grows without bound. For a container
that polls every 5 minutes indefinitely, logs would eventually fill the disk.
Three files of 10MB each (30MB total) is ample for debugging recent issues while
preventing runaway growth. This should be applied to all long-running containers,
not just star-sync.

### Graceful shutdown via threading.Event
**Decision:** The main sleep loop uses `stop_event.wait(timeout=POLL_INTERVAL)`
instead of `time.sleep(POLL_INTERVAL)`.

**Why:** `time.sleep()` ignores SIGTERM signals. When Docker stops a container it
sends SIGTERM and waits 10 seconds before force-killing. With `time.sleep()` the
container always takes the full 10 seconds to stop. With `threading.Event.wait()`,
the SIGTERM handler sets the event and the sleep returns immediately, allowing clean
shutdown in under a second.

---

## Known limitations

### Singles with incomplete MusicBrainz metadata
Some singles are not fully indexed in MusicBrainz — they appear on the artist's page
but lack Country and Format fields. Lidarr uses these fields to decide whether to
index a release. Releases missing them are silently skipped. There is no workaround
within Lidarr; the fix is to edit the MusicBrainz data directly.

### Aurral file rotation timing
The script polls every 5 minutes (configurable). If Aurral rotates a flow between
when the user stars a song and when the script polls, the file will be gone and the
star will be processed but the rescue step will fail with "file not found." The
window for this is small but not zero. Reducing `poll_interval` reduces the risk.

### MusicBrainz rate limiting
MusicBrainz requires at most 1 request per second. The script enforces this via
`mb_rate_limit` (default 1.2s). Processing many starred songs in one poll will be
slow proportionally. This is by design and cannot be worked around without violating
MusicBrainz's terms of service.

### Album monitoring, not track monitoring
Lidarr monitors at the album level, not the track level. Starring a single track
results in the entire album being monitored and downloaded. This is an intentional
tradeoff — Lidarr doesn't support track-level monitoring, and downloading the full
album is generally the right behaviour for music collection management.

### Navidrome multi-library Favourites UI
Navidrome's Favourites view does not always show starred songs across all libraries
when multiple libraries are configured. This is a known Navidrome UI issue. The
Subsonic API (`getStarred2`) correctly returns all starred songs regardless of library,
so star-sync is not affected — only the visual display in Navidrome is inconsistent.