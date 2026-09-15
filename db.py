"""SQLite history and complete persisted analysis responses for reproducible replay."""
import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def database_path():
    path = Path(os.getenv("DATABASE_PATH", "sessions.db"))
    return path if path.is_absolute() else ROOT / path


def _connect():
    conn = sqlite3.connect(database_path(), timeout=10)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    except BaseException:
        conn.close()
        raise


@contextmanager
def _transaction():
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db():
    with _transaction() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, created_at TIMESTAMP,
                resume_hash TEXT, jd_hash TEXT, match_score INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS suggestions (
                id TEXT PRIMARY KEY, session_id TEXT REFERENCES sessions(id),
                suggestion_text TEXT, missing_skill TEXT,
                rating TEXT CHECK (rating IN ('up', 'down')), created_at TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS suggestions_session_idx ON suggestions(session_id)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS analysis_results (
                cache_key TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                response_json TEXT NOT NULL
            )
        """)


def get_cached_analysis(cache_key):
    with _transaction() as conn:
        row = conn.execute("SELECT response_json FROM analysis_results WHERE cache_key=?", (cache_key,)).fetchone()
        return json.loads(row[0]) if row else None


def get_report(session_id):
    with _transaction() as conn:
        row = conn.execute("SELECT response_json FROM analysis_results WHERE session_id=?", (session_id,)).fetchone()
        return json.loads(row[0]) if row else None


def save_canonical_analysis(cache_key, response):
    """Atomically publish one result per identity, even if two workers compute it.

    A losing worker returns the winner's full response, never its own new scores.
    No database transaction is held open during model calls.
    """
    with _transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT response_json FROM analysis_results WHERE cache_key=?", (cache_key,)).fetchone()
        if row:
            return json.loads(row[0])
        saved = dict(response)
        data = saved["data"]
        _insert_session(conn, saved["session_id"], data.get("raw_normalized_text", ""),
                        data.get("job_description", ""), saved["match_score"])
        saved["gaps"] = _insert_suggestions(conn, saved["session_id"], saved["gaps"])
        serialized = json.dumps(saved, ensure_ascii=False, allow_nan=False)
        conn.execute("INSERT INTO analysis_results (cache_key, session_id, response_json) VALUES (?, ?, ?)",
                     (cache_key, saved["session_id"], serialized))
        return json.loads(serialized)


def hash_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None


def _insert_session(conn, session_id, resume_text, jd_text, match_score):
    conn.execute(
        "INSERT INTO sessions (id, created_at, resume_hash, jd_hash, match_score) VALUES (?, ?, ?, ?, ?)",
        (session_id, datetime.now(timezone.utc).isoformat(), hash_text(resume_text), hash_text(jd_text), match_score),
    )


def _insert_suggestions(conn, session_id, suggestions):
    saved = [dict(s, id=str(uuid.uuid4())) for s in suggestions]
    conn.executemany(
        "INSERT INTO suggestions (id, session_id, suggestion_text, missing_skill, rating, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(s["id"], session_id, s["suggestion"], s["missing_skill"], None,
          datetime.now(timezone.utc).isoformat()) for s in saved],
    )
    return saved


def save_analysis(session_id, resume_text, jd_text, match_score, suggestions):
    """Commit the session and suggestions together; return persisted suggestion IDs."""
    with _transaction() as conn:
        _insert_session(conn, session_id, resume_text, jd_text, match_score)
        return _insert_suggestions(conn, session_id, suggestions)


def save_session(session_id, resume_text, jd_text, match_score):
    with _transaction() as conn:
        _insert_session(conn, session_id, resume_text, jd_text, match_score)


def save_suggestions(session_id, suggestions):
    with _transaction() as conn:
        return _insert_suggestions(conn, session_id, suggestions)
