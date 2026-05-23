import csv
import io
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from app.clients import RadarrClient, SonarrClient, PlexClient
from app.config import settings
from app.db import db

log = logging.getLogger(__name__)
NEVER = 99999


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _start_run(type_: str, params: dict) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO runs (type, started_at, status, params) VALUES (?,?,?,?)",
            (type_, _now(), "running", json.dumps(params, default=str))
        )
        return cur.lastrowid


def _finish_run(run_id: int, status: str, summary: dict, log_text: str = ""):
    with db() as conn:
        conn.execute(
            "UPDATE runs SET finished_at=?, status=?, summary=?, log=? WHERE id=?",
            (_now(), status, json.dumps(summary), log_text, run_id)
        )


def _effective_config(overrides: dict = None) -> dict:
    with db() as conn:
        rows = conn.execute("SELECT key, value FROM config").fetchall()
    cfg = {
        "radarr_url": settings.radarr_url,
        "radarr_api_key": settings.radarr_api_key,
        "sonarr_url": settings.sonarr_url,
        "sonarr_api_key": settings.sonarr_api_key,
        "plex_url": settings.plex_url,
        "plex_token": settings.plex_token,
        "quality_threshold": settings.quality_threshold,
        "min_size_gb": settings.min_size_gb,
        "min_days_stale": settings.min_days_stale,
    }
    for row in rows:
        cfg[row["key"]] = row["value"]
    if overrides:
        cfg.update(overrides)
    return cfg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _file_size(item: dict) -> int:
    return sum(p.get("size", 0) for m in item.get("Media", []) for p in m.get("Part", []))


def _days(ts) -> int:
    if not ts:
        return NEVER
    last = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - last).days)


def _date(ts) -> str:
    if not ts:
        return "never"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Analyze
# ---------------------------------------------------------------------------

def run_analyze(params: dict = None):
    if params is None:
        params = _effective_config()
    run_id = _start_run("analyze", params)
    try:
        plex_token = params.get("plex_token", "")
        if not plex_token:
            _finish_run(run_id, "error", {}, "Plex token not configured")
            return run_id

        plex = PlexClient(str(params.get("plex_url", settings.plex_url)), str(plex_token))

        radarr_lib = {}
        if params.get("radarr_api_key"):
            try:
                movies = RadarrClient(str(params["radarr_url"]), str(params["radarr_api_key"])).movies()
                radarr_lib = {m["title"].lower(): m for m in movies}
            except Exception as e:
                log.warning("Radarr unavailable, skipping ID lookup: %s", e)

        sonarr_lib = {}
        if params.get("sonarr_api_key"):
            try:
                series = SonarrClient(str(params["sonarr_url"]), str(params["sonarr_api_key"])).series()
                sonarr_lib = {s["title"].lower(): s for s in series}
            except Exception as e:
                log.warning("Sonarr unavailable, skipping ID lookup: %s", e)

        sections = plex.sections()
        rows = []

        for section in sections:
            stype = section.get("type")
            key = section.get("key")
            if stype == "movie":
                for item in plex.all_items(key, 1):
                    size_gb = _file_size(item) / 1024 ** 3
                    ts = item.get("lastViewedAt")
                    title = item.get("title", "")
                    rows.append({
                        "media_type": "movie", "title": title,
                        "year": str(item.get("year", "")),
                        "size_gb": round(size_gb, 2),
                        "last_watched": _date(ts), "days_stale": _days(ts),
                        "score": round(size_gb * _days(ts), 1),
                        "radarr_id": radarr_lib.get(title.lower(), {}).get("id"),
                        "sonarr_id": None,
                        "plex_key": item.get("ratingKey"),
                    })
            elif stype == "show":
                shows: dict = {}
                for ep in plex.all_items(key, 4):
                    k = ep.get("grandparentRatingKey") or ep.get("grandparentTitle", "?")
                    if k not in shows:
                        shows[k] = {
                            "title": ep.get("grandparentTitle", "?"),
                            "plex_key": ep.get("grandparentRatingKey"),
                            "size_bytes": 0, "latest_ts": None,
                        }
                    shows[k]["size_bytes"] += _file_size(ep)
                    ts = ep.get("lastViewedAt")
                    if ts:
                        ts = int(ts)
                        if shows[k]["latest_ts"] is None or ts > shows[k]["latest_ts"]:
                            shows[k]["latest_ts"] = ts
                for data in shows.values():
                    size_gb = data["size_bytes"] / 1024 ** 3
                    ts = data["latest_ts"]
                    title = data["title"]
                    rows.append({
                        "media_type": "show", "title": title, "year": "",
                        "size_gb": round(size_gb, 2),
                        "last_watched": _date(ts), "days_stale": _days(ts),
                        "score": round(size_gb * _days(ts), 1),
                        "radarr_id": None,
                        "sonarr_id": sonarr_lib.get(title.lower(), {}).get("id"),
                        "plex_key": data["plex_key"],
                    })

        min_size = float(params.get("min_size_gb", settings.min_size_gb))
        min_days = int(params.get("min_days_stale", settings.min_days_stale))
        candidates = [r for r in rows if r["size_gb"] >= min_size and r["days_stale"] >= min_days]
        candidates.sort(key=lambda r: r["score"], reverse=True)

        with db() as conn:
            for r in candidates:
                conn.execute(
                    "INSERT INTO candidates "
                    "(run_id,media_type,title,year,size_gb,last_watched,days_stale,score,radarr_id,sonarr_id,plex_key) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, r["media_type"], r["title"], r["year"], r["size_gb"],
                     r["last_watched"], r["days_stale"], r["score"],
                     r["radarr_id"], r["sonarr_id"], r["plex_key"])
                )

        _finish_run(run_id, "success", {"scanned": len(rows), "candidates": len(candidates)})
    except Exception as exc:
        log.exception("Analyze task failed")
        _finish_run(run_id, "error", {}, str(exc))
    return run_id


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------

def run_upgrade(params: dict = None, csv_text: str = ""):
    if params is None:
        params = _effective_config()
    run_id = _start_run("upgrade", params)
    logs = []
    try:
        if not csv_text.strip():
            _finish_run(run_id, "success", {"matched": 0, "triggered": 0}, "No CSV provided.")
            return run_id

        threshold = float(params.get("quality_threshold", settings.quality_threshold))
        reader = csv.DictReader(io.StringIO(csv_text))
        all_rows = [{k.strip().lower(): (v or "").strip() for k, v in row.items()} for row in reader]
        rows = [r for r in all_rows if _safe_float(r.get("score", "999")) < threshold]

        movie_rows = [r for r in rows if r.get("type", "").lower() == "movie"]
        ep_rows = [r for r in rows if r.get("type", "").lower() in ("episode", "series", "show", "tv")]
        dry_run = str(params.get("dry_run", "false")).lower() in ("true", "1", "yes")
        delay = float(params.get("delay", 0.5))
        matched = unmatched = triggered = 0

        if movie_rows and params.get("radarr_api_key"):
            radarr = RadarrClient(str(params["radarr_url"]), str(params["radarr_api_key"]))
            library = {m.get("title", "").lower(): m for m in radarr.movies()}
            for row in movie_rows:
                movie = library.get(row.get("title", "").lower())
                if not movie:
                    unmatched += 1
                    logs.append(f"No match: {row.get('title')} (score={row.get('score')})")
                    continue
                matched += 1
                label = f"{movie['title']} ({movie.get('year', '?')})"
                if dry_run:
                    logs.append(f"[DRY-RUN] {label}")
                else:
                    try:
                        radarr.search(movie["id"])
                        triggered += 1
                        logs.append(f"Triggered: {label}")
                    except Exception as e:
                        logs.append(f"Error {label}: {e}")
                if delay:
                    time.sleep(delay)

        if ep_rows and params.get("sonarr_api_key"):
            sonarr = SonarrClient(str(params["sonarr_url"]), str(params["sonarr_api_key"]))
            series_lib = {s.get("title", "").lower(): s for s in sonarr.series()}
            for row in ep_rows:
                series = series_lib.get(row.get("title", "").lower())
                if not series:
                    unmatched += 1
                    logs.append(f"No match: {row.get('title')}")
                    continue
                matched += 1
                if dry_run:
                    logs.append(f"[DRY-RUN] {series['title']}")
                else:
                    try:
                        sonarr.search_series(series["id"])
                        triggered += 1
                        logs.append(f"Triggered: {series['title']}")
                    except Exception as e:
                        logs.append(f"Error {series['title']}: {e}")
                if delay:
                    time.sleep(delay)

        _finish_run(run_id, "success",
                    {"matched": matched, "unmatched": unmatched, "triggered": triggered, "dry_run": dry_run},
                    "\n".join(logs))
    except Exception as exc:
        log.exception("Upgrade task failed")
        _finish_run(run_id, "error", {}, str(exc))
    return run_id


def _safe_float(v: str, default: float = 999.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default
