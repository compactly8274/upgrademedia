import os
import sqlite3
from contextlib import contextmanager
from app.config import settings


@contextmanager
def db():
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    db_dir = os.path.dirname(settings.db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL DEFAULT 'running',
                params TEXT,
                summary TEXT,
                log TEXT
            );
            CREATE TABLE IF NOT EXISTS candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                media_type TEXT,
                title TEXT,
                year TEXT,
                size_gb REAL,
                last_watched TEXT,
                days_stale INTEGER,
                score REAL,
                radarr_id INTEGER,
                sonarr_id INTEGER,
                plex_key TEXT,
                deleted_at TEXT
            );
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_type TEXT NOT NULL,
                cron TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1
            );
        """)
