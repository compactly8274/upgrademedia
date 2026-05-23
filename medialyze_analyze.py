#!/usr/bin/env python3
"""
Analyze your Plex library and rank media by size × days-since-last-watched to
surface the best candidates for removal.

Connects directly to the Plex Media Server API — no Radarr/Sonarr needed.

Finding your Plex token:
    https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/

Output columns:
    type         movie or show
    title        media title
    year         release year (movies only)
    size_gb      total on-disk file size in GB
    last_watched date of most recent play (or "never")
    days_stale   days since last watched (99999 if never)
    score        size_gb × days_stale  (higher = better removal candidate)

Usage:
    python medialyze_analyze.py \\
        --plex-url http://192.168.1.x:32400 \\
        --plex-token YOUR_TOKEN \\
        [--min-size-gb 0.5] [--min-days-stale 90] [--top 50] [--output out.csv]
"""

import argparse
import csv
import logging
import sys
from datetime import datetime, timezone

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

NOW = datetime.now(timezone.utc)
NEVER_DAYS = 99999


# ---------------------------------------------------------------------------
# Plex client
# ---------------------------------------------------------------------------

class PlexClient:
    def __init__(self, base_url: str, token: str):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "X-Plex-Token": token,
            "Accept": "application/json",
        })

    def _get(self, path: str, **params) -> dict:
        resp = self.session.get(f"{self.base}{path}", params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def sections(self) -> list[dict]:
        return self._get("/library/sections")["MediaContainer"].get("Directory", [])

    def all_items(self, section_key: str, media_type: int) -> list[dict]:
        data = self._get(f"/library/sections/{section_key}/all", type=media_type)
        return data["MediaContainer"].get("Metadata", [])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _file_size_bytes(item: dict) -> int:
    return sum(
        part.get("size", 0)
        for media in item.get("Media", [])
        for part in media.get("Part", [])
    )


def _days_stale(ts: int | None) -> int:
    if not ts:
        return NEVER_DAYS
    last = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    return max(0, (NOW - last).days)


def _date_str(ts: int | None) -> str:
    if not ts:
        return "never"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Library gathering
# ---------------------------------------------------------------------------

def gather_movies(client: PlexClient, section_key: str) -> list[dict]:
    items = client.all_items(section_key, media_type=1)
    log.info("  %d movies", len(items))
    rows = []
    for item in items:
        size_gb = _file_size_bytes(item) / 1024 ** 3
        ts = item.get("lastViewedAt")
        days = _days_stale(ts)
        rows.append({
            "type": "movie",
            "title": item.get("title", ""),
            "year": str(item.get("year", "")),
            "size_gb": round(size_gb, 2),
            "last_watched": _date_str(ts),
            "days_stale": days,
            "score": round(size_gb * days, 1),
        })
    return rows


def gather_shows(client: PlexClient, section_key: str) -> list[dict]:
    # Fetch all episodes to get per-file sizes and per-episode view dates,
    # then aggregate to the show level.
    episodes = client.all_items(section_key, media_type=4)
    log.info("  %d episodes across all shows", len(episodes))

    shows: dict[str, dict] = {}
    for ep in episodes:
        key = ep.get("grandparentRatingKey") or ep.get("grandparentTitle", "unknown")
        if key not in shows:
            shows[key] = {
                "title": ep.get("grandparentTitle", "unknown"),
                "size_bytes": 0,
                "latest_viewed_at": None,
            }
        shows[key]["size_bytes"] += _file_size_bytes(ep)
        ep_ts = ep.get("lastViewedAt")
        if ep_ts:
            ep_ts = int(ep_ts)
            prev = shows[key]["latest_viewed_at"]
            if prev is None or ep_ts > prev:
                shows[key]["latest_viewed_at"] = ep_ts

    rows = []
    for data in shows.values():
        size_gb = data["size_bytes"] / 1024 ** 3
        ts = data["latest_viewed_at"]
        days = _days_stale(ts)
        rows.append({
            "type": "show",
            "title": data["title"],
            "year": "",
            "size_gb": round(size_gb, 2),
            "last_watched": _date_str(ts),
            "days_stale": days,
            "score": round(size_gb * days, 1),
        })
    return rows


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

FIELDS = ["type", "title", "year", "size_gb", "last_watched", "days_stale", "score"]


def write_csv(rows: list[dict], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    log.info("Wrote %d candidates to %s", len(rows), path)


def print_table(rows: list[dict]) -> None:
    if not rows:
        print("No candidates found.")
        return
    widths = {
        f: max(len(f), max(len(str(r[f])) for r in rows))
        for f in FIELDS
    }
    header = "  ".join(f.ljust(widths[f]) for f in FIELDS)
    print(header)
    print("-" * len(header))
    for row in rows:
        print("  ".join(str(row[f]).ljust(widths[f]) for f in FIELDS))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Rank Plex media by size × staleness to surface removal candidates"
    )
    p.add_argument("--plex-url", default="http://192.168.1.x:32400", help="Plex server base URL")
    p.add_argument("--plex-token", required=True, help="Plex authentication token")
    p.add_argument(
        "--min-size-gb", type=float, default=0.5,
        help="Skip items smaller than this in GB (default: 0.5)",
    )
    p.add_argument(
        "--min-days-stale", type=int, default=90,
        help="Skip items watched within this many days (default: 90)",
    )
    p.add_argument(
        "--top", type=int, default=50,
        help="Show top N candidates; 0 = all (default: 50)",
    )
    p.add_argument("--output", help="Write results to CSV file instead of printing a table")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    client = PlexClient(args.plex_url, args.plex_token)

    log.info("Fetching Plex library sections...")
    try:
        sections = client.sections()
    except requests.RequestException as exc:
        log.error("Failed to reach Plex: %s", exc)
        return 1

    all_rows: list[dict] = []
    for section in sections:
        stype = section.get("type")
        key = section.get("key")
        name = section.get("title", key)
        if stype == "movie":
            log.info("Processing movie library: %s", name)
            all_rows.extend(gather_movies(client, key))
        elif stype == "show":
            log.info("Processing TV library: %s", name)
            all_rows.extend(gather_shows(client, key))
        else:
            log.info("Skipping section '%s' (type=%s)", name, stype)

    log.info("Total items before filters: %d", len(all_rows))

    candidates = [
        r for r in all_rows
        if r["size_gb"] >= args.min_size_gb and r["days_stale"] >= args.min_days_stale
    ]
    log.info(
        "After filters (>= %.1f GB, >= %d days stale): %d items",
        args.min_size_gb, args.min_days_stale, len(candidates),
    )

    candidates.sort(key=lambda r: r["score"], reverse=True)
    if args.top:
        candidates = candidates[: args.top]

    if args.output:
        write_csv(candidates, args.output)
    else:
        print_table(candidates)

    return 0


if __name__ == "__main__":
    sys.exit(main())
