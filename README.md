# star-sync

Watches [Navidrome](https://www.navidrome.org/) for starred tracks and automatically adds the artist and album to [Lidarr](https://lidarr.audio/) for download.

## How it works

1. ❤️ You star a song in Navidrome (from an [Aurral](https://github.com/lklynet/aurral) flow playlist)
2. 🔍 star-sync detects it via the Navidrome Subsonic API
3. 🎵 Looks up the artist in MusicBrainz (via recording ID, file tags, or name search)
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

### 2. Create your config file

Copy `config.yaml.template` from this repo to your server at:
```
/mnt/user/appdata/stacks/music_media/star-sync/config.yaml
```
Fill in your values. This file contains real credentials and should never be committed to git.

### 3. Add to your docker-compose.yml

```yaml
star-sync:
  image: ghcr.io/m-a-johnson/star-sync:latest
  container_name: star-sync
  volumes:
    - /mnt/user/appdata/stacks/music_media/star-sync/config.yaml:/config/config.yaml:ro
    - /mnt/user/appdata/stacks/music_media/star-sync/data:/data
    - /mnt/user/docker_media/aurral/downloads:/downloads:ro
  networks:
    - main_network
  restart: unless-stopped
```

### 4. Configuration

All settings live in `config.yaml`. Any setting can be overridden by setting the
corresponding environment variable (uppercase). For example `dry_run: true` in the
config can be overridden by setting `DRY_RUN=false` in the compose environment block.

| Setting | Description | Default |
|---|---|---|
| `navidrome_url` | Navidrome URL | `http://navidrome:4533` |
| `navidrome_user` | Navidrome username | |
| `navidrome_pass` | Navidrome password | |
| `navidrome_flows_library_id` | Library ID to filter stars from. Find via `/rest/getMusicFolders.view`. Leave empty for all libraries. | |
| `lidarr_url` | Lidarr URL | `http://lidarr:8686` |
| `lidarr_api_key` | Lidarr API key (Settings → General) | |
| `lidarr_root_folder` | Root folder path inside Lidarr's container | |
| `lidarr_quality_profile_id` | Lidarr quality profile ID | `1` |
| `lidarr_metadata_profile_id` | Lidarr metadata profile ID | `1` |
| `downloads_path` | Path to Aurral downloads folder inside this container | `/downloads` |
| `state_file` | Path to state file (tracks processed songs) | `/data/state.json` |
| `pending_file` | Path to pending interventions file | `/data/pending.yaml` |
| `poll_interval` | Seconds between Navidrome polls | `300` |
| `dry_run` | Log actions without making changes | `true` |
| `process_main_library_stars` | Also add artists from main library stars to Lidarr | `false` |
| `artist_wait_timeout` | Seconds to wait for Lidarr to index a new artist | `120` |
| `album_wait_timeout` | Seconds to wait for Lidarr to load albums | `120` |
| `mb_rate_limit` | Seconds between MusicBrainz requests (min 1.0) | `1.2` |
| `log_level` | Log verbosity: DEBUG, INFO, WARNING, ERROR | `INFO` |

### 5. Find your Flows library ID

Open this URL in your browser (replace credentials):

```
http://your-navidrome/rest/getMusicFolders.view?u=USER&p=PASS&v=1.16.0&c=test&f=json
```

Use the `id` value for your Aurral/flows library.

### 6. Find your Lidarr profile IDs

```
http://your-lidarr/api/v1/qualityprofile?apikey=YOUR_KEY
http://your-lidarr/api/v1/metadataprofile?apikey=YOUR_KEY
```

## Artist lookup

star-sync uses a priority chain to find the correct MusicBrainz artist ID:

1. **Recording ID from Navidrome** — most accurate, used when the file has a MusicBrainz recording ID embedded
2. **Artist MBID from file tags** — reads embedded MusicBrainz artist ID using mutagen
3. **Album artist text search** — uses `albumArtists[0]` to avoid multi-artist confusion (e.g. uses "Usher" not "Lil Jon; Ludacris; Usher")
4. **Track artist text search** — last resort, splits multi-artist strings on common separators (`;`, `&`, `feat.`, etc.)

## Pending interventions

When star-sync cannot match an album in Lidarr (e.g. for singles or albums with different titles), it writes the item to `pending.yaml` rather than silently failing.

To resolve a pending item:

1. Open `pending.yaml` on your server
2. Find the unresolved item (empty `mb_release_group_id`)
3. Search MusicBrainz for the artist + album: `https://musicbrainz.org`
4. Copy the UUID from the release group URL:
   ```
   https://musicbrainz.org/release-group/87f8f3b6-476e-40b0-8f5f-ea2ebc1743a2
                                         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
   ```
5. Paste it into `mb_release_group_id` and save the file
6. star-sync picks it up on the next poll, monitors the album, and removes it from the file

## Dry run mode

`dry_run` defaults to `true` in the template — preview all actions without touching Lidarr.

After confirming everything looks correct, set `dry_run: false` in your config and reset
the state file so existing stars get processed:

```bash
rm /mnt/user/appdata/stacks/music_media/star-sync/data/state.json
docker compose up -d star-sync
```

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

## License

MIT