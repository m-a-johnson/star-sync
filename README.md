# star-sync

Watches [Navidrome](https://www.navidrome.org/) for starred tracks and automatically adds the artist and album to [Lidarr](https://lidarr.audio/) for download.

## How it works

1. ❤️ You star a song in Navidrome (from an [Aurral](https://github.com/lklynet/aurral) flow playlist)
2. 🔍 star-sync detects it via the Navidrome Subsonic API
3. 🎵 Looks up the artist in MusicBrainz
4. ➕ Adds the artist to Lidarr as **unmonitored** (no whole-discography downloads)
5. 📀 Finds the matching album and sets it to **monitored**
6. ⬇️ Triggers a search — Lidarr starts downloading
7. 🗂️ Lidarr organises it into your permanent library
8. 🎧 Navidrome picks it up on next scan

## Requirements

- Navidrome (with Aurral flows library configured)
- Lidarr
- Docker

## Setup

### 1. Pull the image

```bash
docker pull ghcr.io/m-a-johnson/star-sync:latest
```

### 2. Add to your docker-compose.yml

```yaml
star-sync:
  image: ghcr.io/m-a-johnson/star-sync:latest
  container_name: star-sync
  networks:
    - main_network
  environment:
    - TZ=America/Edmonton
    - NAVIDROME_URL=http://navidrome:4533
    - NAVIDROME_USER=${STAR_SYNC_ND_USER}
    - NAVIDROME_PASS=${STAR_SYNC_ND_PASS}
    - NAVIDROME_FLOWS_LIBRARY_ID=${STAR_SYNC_NAVIDROME_FLOWS_LIBRARY_ID}
    - LIDARR_URL=http://lidarr:8686
    - LIDARR_API_KEY=${STAR_SYNC_LIDARR_API_KEY}
    - LIDARR_ROOT_FOLDER=/data/media/music
    - LIDARR_QUALITY_PROFILE_ID=${STAR_SYNC_QUALITY_PROFILE_ID:-1}
    - LIDARR_METADATA_PROFILE_ID=${STAR_SYNC_METADATA_PROFILE_ID:-1}
    - DOWNLOADS_PATH=/downloads
    - STATE_FILE=/data/state.json
    - POLL_INTERVAL=${STAR_SYNC_POLL_INTERVAL:-300}
    - DRY_RUN=${STAR_SYNC_DRY_RUN:-false}
    - PROCESS_MAIN_LIBRARY_STARS=${STAR_SYNC_PROCESS_MAIN_LIBRARY_STARS:-false}
    - LOG_LEVEL=${STAR_SYNC_LOG_LEVEL:-INFO}
    - ALBUM_WAIT_TIMEOUT=${STAR_SYNC_ALBUM_WAIT_TIMEOUT:-120}
  volumes:
    - /mnt/user/appdata/stacks/music_media/star-sync/data:/data
    - /mnt/user/docker_media/aurral/downloads:/downloads:ro
  restart: unless-stopped
```

### 3. Configure environment variables

| Variable | Description | Default |
|---|---|---|
| `NAVIDROME_URL` | Navidrome URL | `http://navidrome:4533` |
| `NAVIDROME_USER` | Navidrome username | `admin` |
| `NAVIDROME_PASS` | Navidrome password | |
| `NAVIDROME_FLOWS_LIBRARY_ID` | Library ID to filter stars from. Find via `/rest/getMusicFolders.view`. Leave empty for all libraries. | |
| `LIDARR_URL` | Lidarr URL | `http://lidarr:8686` |
| `LIDARR_API_KEY` | Lidarr API key (Settings → General) | |
| `LIDARR_ROOT_FOLDER` | Root folder path inside Lidarr's container | `/music/library` |
| `LIDARR_QUALITY_PROFILE_ID` | Lidarr quality profile ID | `1` |
| `LIDARR_METADATA_PROFILE_ID` | Lidarr metadata profile ID | `1` |
| `DOWNLOADS_PATH` | Path to Aurral downloads folder inside this container | `/downloads` |
| `STATE_FILE` | Path to state file (tracks processed songs) | `/data/state.json` |
| `POLL_INTERVAL` | Seconds between Navidrome polls | `300` |
| `DRY_RUN` | Log actions without making changes | `false` |
| `PROCESS_MAIN_LIBRARY_STARS` | Also add artists from main library stars | `false` |
| `ARTIST_WAIT_TIMEOUT` | Seconds to wait for Lidarr to index a new artist | `120` |
| `ALBUM_WAIT_TIMEOUT` | Seconds to wait for Lidarr to load albums | `120` |
| `MB_RATE_LIMIT` | Seconds between MusicBrainz requests (min 1.0) | `1.2` |
| `LOG_LEVEL` | Log verbosity: DEBUG, INFO, WARNING, ERROR | `INFO` |

### 4. Find your Flows library ID

Open this URL in your browser (replace credentials):

```
https://your-navidrome/rest/getMusicFolders.view?u=USER&p=PASS&v=1.16.0&c=test&f=json
```

Use the `id` value for your Aurral/flows library.

### 5. Find your Lidarr profile IDs

```
https://your-lidarr/api/v1/qualityprofile?apikey=YOUR_KEY
https://your-lidarr/api/v1/metadataprofile?apikey=YOUR_KEY
```

## Dry run mode

Set `DRY_RUN=true` to preview all actions without touching Lidarr. Recommended for first run.

## Resetting state

To reprocess all starred songs from scratch:

```bash
rm /mnt/user/appdata/stacks/music_media/star-sync/data/state.json
docker compose up -d star-sync
```

## Building from source

The image is built automatically via GitHub Actions on every push to `main`.

```bash
git clone https://github.com/m-a-johnson/star-sync
cd star-sync
docker build -t star-sync .
```