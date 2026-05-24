import json
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles

from app import scheduler as sched
from app.clients import RadarrClient, SonarrClient
from app.config import settings
from app.db import db, init_db
from app import tasks


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    sched.start()
    yield
    sched.stop()


app = FastAPI(title="Media Manager", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

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
    }
    for row in rows:
        cfg[row["key"]] = row["value"]
    if overrides:
        cfg.update(overrides)
    return cfg


@app.get("/api/config")
def get_config():
    return _effective_config()


@app.post("/api/config")
def save_config(body: dict):
    with db() as conn:
        for key, value in body.items():
            conn.execute(
                "INSERT INTO config (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value))
            )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

@app.get("/api/runs")
def list_runs(limit: int = 100):
    with db() as conn:
        rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/runs/{run_id}")
def get_run(run_id: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    return dict(row)


@app.post("/api/runs/analyze")
def trigger_analyze(body: dict = {}):
    params = _effective_config(body)
    threading.Thread(target=tasks.run_analyze, args=(params,), daemon=True).start()
    return {"queued": True}


@app.post("/api/runs/upgrade")
async def trigger_upgrade(
    csv_file: UploadFile = File(None),
    dry_run: str = Form("false"),
    threshold: float = Form(None),
):
    csv_text = ""
    if csv_file and csv_file.filename:
        csv_text = (await csv_file.read()).decode("utf-8-sig")
    params = _effective_config({"dry_run": dry_run})
    if threshold is not None:
        params["quality_threshold"] = threshold
    threading.Thread(target=tasks.run_upgrade, args=(params, csv_text), daemon=True).start()
    return {"queued": True}


# ---------------------------------------------------------------------------
# Candidates (from analyze runs)
# ---------------------------------------------------------------------------

@app.get("/api/candidates")
def list_candidates():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM candidates "
            "WHERE run_id=(SELECT id FROM runs WHERE type='analyze' ORDER BY id DESC LIMIT 1) "
            "AND deleted_at IS NULL ORDER BY score DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@app.delete("/api/candidates/{candidate_id}")
def delete_candidate(candidate_id: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    row = dict(row)
    cfg = _effective_config()
    errors = []

    if row.get("radarr_id") and cfg.get("radarr_api_key"):
        try:
            RadarrClient(cfg["radarr_url"], cfg["radarr_api_key"]).delete(int(row["radarr_id"]))
        except Exception as e:
            errors.append(f"Radarr: {e}")

    if row.get("sonarr_id") and cfg.get("sonarr_api_key"):
        try:
            SonarrClient(cfg["sonarr_url"], cfg["sonarr_api_key"]).delete(int(row["sonarr_id"]))
        except Exception as e:
            errors.append(f"Sonarr: {e}")

    if errors:
        raise HTTPException(500, detail="; ".join(errors))

    with db() as conn:
        conn.execute(
            "UPDATE candidates SET deleted_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), candidate_id)
        )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Library browser (live from Radarr / Sonarr)
# ---------------------------------------------------------------------------

@app.get("/api/library/movies")
def library_movies():
    cfg = _effective_config()
    if not cfg.get("radarr_api_key"):
        raise HTTPException(400, "Radarr API key not configured")
    movies = RadarrClient(cfg["radarr_url"], cfg["radarr_api_key"]).movies()
    return [
        {
            "id": m["id"],
            "title": m.get("title", ""),
            "year": m.get("year"),
            "size_gb": round((m.get("movieFile") or {}).get("size", 0) / 1024 ** 3, 2),
            "quality": (
                ((m.get("movieFile") or {}).get("quality") or {}).get("quality", {}).get("name", "")
                if m.get("hasFile") else "missing"
            ),
            "has_file": m.get("hasFile", False),
            "monitored": m.get("monitored", True),
        }
        for m in movies
    ]


@app.delete("/api/library/movies/{movie_id}")
def delete_library_movie(movie_id: int):
    cfg = _effective_config()
    if not cfg.get("radarr_api_key"):
        raise HTTPException(400, "Radarr API key not configured")
    try:
        RadarrClient(cfg["radarr_url"], cfg["radarr_api_key"]).delete(movie_id)
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"ok": True}


@app.get("/api/library/series")
def library_series():
    cfg = _effective_config()
    if not cfg.get("sonarr_api_key"):
        raise HTTPException(400, "Sonarr API key not configured")
    series_list = SonarrClient(cfg["sonarr_url"], cfg["sonarr_api_key"]).series()
    return [
        {
            "id": s["id"],
            "title": s.get("title", ""),
            "year": s.get("year"),
            "size_gb": round(s.get("statistics", {}).get("sizeOnDisk", 0) / 1024 ** 3, 2),
            "episode_count": s.get("statistics", {}).get("episodeFileCount", 0),
            "episode_total": s.get("statistics", {}).get("totalEpisodeCount", 0),
            "status": s.get("status", ""),
            "monitored": s.get("monitored", True),
        }
        for s in series_list
    ]


@app.delete("/api/library/series/{series_id}")
def delete_library_series(series_id: int):
    cfg = _effective_config()
    if not cfg.get("sonarr_api_key"):
        raise HTTPException(400, "Sonarr API key not configured")
    try:
        SonarrClient(cfg["sonarr_url"], cfg["sonarr_api_key"]).delete(series_id)
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"ok": True}


@app.post("/api/library/movies/{movie_id}/search")
def search_library_movie(movie_id: int):
    cfg = _effective_config()
    if not cfg.get("radarr_api_key"):
        raise HTTPException(400, "Radarr API key not configured")
    try:
        RadarrClient(cfg["radarr_url"], cfg["radarr_api_key"]).search(movie_id)
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"ok": True}


@app.post("/api/library/series/{series_id}/search")
def search_library_series(series_id: int):
    cfg = _effective_config()
    if not cfg.get("sonarr_api_key"):
        raise HTTPException(400, "Sonarr API key not configured")
    try:
        SonarrClient(cfg["sonarr_url"], cfg["sonarr_api_key"]).search_series(series_id)
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"ok": True}


@app.get("/api/library/wanted")
def library_wanted():
    cfg = _effective_config()
    out = []
    if cfg.get("radarr_api_key"):
        try:
            for m in RadarrClient(cfg["radarr_url"], cfg["radarr_api_key"]).movies():
                if m.get("monitored") and not m.get("hasFile"):
                    out.append({"id": m["id"], "type": "movie", "title": m.get("title", ""),
                                "year": m.get("year"), "status": m.get("status", ""), "missing_episodes": None})
        except Exception:
            pass
    if cfg.get("sonarr_api_key"):
        try:
            for s in SonarrClient(cfg["sonarr_url"], cfg["sonarr_api_key"]).series():
                if not s.get("monitored"):
                    continue
                stats = s.get("statistics") or {}
                missing = max(0, stats.get("totalEpisodeCount", 0) - stats.get("episodeFileCount", 0))
                if missing > 0:
                    out.append({"id": s["id"], "type": "series", "title": s.get("title", ""),
                                "year": s.get("year"), "status": s.get("status", ""), "missing_episodes": missing})
        except Exception:
            pass
    out.sort(key=lambda x: x.get("title", "").lower())
    return out


@app.get("/api/stats")
def get_stats():
    cfg = _effective_config()
    result = {
        "movies": {"count": 0, "size_gb": 0.0, "missing": 0, "by_quality": {}},
        "series": {"count": 0, "size_gb": 0.0, "missing_episodes": 0, "by_status": {}},
        "disk": [],
    }
    if cfg.get("radarr_api_key"):
        try:
            radarr = RadarrClient(cfg["radarr_url"], cfg["radarr_api_key"])
            movies = radarr.movies()
            result["movies"]["count"] = len(movies)
            result["movies"]["missing"] = sum(1 for m in movies if m.get("monitored") and not m.get("hasFile"))
            total = 0
            by_q: dict = {}
            for m in movies:
                mf = m.get("movieFile") or {}
                sz = mf.get("size", 0)
                total += sz
                label = (((mf.get("quality") or {}).get("quality") or {}).get("name")
                         or ("No File" if not m.get("hasFile") else "Unknown"))
                by_q[label] = by_q.get(label, 0) + sz
            result["movies"]["size_gb"] = round(total / 1024 ** 3, 1)
            result["movies"]["by_quality"] = {
                k: round(v / 1024 ** 3, 1)
                for k, v in sorted(by_q.items(), key=lambda x: -x[1])
            }
            try:
                disk = radarr._get("/api/v3/diskspace")
                result["disk"] = [
                    {"path": d.get("path", ""),
                     "free_gb": round(d.get("freeSpace", 0) / 1024 ** 3, 1),
                     "total_gb": round(d.get("totalSpace", 0) / 1024 ** 3, 1)}
                    for d in disk
                ]
            except Exception:
                pass
        except Exception as e:
            result["movies"]["error"] = str(e)
    if cfg.get("sonarr_api_key"):
        try:
            series_list = SonarrClient(cfg["sonarr_url"], cfg["sonarr_api_key"]).series()
            result["series"]["count"] = len(series_list)
            total = 0
            missing_eps = 0
            by_status: dict = {}
            for s in series_list:
                st = s.get("statistics") or {}
                total += st.get("sizeOnDisk", 0)
                missing_eps += max(0, st.get("totalEpisodeCount", 0) - st.get("episodeFileCount", 0))
                status = s.get("status", "unknown")
                by_status[status] = by_status.get(status, 0) + 1
            result["series"]["size_gb"] = round(total / 1024 ** 3, 1)
            result["series"]["missing_episodes"] = missing_eps
            result["series"]["by_status"] = by_status
        except Exception as e:
            result["series"]["error"] = str(e)
    return result


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------

@app.get("/api/schedules")
def list_schedules():
    with db() as conn:
        rows = conn.execute("SELECT * FROM schedules").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/schedules")
def create_schedule(body: dict):
    if body.get("job_type") not in ("analyze", "upgrade") or not body.get("cron"):
        raise HTTPException(400, "job_type and cron required")
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO schedules (job_type, cron, enabled) VALUES (?,?,1)",
            (body["job_type"], body["cron"])
        )
        schedule_id = cur.lastrowid
    sched.load_schedules()
    return {"id": schedule_id}


@app.put("/api/schedules/{schedule_id}")
def update_schedule(schedule_id: int, body: dict):
    with db() as conn:
        row = conn.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        conn.execute(
            "UPDATE schedules SET job_type=?, cron=?, enabled=? WHERE id=?",
            (
                body.get("job_type", row["job_type"]),
                body.get("cron", row["cron"]),
                1 if body.get("enabled", bool(row["enabled"])) else 0,
                schedule_id,
            )
        )
    sched.load_schedules()
    return {"ok": True}


@app.delete("/api/schedules/{schedule_id}")
def delete_schedule(schedule_id: int):
    with db() as conn:
        conn.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
    sched.load_schedules()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Static files — must be last
# ---------------------------------------------------------------------------
app.mount("/", StaticFiles(directory="static", html=True), name="static")
