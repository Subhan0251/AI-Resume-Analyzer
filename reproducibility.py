"""Stable identities for persisted comparisons; no credentials enter the cache key."""
import hashlib
import json
import os
import threading
from functools import lru_cache
from pathlib import Path

from errors import InputLimitError, InvalidDocumentError, AUTHENTIC_DOCUMENT_MESSAGE

_LOCKS = [threading.Lock() for _ in range(64)]


@lru_cache(maxsize=1)
def analysis_version():
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in ("app.py", "db.py", "parser.py", "matcher.py", "scorecard.py", "jd.py", "skills.py", "config.py", "reproducibility.py", "requirements.txt"):
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def comparison_key(stream, extension, jd, max_bytes):
    digest = hashlib.sha256()
    size = 0
    stream.seek(0)
    try:
        while chunk := stream.read(64 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise InputLimitError("Resume file exceeds the 10 MB limit.")
            digest.update(chunk)
    finally:
        stream.seek(0)
    if not size:
        raise InvalidDocumentError(AUTHENTIC_DOCUMENT_MESSAGE)
    identity = {
        "file_sha256": digest.hexdigest(), "extension": extension.lower(),
        "jd": jd.strip().replace("\r\n", "\n"), "version": analysis_version(),
        "model": os.getenv("RESUME_PARSER_MODEL", ""),
        "reasoning": os.getenv("REASONING_EFFORT", ""),
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def comparison_lock(key):
    # Bound lock storage while preventing duplicate computation for the same pair
    # within a process. The database chooses a single winner across processes.
    return _LOCKS[int(key[:8], 16) % len(_LOCKS)]
