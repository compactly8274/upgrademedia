import csv
import io
import json
import logging
import os
import re
import subprocess
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from app.clients import RadarrClient, SonarrClient, PlexClient
from app.config import settings
from app.db import db
from app import scanner as sc

log = logging.getLogger(__name__)
NEVER = 99999

# Pending auto-continue batch (set when auto_continue fires, cleared on new run or cancel)
_pending_batch: dict = {}

# Per-run cancellation events: run_id -> threading.Event
_cancel_flags: dict = {}


def _cancel_pending():
    t = _pending_batch.get("timer")
    if t:
        t.cancel()
    _pending_batch.clear()


def _register_cancel(run_id: int) -> threading.Event:
    ev = threading.Event()
    _cancel_flags[run_id] = ev
    return ev


def _clear_cancel(run_id: int):
    _cancel_flags.pop(run_id, None)


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
        "webhook_url": settings.webhook_url,
        "webhook_type": settings.webhook_type,
        "media_paths": settings.media_paths,
        "search_delay": settings.search_delay,
        "search_limit": settings.search_limit,
        "search_cooldown_days": settings.search_cooldown_days,
        "search_batch_gap": settings.search_batch_gap,
    }
    for row in rows:
        cfg[row["key"]] = row["value"]
    if overrides:
        cfg.update(overrides)
    return cfg


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def _notify(cfg: dict, title: str, body: str):
    url = str(cfg.get("webhook_url") or "").strip()
    if not url:
        return
    wtype = str(cfg.get("webhook_type") or "discord").lower().strip()
    try:
        if wtype == "discord":
            requests.post(url, json={"embeds": [{"title": title, "description": body, "color": 0x3b82f6}]}, timeout=10)
        elif wtype == "ntfy":
            requests.post(url, data=body.encode(), headers={"Title": title, "Priority": "default"}, timeout=10)
        elif wtype == "gotify":
            requests.post(url, json={"title": title, "message": body, "priority": 5}, timeout=10)
        else:
            requests.post(url, json={"title": title, "message": body}, timeout=10)
    except Exception as exc:
        log.warning("Notification failed: %s", exc)


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
    cancel = _register_cancel(run_id)
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

        if cancel.is_set():
            _finish_run(run_id, "cancelled", {}, "Cancelled by user")
        else:
            _finish_run(run_id, "success", {"scanned": len(rows), "candidates": len(candidates)})
            _notify(params, "Media Manager — Analyze complete",
                    f"Scanned {len(rows)} items, found {len(candidates)} removal candidates.")
    except Exception as exc:
        log.exception("Analyze task failed")
        _finish_run(run_id, "error", {}, str(exc))
        _notify(params, "Media Manager — Analyze failed", str(exc))
    finally:
        _clear_cancel(run_id)
    return run_id


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------

def run_upgrade(params: dict = None, csv_text: str = ""):
    if params is None:
        params = _effective_config()
    run_id = _start_run("upgrade", params)
    cancel = _register_cancel(run_id)
    logs = []
    try:
        if not csv_text.strip():
            _finish_run(run_id, "success", {"matched": 0, "triggered": 0}, "No CSV provided.")
            return run_id

        threshold = float(params.get("quality_threshold", settings.quality_threshold))

        # Strip Medialyze comment lines and blank lines before CSV parsing
        csv_lines = [l for l in csv_text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
        reader = csv.DictReader(io.StringIO("\n".join(csv_lines)))
        all_rows = [{k.strip().lower(): (v or "").strip() for k, v in row.items() if k is not None} for row in reader]

        logs.append(f"Parsed {len(all_rows)} rows from CSV (threshold <= {threshold})")
        if all_rows:
            sample_keys = list(all_rows[0].keys())
            logs.append(f"Columns: {', '.join(sample_keys[:8])}{'…' if len(sample_keys)>8 else ''}")

        # Real Medialyze CSV uses quality_score column; fall back to score for compatibility
        def _row_score(r):
            raw = r.get("quality_score") or r.get("score") or ""
            return _safe_float(raw)

        rows = [r for r in all_rows if _row_score(r) <= threshold]
        logs.append(f"Rows at or below threshold: {len(rows)}")

        # Medialyze CSV has no type column — detect from content_category, series_title, or path
        def _is_tv(r):
            cat = (r.get("content_category") or "").lower()
            if "movie" in cat:
                return False
            if any(w in cat for w in ("episode", "show", "series", "tv")):
                return True
            if r.get("series_title"):
                return True
            return "season" in (r.get("relative_path", "")).lower()

        movie_rows = [r for r in rows if not _is_tv(r)]
        ep_rows = [r for r in rows if _is_tv(r)]
        logs.append(f"Movies: {len(movie_rows)}, TV episodes: {len(ep_rows)}")
        dry_run = str(params.get("dry_run", "false")).lower() in ("true", "1", "yes")
        delay = float(params.get("delay", 0.5))
        matched = unmatched = triggered = 0

        if movie_rows and not params.get("radarr_api_key"):
            logs.append(f"Skipping {len(movie_rows)} movie row(s) — Radarr API key not configured")
        if movie_rows and params.get("radarr_api_key"):
            radarr = RadarrClient(str(params["radarr_url"]), str(params["radarr_api_key"]))
            all_movies = radarr.movies()
            title_lookup = {m.get("title", "").lower(): m for m in all_movies}
            # Index by file stem for filename-based matching
            file_stem_lookup = {}
            for m in all_movies:
                mf = m.get("movieFile") or {}
                stem = Path(mf.get("relativePath", "")).stem.lower()
                if stem:
                    file_stem_lookup[stem] = m
            logs.append(f"Radarr library: {len(all_movies)} movies, {len(file_stem_lookup)} with files")

            for row in movie_rows:
                if cancel.is_set(): break
                filename = row.get("filename", "")
                rel_path = row.get("relative_path", "")
                file_stem = Path(filename).stem.lower() if filename else ""
                rel_stem = Path(rel_path).stem.lower() if rel_path else ""
                score = row.get("quality_score") or row.get("score", "?")
                movie = (
                    file_stem_lookup.get(file_stem)
                    or file_stem_lookup.get(rel_stem)
                    or title_lookup.get(file_stem)
                    or title_lookup.get(rel_stem)
                )
                if not movie:
                    unmatched += 1
                    logs.append(f"No match: {filename or rel_path} (quality_score={score})")
                    continue
                matched += 1
                label = f"{movie['title']} ({movie.get('year', '?')})"
                if dry_run:
                    logs.append(f"[DRY-RUN] {label} (quality_score={score})")
                else:
                    try:
                        radarr.search(movie["id"])
                        triggered += 1
                        logs.append(f"Triggered: {label}")
                    except Exception as e:
                        logs.append(f"Error {label}: {e}")
                if delay:
                    time.sleep(delay)

        if ep_rows and not params.get("sonarr_api_key"):
            logs.append(f"Skipping {len(ep_rows)} TV row(s) — Sonarr API key not configured")
        if ep_rows and params.get("sonarr_api_key"):
            sonarr = SonarrClient(str(params["sonarr_url"]), str(params["sonarr_api_key"]))
            series_lib = {s.get("title", "").lower(): s for s in sonarr.series()}
            logs.append(f"Sonarr library: {len(series_lib)} series")
            for row in ep_rows:
                if cancel.is_set(): break
                # Medialyze provides series_title for TV content
                series_title = (row.get("series_title") or row.get("title", "")).lower()
                score = row.get("quality_score") or row.get("score", "?")
                series = series_lib.get(series_title)
                if not series:
                    unmatched += 1
                    logs.append(f"No match: {series_title} (quality_score={score})")
                    continue
                matched += 1
                if dry_run:
                    logs.append(f"[DRY-RUN] {series['title']} (quality_score={score})")
                else:
                    try:
                        sonarr.search_series(series["id"])
                        triggered += 1
                        logs.append(f"Triggered: {series['title']}")
                    except Exception as e:
                        logs.append(f"Error {series['title']}: {e}")
                if delay:
                    time.sleep(delay)

        if cancel.is_set():
            _finish_run(run_id, "cancelled",
                        {"matched": matched, "unmatched": unmatched, "triggered": triggered},
                        "\n".join(logs) + "\nCancelled by user")
        else:
            _finish_run(run_id, "success",
                        {"matched": matched, "unmatched": unmatched, "triggered": triggered, "dry_run": dry_run},
                        "\n".join(logs))
            suffix = " (dry run)" if dry_run else ""
            _notify(params, f"Media Manager — Upgrade complete{suffix}",
                    f"Matched {matched}, triggered {triggered}, unmatched {unmatched}.")
    except Exception as exc:
        log.exception("Upgrade task failed")
        _finish_run(run_id, "error", {}, str(exc))
        _notify(params, "Media Manager — Upgrade failed", str(exc))
    finally:
        _clear_cancel(run_id)
    return run_id


def _safe_float(v: str, default: float = 999.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


_SEASON_RE = re.compile(r'[Ss]eason\s*(\d{1,2})|[Ss](\d{1,2})[Ee]\d+')


def _parse_season(path: str):
    m = _SEASON_RE.search(path)
    if m:
        return int(m.group(1) or m.group(2))
    return None


_SCAN_INSERT_SQL = """
    INSERT INTO scan_files
      (path, filename, size_bytes, mtime, file_hash,
       duration_seconds, video_codec, width, height, video_bitrate_kbps,
       hdr_type, audio_codec, audio_channels,
       non_english_audio, non_english_subs, audio_langs, sub_langs,
       quality_score, score_breakdown, scanned_at, radarr_id, sonarr_id,
       season_number)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(path) DO UPDATE SET
      filename=excluded.filename, size_bytes=excluded.size_bytes,
      mtime=excluded.mtime, file_hash=excluded.file_hash,
      duration_seconds=excluded.duration_seconds,
      video_codec=excluded.video_codec, width=excluded.width, height=excluded.height,
      video_bitrate_kbps=excluded.video_bitrate_kbps, hdr_type=excluded.hdr_type,
      audio_codec=excluded.audio_codec, audio_channels=excluded.audio_channels,
      non_english_audio=excluded.non_english_audio,
      non_english_subs=excluded.non_english_subs,
      audio_langs=excluded.audio_langs, sub_langs=excluded.sub_langs,
      quality_score=excluded.quality_score, score_breakdown=excluded.score_breakdown,
      scanned_at=excluded.scanned_at,
      radarr_id=excluded.radarr_id, sonarr_id=excluded.sonarr_id,
      season_number=excluded.season_number
"""
SCAN_WORKERS = int(os.getenv("SCAN_WORKERS", "4"))
SCAN_BATCH = 20  # flush to DB and update progress every N completed files


def _scan_progress(run_id: int, summary: dict, logs: list):
    with db() as conn:
        conn.execute("UPDATE runs SET summary=?, log=? WHERE id=?",
                     (json.dumps(summary), "\n".join(logs), run_id))


# ---------------------------------------------------------------------------
# Bulk search (trigger Radarr/Sonarr for filtered scan results)
# ---------------------------------------------------------------------------

def run_search_all(params: dict, filters: dict):
    _cancel_pending()
    run_id = _start_run("search_all", params)
    cancel = _register_cancel(run_id)
    logs = []
    try:
        clauses = ["(radarr_id IS NOT NULL OR sonarr_id IS NOT NULL)"]
        args = []
        max_score = filters.get("max_score")
        codec = filters.get("codec")
        non_english = filters.get("non_english")
        force = bool(filters.get("force", False))
        season_upgrade = bool(filters.get("season_upgrade", False))
        auto_continue = bool(filters.get("auto_continue", False))
        cooldown_days = int(params.get("search_cooldown_days", settings.search_cooldown_days))
        batch_gap = int(params.get("search_batch_gap", settings.search_batch_gap))
        if max_score is not None:
            clauses.append("quality_score <= ?"); args.append(float(max_score))
        if codec:
            clauses.append("video_codec = ?"); args.append(str(codec))
        if non_english:
            clauses.append("(non_english_audio > 0 OR non_english_subs > 0)")
        # Cooldown: skip files searched within N days (always enforced when auto_continue to avoid loops)
        effective_cooldown = max(1, cooldown_days) if auto_continue else cooldown_days
        if effective_cooldown > 0:
            clauses.append(f"(last_searched_at IS NULL OR last_searched_at < datetime('now', '-{effective_cooldown} days'))")

        with db() as conn:
            files = [dict(r) for r in conn.execute(
                f"SELECT * FROM scan_files WHERE {' AND '.join(clauses)}", args
            ).fetchall()]

        delay = float(params.get("search_delay", settings.search_delay))
        limit = int(params.get("search_limit", settings.search_limit))
        if limit > 0:
            files = files[:limit]

        cooldown_label = f", cooldown={effective_cooldown}d" if effective_cooldown > 0 else ""
        logs.append(f"Found {len(files)} file(s) to process (delay={delay}s, force={force}, season_upgrade={season_upgrade}{cooldown_label})")
        if limit > 0:
            logs.append(f"Batch limit: {limit} files per run, {delay}s between each")
        _scan_progress(run_id, {"total": len(files), "triggered": 0, "skipped": 0, "errors": 0, "done": 0}, logs)

        radarr = RadarrClient(str(params["radarr_url"]), str(params["radarr_api_key"])) if params.get("radarr_api_key") else None
        sonarr = SonarrClient(str(params["sonarr_url"]), str(params["sonarr_api_key"])) if params.get("sonarr_api_key") else None
        triggered = skipped = errors = 0
        now = _now()

        def _mark_searched(file_ids: list):
            if not file_ids:
                return
            with db() as conn:
                conn.executemany(
                    "UPDATE scan_files SET last_searched_at=? WHERE id=?",
                    [(now, fid) for fid in file_ids]
                )

        if season_upgrade and sonarr:
            radarr_files = [f for f in files if f.get("radarr_id") and not f.get("sonarr_id")]
            sonarr_files = [f for f in files if f.get("sonarr_id")]
            other_files = [f for f in files if not f.get("radarr_id") and not f.get("sonarr_id")]

            season_groups = {}
            no_season = {}  # sid -> [all files for this series with no season number]
            for f in sonarr_files:
                sn = f.get("season_number")
                sid = int(f["sonarr_id"])
                if sn is not None:
                    season_groups.setdefault((sid, int(sn)), []).append(f)
                else:
                    no_season.setdefault(sid, []).append(f)

            work = (
                [('radarr', f) for f in radarr_files]
                + [('season', (key, grp)) for key, grp in season_groups.items()]
                + [('series', (sid, grp)) for sid, grp in no_season.items()]
                + [('skip', f) for f in other_files]
            )
            done = 0

            for i, (kind, item) in enumerate(work):
                if cancel.is_set(): done += 1; break
                ok = False
                if kind == 'radarr':
                    f = item
                    try:
                        if force:
                            movie = radarr.movie(int(f["radarr_id"]))
                            mf = (movie.get("movieFile") or {}) if movie else {}
                            if mf.get("id"):
                                radarr.delete_file(int(mf["id"]))
                                logs.append(f"Deleted file: {f['filename']}")
                        radarr.search(int(f["radarr_id"]))
                        _mark_searched([f["id"]])
                        triggered += 1; ok = True
                    except Exception as e:
                        errors += 1; logs.append(f"Error {f['filename']}: {e}")
                    done += 1
                elif kind == 'season':
                    (sid, sn), grp = item
                    try:
                        sonarr.search_season(sid, sn)
                        _mark_searched([f["id"] for f in grp])
                        triggered += 1; ok = True
                        logs.append(f"Season search: series_id={sid} season={sn} ({len(grp)} file(s))")
                    except Exception as e:
                        try:
                            sonarr.search_series(sid)
                            _mark_searched([f["id"] for f in grp])
                            triggered += 1; ok = True
                            logs.append(f"Season fallback→series: series_id={sid} season={sn} ({e})")
                        except Exception as e2:
                            errors += 1
                            logs.append(f"Error series_id={sid} season={sn}: {e2}")
                    done += len(grp)
                elif kind == 'series':
                    sid, grp = item
                    try:
                        sonarr.search_series(sid)
                        _mark_searched([f["id"] for f in grp])
                        triggered += 1; ok = True
                        logs.append(f"Series search: series_id={sid} ({len(grp)} file(s))")
                    except Exception as e:
                        errors += 1; logs.append(f"Error series_id={sid}: {e}")
                    done += len(grp)
                else:
                    skipped += 1
                    done += 1

                if ok and delay:
                    time.sleep(delay)
                if (i + 1) % 20 == 0 or i == len(work) - 1:
                    _scan_progress(run_id, {
                        "total": len(files), "triggered": triggered,
                        "skipped": skipped, "errors": errors, "done": done,
                    }, logs)
        else:
            # Deduplicate sonarr files by series_id so we fire one search per series
            sonarr_groups: dict = {}
            flat_work = []
            for f in files:
                if f.get("sonarr_id") and sonarr:
                    sonarr_groups.setdefault(int(f["sonarr_id"]), []).append(f)
                else:
                    flat_work.append(f)

            work_items = (
                [('radarr', f) for f in flat_work]
                + [('sonarr', (sid, grp)) for sid, grp in sonarr_groups.items()]
            )
            done = 0
            for i, (kind, item) in enumerate(work_items):
                if cancel.is_set(): break
                ok = False
                if kind == 'radarr':
                    f = item
                    if f.get("radarr_id") and radarr:
                        try:
                            if force:
                                movie = radarr.movie(int(f["radarr_id"]))
                                mf = (movie.get("movieFile") or {}) if movie else {}
                                if mf.get("id"):
                                    radarr.delete_file(int(mf["id"]))
                                    logs.append(f"Deleted file: {f['filename']}")
                            radarr.search(int(f["radarr_id"]))
                            _mark_searched([f["id"]])
                            triggered += 1; ok = True
                        except Exception as e:
                            errors += 1; logs.append(f"Error {f['filename']}: {e}")
                    else:
                        skipped += 1
                    done += 1
                else:  # sonarr
                    sid, grp = item
                    try:
                        sonarr.search_series(sid)
                        _mark_searched([f["id"] for f in grp])
                        triggered += 1; ok = True
                    except Exception as e:
                        errors += 1; logs.append(f"Error series_id={sid}: {e}")
                    done += len(grp)
                if ok and delay:
                    time.sleep(delay)
                if (i + 1) % 20 == 0 or i == len(work_items) - 1:
                    _scan_progress(run_id, {
                        "total": len(files), "triggered": triggered,
                        "skipped": skipped, "errors": errors, "done": done,
                    }, logs)

        summary = {"total": len(files), "triggered": triggered, "skipped": skipped, "errors": errors}
        logs.append(f"Done — triggered {triggered}, skipped {skipped} (unlinked), errors {errors}")

        # Auto-continue: schedule next batch if more files remain
        if auto_continue and triggered > 0:
            with db() as conn:
                remaining = conn.execute(
                    f"SELECT COUNT(*) FROM scan_files WHERE {' AND '.join(clauses)}", args
                ).fetchone()[0]
            if remaining > 0:
                gap_label = f"in {batch_gap // 60}m {batch_gap % 60}s" if batch_gap else "immediately"
                logs.append(f"Auto-continue: {remaining} file(s) remain, next batch {gap_label}")
                summary["auto_continue_remaining"] = remaining
                t = threading.Timer(batch_gap, run_search_all, args=(params, filters))
                t.daemon = True
                t.start()
                scheduled_at = (datetime.now(timezone.utc) + timedelta(seconds=batch_gap)).isoformat()
                _pending_batch.update(timer=t, scheduled_at=scheduled_at, remaining=remaining, gap=batch_gap)
            else:
                logs.append("Auto-continue: all files searched, no more batches needed")

        if cancel.is_set():
            logs.append("Cancelled by user")
            _finish_run(run_id, "cancelled", summary, "\n".join(logs))
        else:
            _finish_run(run_id, "success", summary, "\n".join(logs))
            _notify(params, "Media Manager — Bulk Search complete",
                    f"Triggered {triggered} upgrade searches.")
    except Exception as exc:
        log.exception("search_all task failed")
        _finish_run(run_id, "error", {}, str(exc))
        _notify(params, "Media Manager — Bulk Search failed", str(exc))
    finally:
        _clear_cancel(run_id)
    return run_id


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def _scan_worker(path: str, size_bytes: int, mtime: float, do_hash: bool,
                 radarr_lib: dict, sonarr_lib: dict, now: str):
    try:
        filename = os.path.basename(path)
        info = sc.scan_file(path, do_hash=do_hash)
        if info is None:
            return ('error', path, None)
        stem = Path(filename).stem.lower()
        radarr_id = radarr_lib.get(stem)
        sonarr_id = None
        if not radarr_id:
            path_lower = path.lower()
            for title, sid in sonarr_lib.items():
                if title in path_lower:
                    sonarr_id = sid
                    break
        season_number = _parse_season(path) if sonarr_id else None
        row = (
            path, filename, size_bytes, mtime, info["file_hash"],
            info["duration_seconds"], info["video_codec"], info["width"], info["height"],
            info["video_bitrate_kbps"], info["hdr_type"], info["audio_codec"],
            info["audio_channels"], info["non_english_audio"], info["non_english_subs"],
            info["audio_langs"], info["sub_langs"],
            info["quality_score"], info["score_breakdown"], now,
            radarr_id, sonarr_id, season_number,
        )
        return ('ok', path, row)
    except Exception as e:
        log.warning("Error scanning %s: %s", path, e)
        return ('error', path, str(e))


def run_scan(params: dict = None):
    if params is None:
        params = _effective_config()
    run_id = _start_run("scan", params)
    cancel = _register_cancel(run_id)
    logs = []
    try:
        raw_paths = str(params.get("media_paths") or "").strip()
        if not raw_paths:
            _finish_run(run_id, "error", {}, "MEDIA_PATHS not configured — set it in Settings or as an environment variable.")
            return run_id

        media_paths = [p.strip() for p in raw_paths.split(",") if p.strip()]
        logs.append(f"Scanning {len(media_paths)} path(s): {', '.join(media_paths)}")

        files = sc.walk_media_paths(media_paths)
        logs.append(f"Found {len(files)} video file(s)")

        # Pre-load existing records so skip check is a dict lookup, not 25k DB queries
        with db() as conn:
            existing = {
                row["path"]: (row["size_bytes"], row["mtime"])
                for row in conn.execute("SELECT path, size_bytes, mtime FROM scan_files").fetchall()
            }

        # Separate files into skip (unchanged) and to_scan (new or modified)
        to_scan = []   # list of (path, size_bytes, mtime)
        skipped = 0
        for path in files:
            try:
                st = os.stat(path)
                ex = existing.get(path)
                if ex and ex[0] == st.st_size and ex[1] == st.st_mtime:
                    skipped += 1
                else:
                    to_scan.append((path, st.st_size, st.st_mtime))
            except Exception as e:
                log.warning("stat failed %s: %s", path, e)

        logs.append(f"Skipping {skipped} unchanged file(s), scanning {len(to_scan)} new/modified")

        # Only hash files whose size is shared with another file (duplicate candidates)
        size_counts = Counter(size for _, size, _ in to_scan)

        _scan_progress(run_id, {"total": len(files), "scanned": 0, "skipped": skipped, "errors": 0, "done": skipped}, logs)

        radarr_lib = {}
        sonarr_lib = {}
        if params.get("radarr_api_key"):
            try:
                movies = RadarrClient(str(params["radarr_url"]), str(params["radarr_api_key"])).movies()
                for m in movies:
                    mf = m.get("movieFile") or {}
                    stem = Path(mf.get("relativePath", "")).stem.lower()
                    if stem:
                        radarr_lib[stem] = m["id"]
            except Exception as e:
                logs.append(f"Radarr lookup unavailable: {e}")
        if params.get("sonarr_api_key"):
            try:
                series = SonarrClient(str(params["sonarr_url"]), str(params["sonarr_api_key"])).series()
                sonarr_lib = {s["title"].lower(): s["id"] for s in series}
            except Exception as e:
                logs.append(f"Sonarr lookup unavailable: {e}")

        scanned = errors = 0
        done = skipped
        now = _now()
        batch = []

        workers = min(SCAN_WORKERS, len(to_scan)) if to_scan else 1
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _scan_worker,
                    path, size_bytes, mtime,
                    size_counts[size_bytes] > 1,
                    radarr_lib, sonarr_lib, now
                ): path
                for path, size_bytes, mtime in to_scan
            }

            for future in as_completed(futures):
                if cancel.is_set():
                    break
                done += 1
                status, path, result = future.result()
                if status == 'ok':
                    batch.append(result)
                    scanned += 1
                else:
                    errors += 1
                    if result:
                        logs.append(f"Error: {os.path.basename(path)}: {result}")

                if len(batch) >= SCAN_BATCH or done == len(files):
                    if batch:
                        with db() as conn:
                            for row in batch:
                                conn.execute(_SCAN_INSERT_SQL, row)
                        batch = []
                    _scan_progress(run_id, {
                        "total": len(files), "scanned": scanned,
                        "skipped": skipped, "errors": errors, "done": done,
                    }, logs)

        # Remove DB records for files that no longer exist on disk
        removed = 0
        if files:
            scanned_paths = set(files)
            with db() as conn:
                all_db_paths = [row[0] for row in conn.execute("SELECT path FROM scan_files").fetchall()]
            stale = [p for p in all_db_paths if p not in scanned_paths]
            if stale:
                with db() as conn:
                    conn.executemany("DELETE FROM scan_files WHERE path=?", [(p,) for p in stale])
                removed = len(stale)
                logs.append(f"Removed {removed} record(s) for files no longer on disk")

        summary = {"total": len(files), "scanned": scanned, "skipped": skipped, "removed": removed, "errors": errors}
        if cancel.is_set():
            logs.append("Cancelled by user")
            _finish_run(run_id, "cancelled", summary, "\n".join(logs))
        else:
            logs.append(f"Done — scanned {scanned}, skipped {skipped} (unchanged), {removed} removed, {errors} errors")
            _finish_run(run_id, "success", summary, "\n".join(logs))
            _notify(params, "Media Manager — Scan complete",
                    f"Scanned {scanned} files, {skipped} unchanged, {removed} removed, {errors} errors.")
    except Exception as exc:
        log.exception("Scan task failed")
        _finish_run(run_id, "error", {}, str(exc))
        _notify(params, "Media Manager — Scan failed", str(exc))
    finally:
        _clear_cancel(run_id)
    return run_id


# ---------------------------------------------------------------------------
# Strip non-English streams
# ---------------------------------------------------------------------------

def run_strip(file_ids: list, params: dict = None):
    if params is None:
        params = _effective_config()
    run_id = _start_run("strip", params)
    cancel = _register_cancel(run_id)
    logs = []
    try:
        if not file_ids:
            _finish_run(run_id, "success", {"stripped": 0}, "No files selected.")
            return run_id

        with db() as conn:
            rows = conn.execute(
                f"SELECT * FROM scan_files WHERE id IN ({','.join('?' for _ in file_ids)})",
                file_ids
            ).fetchall()

        logs.append(f"Stripping non-English streams from {len(rows)} file(s)")
        stripped = skipped = errors = 0

        for row in rows:
            if cancel.is_set(): break
            row = dict(row)
            path = row["path"]
            filename = row["filename"]
            if not os.path.isfile(path):
                logs.append(f"Missing: {filename}")
                errors += 1
                continue

            probe = sc.probe_file(path)
            if not probe:
                logs.append(f"Cannot probe: {filename}")
                errors += 1
                continue

            cmd = sc.build_strip_command(path, probe)
            if cmd is None:
                logs.append(f"Nothing to strip: {filename}")
                skipped += 1
                continue

            tmp = path + '.stripping.mkv'
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
                if result.returncode != 0:
                    logs.append(f"ffmpeg error {filename}: {result.stderr[-200:]}")
                    errors += 1
                    if os.path.exists(tmp):
                        os.remove(tmp)
                    continue

                os.replace(tmp, path)
                now = _now()
                with db() as conn:
                    conn.execute(
                        "UPDATE scan_files SET stripped_at=?, non_english_audio=0, non_english_subs=0 WHERE id=?",
                        (now, row["id"])
                    )
                stripped += 1
                logs.append(f"Stripped: {filename}")
            except subprocess.TimeoutExpired:
                logs.append(f"Timeout: {filename}")
                errors += 1
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception as e:
                logs.append(f"Error {filename}: {e}")
                errors += 1
                if os.path.exists(tmp):
                    try: os.remove(tmp)
                    except: pass

        summary = {"stripped": stripped, "skipped": skipped, "errors": errors}
        if cancel.is_set():
            logs.append("Cancelled by user")
            _finish_run(run_id, "cancelled", summary, "\n".join(logs))
        else:
            logs.append(f"Done — stripped {stripped}, skipped {skipped} (already clean), errors {errors}")
            _finish_run(run_id, "success", summary, "\n".join(logs))
            _notify(params, "Media Manager — Strip complete",
                    f"Stripped {stripped} files, {skipped} already clean, {errors} errors.")
    except Exception as exc:
        log.exception("Strip task failed")
        _finish_run(run_id, "error", {}, str(exc))
        _notify(params, "Media Manager — Strip failed", str(exc))
    finally:
        _clear_cancel(run_id)
    return run_id
