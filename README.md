# Media Manager

A self-hosted web app for managing your Plex/Radarr/Sonarr media stack. Browse your libraries, identify stale or low-quality media, trigger quality upgrades from [Medialyze](https://medialyze.app) CSV exports, and schedule everything to run automatically — all from a mobile-friendly UI.

![Docker](https://img.shields.io/badge/docker-ghcr.io%2Fcompactly8274%2Fuprademedia-blue)

---

## Features

- **Library browser** — live view of every movie and series from Radarr and Sonarr, sortable and searchable, with one-tap delete
- **Analyze** — scans Plex to find media that's large and hasn't been watched recently, scored by `size × days_stale`; candidates can be deleted directly from the results
- **Quality upgrade** — upload a Medialyze CSV export and trigger Radarr/Sonarr to search for better versions of anything below your quality threshold
- **Schedules** — set cron-based schedules to run Analyze or Upgrade jobs automatically
- **Run history** — full log of every job, expandable inline
- **Delete to exclusion list** — deleting anything (from Library, Analyze, or candidates) also adds it to Radarr's import exclusion / Sonarr's import list exclusion so it won't be re-downloaded
- **Mobile-friendly** — bottom tab bar on iPhone, horizontal-scrolling tables, responsive layouts throughout

---

## Quick Start

### Docker Compose (recommended)

```yaml
services:
  mediamanager:
    image: ghcr.io/compactly8274/upgrademedia:latest
    ports:
      - "8000:8000"
    volumes:
      - mediamanager-data:/data
    environment:
      RADARR_URL: http://192.168.1.x:7878
      RADARR_API_KEY: ""
      SONARR_URL: http://192.168.1.x:8989
      SONARR_API_KEY: ""
      PLEX_URL: http://192.168.1.x:32400
      PLEX_TOKEN: ""
      QUALITY_THRESHOLD: "60"
      MIN_SIZE_GB: "0.5"
      MIN_DAYS_STALE: "90"
    restart: unless-stopped

volumes:
  mediamanager-data:
```

```bash
docker compose up -d
```

Open **http://your-server:8000** in a browser or on your phone.

Settings can also be changed at any time from the Settings tab in the UI — no container restart needed.

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `RADARR_URL` | — | Radarr base URL, e.g. `http://192.168.1.10:7878` |
| `RADARR_API_KEY` | — | Radarr API key (Settings → General → API Key) |
| `SONARR_URL` | — | Sonarr base URL, e.g. `http://192.168.1.10:8989` |
| `SONARR_API_KEY` | — | Sonarr API key (Settings → General → API Key) |
| `PLEX_URL` | — | Plex Media Server URL, e.g. `http://192.168.1.10:32400` |
| `PLEX_TOKEN` | — | Plex auth token ([how to find yours](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)) |
| `QUALITY_THRESHOLD` | `60` | Upgrade tab default: files scoring at or below this are queued |
| `MIN_SIZE_GB` | `0.5` | Analyze tab default: minimum file size to consider as a candidate |
| `MIN_DAYS_STALE` | `90` | Analyze tab default: minimum days since last watched |

Values set in the UI (Settings tab) are persisted in the SQLite database at `/data/medialyze.db` and take precedence over environment variables.

---

## Usage

### Library

Browse all movies and series directly from Radarr and Sonarr. Sort by title, year, or size. Search by name. Hit **Del** to delete files from disk — the item is also added to the respective exclusion list so it won't be re-imported.

### Analyze

Scans your Plex libraries and scores each title by `size_gb × days_since_last_watched`. Higher score = bigger and more stale = better candidate for removal. Filter the results by size, staleness, and media type. Delete candidates directly from the list.

Run on demand with **Scan Now**, or set a schedule in the Schedules tab.

### Quality Upgrade

Pairs with [Medialyze](https://medialyze.app) to find and replace low-quality encodes:

1. Export a CSV from Medialyze
2. Upload it in the Upgrade tab
3. Set your quality score threshold (files at or below this score are processed)
4. Optionally enable **Dry run** to preview matches without sending any commands
5. Click **Run Upgrade** — matched movies trigger a `MoviesSearch` in Radarr; matched series trigger a `SeriesSearch` in Sonarr

The run log (visible in the History tab) shows exactly how many rows were parsed, how many fell below the threshold, and which titles matched or failed to match.

### Schedules

Add cron expressions to run Analyze or Upgrade jobs automatically.

| Expression | Meaning |
|---|---|
| `0 3 * * *` | Daily at 3 AM |
| `0 3 * * 0` | Weekly on Sunday at 3 AM |
| `0 */6 * * *` | Every 6 hours |

Enable or disable individual schedules without deleting them.

### History

Full log of every run. Click any row to expand the raw log output.

---

## Data

All state is stored in a single SQLite database at `/data/medialyze.db` inside the container. Mount a named volume (as shown in the compose file) to persist it across container updates.

```bash
# Backup
docker cp mediamanager:/data/medialyze.db ./medialyze.db.bak

# Restore
docker cp ./medialyze.db.bak mediamanager:/data/medialyze.db
```

---

## Building Locally

```bash
git clone https://github.com/compactly8274/upgrademedia
cd upgrademedia
docker build -t mediamanager .
docker run -p 8000:8000 -v mediamanager-data:/data mediamanager
```

The app is a FastAPI backend (`app/`) serving a single-page Alpine.js + Tailwind CSS frontend (`static/index.html`). No build step required.

---

## License

MIT
