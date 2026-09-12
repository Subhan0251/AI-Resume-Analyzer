"""
db.py
=====
Lightweight SQLite persistence for session and gap-suggestion history
(point 6). Stores hashes, not raw resume/JD text, so this table can't leak
candidate PII on its own.

Design notes:
- SQLite is a reasonable choice right now: single file, zero ops overhead,
  fine for the current load (one resume vs one JD per request). It's a
  single-writer store though - if write volume grows a lot (e.g. once the
  planned mock-interview agents start writing here too, from multiple
  workers), this should move to Postgres. Nothing else here needs to change
  for that migration except the connection layer.
- Every function here is blocking (sqlite3 is synchronous by design).
  Callers must run these through run_in_executor, exactly like the
  parsing/matching calls, so they don't block the event loop - see app.py.
- A new connection is opened per call rather than sharing one across
  threads, since sqlite3 connections aren't safe to share across threads by
  default and these calls run inside a thread-pool executor.
"""
import sqlite3
import hashlib
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

DB_PATH = "sessions.db"


def _connect() -> sqlite3.Connection:
    """Open a short-lived connection for one call. WAL mode lets reads proceed without waiting on an in-flight write."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db() -> None:
    """Creates the sessions/suggestions tables if they don't already exist. Call once at app startup - safe to call repeatedly."""
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                created_at TIMESTAMP,
                resume_hash TEXT,
                jd_hash TEXT,
                match_score INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS suggestions (
                id TEXT PRIMARY KEY,
                session_id TEXT REFERENCES sessions(id),
                suggestion_text TEXT,
                missing_skill TEXT,
                rating TEXT CHECK (rating IN ('up','down', NULL)),
                created_at TIMESTAMP
            )
        """)


def hash_text(text: Optional[str]) -> Optional[str]:
    """SHA-256 hash of resume/JD text. Returns None for empty input so an absent JD stores as NULL, not a hash of an empty string."""
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def save_session(session_id: str, resume_text: str, jd_text: str, match_score: Optional[int]) -> None:
    """Inserts one row per processed request: which resume (by hash) was matched against which JD (by hash, if any), and the resulting score (if any)."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sessions (id, created_at, resume_hash, jd_hash, match_score) VALUES (?, ?, ?, ?, ?)",
            (
                session_id,
                datetime.now(timezone.utc).isoformat(),
                hash_text(resume_text),
                hash_text(jd_text),
                match_score,
            ),
        )


def save_suggestions(session_id: str, suggestions: List[Dict[str, Any]]) -> None:
    """Inserts one row per gap suggestion generated for this session, each with its own id, so a suggestion can later be looked up and rated (thumbs up/down)."""
    if not suggestions:
        return
    with _connect() as conn:
        conn.executemany(
            "INSERT INTO suggestions (id, session_id, suggestion_text, missing_skill, rating, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    str(uuid.uuid4()),
                    session_id,
                    s.get("suggestion", ""),
                    s.get("missing_skill", ""),
                    None,
                    datetime.now(timezone.utc).isoformat(),
                )
                for s in suggestions
            ],
        )