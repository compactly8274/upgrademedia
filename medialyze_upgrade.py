#!/usr/bin/env python3
"""
Read a Medialyze CSV of low-quality files, match each entry against Radarr
(movies) and/or Sonarr (TV episodes/series), and trigger search commands to
replace files that fall below a score threshold.

Usage:
    python medialyze_upgrade.py --csv medialyze_export.csv \\
        --threshold 60 \\
        --radarr-url http://192.168.1.x:7878 --radarr-api-key YOUR_KEY \\
        --sonarr-url http://192.168.1.x:8989 --sonarr-api-key YOUR_KEY \\
        [--dry-run]

CSV columns expected (case-insensitive):
    type      - "movie" or "episode" (required to route the row)
    title     - movie or series title
    year      - release year (movies)
    season    - season number (episodes)
    episode   - episode number (optional; omit to trigger a full series search)
    score     - quality score
    filename  - optional, improves matching accuracy
"""

import argparse
import csv
import logging
import sys
import time
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def _normalise_headers(row: dict) -> dict:
    return {k.strip().lower(): v.strip() for k, v in row.items()}


def load_csv(path: str, threshold: float) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            row = _normalise_headers(raw)
            if "score" not in row:
                log.warning("Row missing 'score' column, skipping: %s", row)
                continue
            try:
                score = float(row["score"])
            except ValueError:
                log.warning("Non-numeric score %r, skipping row", row["score"])
                continue
            if score < threshold:
                rows.append(row)
    log.info("Loaded %d rows below threshold %.1f from %s", len(rows), threshold, path)
    return rows


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _stem(filename: str) -> str:
    return Path(filename).stem.lower()


# ---------------------------------------------------------------------------
# Radarr client
# ---------------------------------------------------------------------------

class RadarrClient:
    def __init__(self, base_url: str, api_key: str):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"X-Api-Key": api_key, "Accept": "application/json"})

    def _get(self, endpoint: str, **params):
        resp = self.session.get(f"{self.base}/api/v3/{endpoint}", params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _post(self, endpoint: str, payload: dict):
        resp = self.session.post(f"{self.base}/api/v3/{endpoint}", json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def get_all_movies(self) -> list[dict]:
        return self._get("movie")

    def search_movie(self, movie_id: int):
        return self._post("command", {"name": "MoviesSearch", "movieIds": [movie_id]})


def find_radarr_movie(row: dict, library: list[dict]) -> dict | None:
    csv_filename = row.get("filename", "")
    csv_title = row.get("title", "")
    csv_year = row.get("year", "")

    # 1. Filename stem match
    if csv_filename:
        csv_stem = _stem(csv_filename)
        for movie in library:
            mf = movie.get("movieFile")
            if mf and mf.get("relativePath") and _stem(mf["relativePath"]) == csv_stem:
                return movie

    # 2. Title + year
    if csv_title and csv_year:
        key = f"{csv_title.strip().lower()} ({csv_year.strip()})"
        for movie in library:
            radarr_key = f"{movie.get('title', '').strip().lower()} ({movie.get('year', '')})"
            if radarr_key == key:
                return movie

    # 3. Title only
    if csv_title:
        csv_title_lc = csv_title.strip().lower()
        for movie in library:
            if movie.get("title", "").strip().lower() == csv_title_lc:
                return movie

    return None


# ---------------------------------------------------------------------------
# Sonarr client
# ---------------------------------------------------------------------------

class SonarrClient:
    def __init__(self, base_url: str, api_key: str):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"X-Api-Key": api_key, "Accept": "application/json"})

    def _get(self, endpoint: str, **params):
        resp = self.session.get(f"{self.base}/api/v3/{endpoint}", params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _post(self, endpoint: str, payload: dict):
        resp = self.session.post(f"{self.base}/api/v3/{endpoint}", json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def get_all_series(self) -> list[dict]:
        return self._get("series")

    def get_episodes(self, series_id: int) -> list[dict]:
        return self._get("episode", seriesId=series_id)

    def search_series(self, series_id: int):
        return self._post("command", {"name": "SeriesSearch", "seriesId": series_id})

    def search_episode(self, episode_ids: list[int]):
        return self._post("command", {"name": "EpisodeSearch", "episodeIds": episode_ids})


def find_sonarr_series(row: dict, series_list: list[dict]) -> dict | None:
    csv_title = row.get("title", "")
    csv_filename = row.get("filename", "")

    # 1. Filename: check if the series folder name appears in the file stem
    if csv_filename:
        csv_stem = _stem(csv_filename)
        for s in series_list:
            folder = Path(s.get("path", "")).name.lower()
            if folder and folder in csv_stem:
                return s

    # 2. Exact title match
    if csv_title:
        csv_title_lc = csv_title.strip().lower()
        for s in series_list:
            if s.get("title", "").strip().lower() == csv_title_lc:
                return s

    return None


def find_sonarr_episode(row: dict, series_id: int, client: SonarrClient) -> dict | None:
    season_str = row.get("season", "")
    episode_str = row.get("episode", "")
    if not season_str or not episode_str:
        return None
    try:
        season = int(season_str)
        episode = int(episode_str)
    except ValueError:
        return None

    for ep in client.get_episodes(series_id):
        if ep.get("seasonNumber") == season and ep.get("episodeNumber") == episode:
            return ep
    return None


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

def process_movie_rows(
    rows: list[dict],
    client: RadarrClient,
    dry_run: bool,
    delay: float,
) -> tuple[int, int, int]:
    log.info("Fetching Radarr movie library...")
    library = client.get_all_movies()
    log.info("Radarr library: %d movies", len(library))

    matched = unmatched = triggered = 0
    for row in rows:
        label = row.get("title") or row.get("filename") or str(row)
        score = row.get("score", "?")
        movie = find_radarr_movie(row, library)
        if movie is None:
            log.warning("No Radarr match: %s (score=%s)", label, score)
            unmatched += 1
            continue

        matched += 1
        mid = movie["id"]
        title = f"{movie['title']} ({movie.get('year', '?')})"

        if dry_run:
            log.info("[DRY-RUN] Would search movie: %s  id=%d  score=%s", title, mid, score)
            continue

        try:
            result = client.search_movie(mid)
            log.info("Triggered movie search: %s  id=%d  score=%s  cmd=%s",
                     title, mid, score, result.get("id", "?"))
            triggered += 1
        except requests.RequestException as exc:
            log.error("Command failed for %s: %s", title, exc)

        if delay > 0:
            time.sleep(delay)

    return matched, unmatched, triggered


def process_episode_rows(
    rows: list[dict],
    client: SonarrClient,
    dry_run: bool,
    delay: float,
) -> tuple[int, int, int]:
    log.info("Fetching Sonarr series library...")
    series_list = client.get_all_series()
    log.info("Sonarr library: %d series", len(series_list))

    matched = unmatched = triggered = 0
    for row in rows:
        label = row.get("title") or row.get("filename") or str(row)
        score = row.get("score", "?")

        series = find_sonarr_series(row, series_list)
        if series is None:
            log.warning("No Sonarr match: %s (score=%s)", label, score)
            unmatched += 1
            continue

        matched += 1
        sid = series["id"]
        series_title = series["title"]

        episode = find_sonarr_episode(row, sid, client)
        if episode:
            ep_label = f"{series_title} S{row.get('season', '?'):>02}E{row.get('episode', '?'):>02}"
            if dry_run:
                log.info("[DRY-RUN] Would search episode: %s  id=%d  score=%s",
                         ep_label, episode["id"], score)
            else:
                try:
                    result = client.search_episode([episode["id"]])
                    log.info("Triggered episode search: %s  id=%d  score=%s  cmd=%s",
                             ep_label, episode["id"], score, result.get("id", "?"))
                    triggered += 1
                except requests.RequestException as exc:
                    log.error("Command failed for %s: %s", ep_label, exc)
        else:
            if dry_run:
                log.info("[DRY-RUN] Would search series: %s  id=%d  score=%s",
                         series_title, sid, score)
            else:
                try:
                    result = client.search_series(sid)
                    log.info("Triggered series search: %s  id=%d  score=%s  cmd=%s",
                             series_title, sid, score, result.get("id", "?"))
                    triggered += 1
                except requests.RequestException as exc:
                    log.error("Command failed for %s: %s", series_title, exc)

        if delay > 0:
            time.sleep(delay)

    return matched, unmatched, triggered


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Trigger Radarr/Sonarr searches for Medialyze low-quality files"
    )
    p.add_argument("--csv", required=True, help="Path to Medialyze CSV export")
    p.add_argument(
        "--threshold", type=float, default=60.0,
        help="Quality score threshold; rows below this are targeted (default: 60)",
    )
    p.add_argument("--radarr-url", default="http://192.168.1.x:7878", help="Radarr base URL")
    p.add_argument("--radarr-api-key", help="Radarr API key")
    p.add_argument("--sonarr-url", default="http://192.168.1.x:8989", help="Sonarr base URL")
    p.add_argument("--sonarr-api-key", help="Sonarr API key")
    p.add_argument(
        "--dry-run", action="store_true",
        help="Print what would happen without sending any commands",
    )
    p.add_argument(
        "--delay", type=float, default=0.5,
        help="Seconds to wait between API commands (default: 0.5)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if not args.radarr_api_key and not args.sonarr_api_key:
        log.error("At least one of --radarr-api-key or --sonarr-api-key must be provided")
        return 1

    rows = load_csv(args.csv, args.threshold)
    if not rows:
        log.info("Nothing to do.")
        return 0

    movie_rows, episode_rows, unknown_rows = [], [], []
    for row in rows:
        media_type = row.get("type", "").lower()
        if media_type == "movie":
            movie_rows.append(row)
        elif media_type in ("episode", "series", "show", "tv"):
            episode_rows.append(row)
        else:
            unknown_rows.append(row)

    if unknown_rows:
        log.warning(
            "%d rows have unrecognised or missing 'type' column (expected 'movie' or 'episode'); skipping.",
            len(unknown_rows),
        )

    total_matched = total_unmatched = total_triggered = 0

    if movie_rows:
        if not args.radarr_api_key:
            log.warning("Skipping %d movie rows — no --radarr-api-key provided", len(movie_rows))
        else:
            radarr = RadarrClient(args.radarr_url, args.radarr_api_key)
            try:
                m, u, t = process_movie_rows(movie_rows, radarr, args.dry_run, args.delay)
                total_matched += m
                total_unmatched += u
                total_triggered += t
            except requests.RequestException as exc:
                log.error("Failed to reach Radarr: %s", exc)
                return 1

    if episode_rows:
        if not args.sonarr_api_key:
            log.warning("Skipping %d episode rows — no --sonarr-api-key provided", len(episode_rows))
        else:
            sonarr = SonarrClient(args.sonarr_url, args.sonarr_api_key)
            try:
                m, u, t = process_episode_rows(episode_rows, sonarr, args.dry_run, args.delay)
                total_matched += m
                total_unmatched += u
                total_triggered += t
            except requests.RequestException as exc:
                log.error("Failed to reach Sonarr: %s", exc)
                return 1

    log.info(
        "Done. matched=%d  unmatched=%d  triggered=%d  dry_run=%s",
        total_matched, total_unmatched, total_triggered, args.dry_run,
    )
    return 0 if total_unmatched == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
