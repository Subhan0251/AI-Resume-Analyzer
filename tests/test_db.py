import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.tmp = tempfile.TemporaryDirectory(dir=root)
        path = Path(self.tmp.name).resolve()
        assert path.is_relative_to(root)
        self.addCleanup(self.tmp.cleanup)
        self.patch = patch.dict("os.environ", {"DATABASE_PATH": str(path / "test.db")})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        db.init_db()

    def test_atomic_save_returns_ids_and_hashes(self):
        saved = db.save_analysis("session", "private resume", "jd", 60,
                                 [{"missing_skill": "Python", "suggestion": "Learn Python"}])
        with db._transaction() as conn:
            self.assertEqual(conn.execute("SELECT id FROM suggestions").fetchone()[0], saved[0]["id"])
            self.assertEqual(conn.execute("SELECT resume_hash FROM sessions").fetchone()[0], db.hash_text("private resume"))

    def test_suggestion_failure_rolls_back_session(self):
        with self.assertRaises(sqlite3.ProgrammingError):
            db.save_analysis("session", "resume", "jd", 60,
                             [{"missing_skill": "Python", "suggestion": object()}])
        with db._transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM suggestions").fetchone()[0], 0)

    def test_foreign_keys_enforced(self):
        with self.assertRaises(sqlite3.IntegrityError):
            db.save_suggestions("absent", [{"missing_skill": "Python", "suggestion": "Learn"}])

    def test_connections_closed_on_success_and_failure(self):
        connections = []
        original = db._connect
        def tracked():
            conn = original()
            connections.append(conn)
            return conn
        with patch.object(db, "_connect", side_effect=tracked):
            db.save_session("session", "resume", "", None)
            with self.assertRaises(sqlite3.IntegrityError):
                db.save_session("session", "resume", "", None)
        for conn in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")
