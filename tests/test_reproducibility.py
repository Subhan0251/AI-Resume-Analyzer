import io
import json
import tempfile
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch, Mock

import app
import db
from jd import JDRequirements
from matcher import ResumeJDMatcher
from parser import DynamicResumeParser, ResumeSchema, ContactInfo
from reproducibility import comparison_key
from skills import unique_skills


class SkillRepresentationTests(unittest.TestCase):
    def test_grouped_and_individual_skills_match_identically(self):
        grouped = ["Programming & Backend: Python, SQL, FastAPI, REST APIs",
                   "Cloud & Tools: AWS (EC2, S3), Git/GitHub", "Frameworks: LangChain, PyTorch"]
        atoms = ["Python", "SQL", "FastAPI", "REST API", "AWS", "EC2", "S3", "Git", "GitHub", "LangChain", "PyTorch"]
        self.assertEqual(unique_skills(grouped), unique_skills(atoms))

    def test_punctuation_and_empty_entries(self):
        self.assertEqual(unique_skills(["CI/CD, C++, C#, C, Java, JavaScript", "", " "]),
                         ["c", "c#", "c++", "ci/cd", "java", "javascript"])

    def test_parser_emits_atomic_skills_even_when_model_groups_them(self):
        parser = DynamicResumeParser("synthetic.docx")
        schema = ResumeSchema(contact=ContactInfo(), skills=["Backend: Python, SQL, FastAPI"])
        with patch.object(parser, "_parse_docx", return_value="Backend: Python, SQL, FastAPI"), patch.object(parser, "_extract_with_llm", return_value=schema):
            self.assertEqual(parser.process()["parsed_fields"]["skills"], ["fastapi", "python", "sql"])


class ReproducibilityTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.tmp = tempfile.TemporaryDirectory(dir=root)
        directory = Path(self.tmp.name).resolve()
        assert directory.is_relative_to(root)
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict("os.environ", {"DATABASE_PATH": str(directory / "test.db")})
        env.start(); self.addCleanup(env.stop)
        db.init_db()

    def response(self, session, score=75):
        return {"session_id": session, "status": "success", "match_score": score,
                "data": {"raw_normalized_text": "Synthetic resume", "job_description": "Python developer"},
                "gaps": [{"missing_skill": "SQL", "suggestion": "Learn SQL"}]}

    def test_first_committed_result_wins_across_connections(self):
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda response: db.save_canonical_analysis("same-key", response),
                                    [self.response("first", 75), self.response("second", 20)]))
        self.assertEqual(results[0], results[1])
        self.assertEqual(db.get_cached_analysis("same-key"), results[0])
        with db._transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM suggestions").fetchone()[0], 1)

    def test_cache_and_session_rollback_together(self):
        response = self.response("failed")
        response["unserializable"] = object()
        with self.assertRaises(TypeError):
            db.save_canonical_analysis("key", response)
        self.assertIsNone(db.get_cached_analysis("key"))
        with db._transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)

    def test_persisted_response_survives_a_fresh_python_process(self):
        expected = db.save_canonical_analysis("restart-key", self.response("persisted"))
        output = subprocess.check_output(
            [sys.executable, "-c", "import db,json; print(json.dumps(db.get_cached_analysis('restart-key')))"],
            cwd=Path(__file__).resolve().parents[1], text=True,
        )
        self.assertEqual(json.loads(output), expected)

    def test_same_upload_returns_identical_full_response_without_model_calls(self):
        parsed = {"parsed_fields": {"skills": ["Python"]}, "raw_normalized_text": "Python resume", "job_description": "Python developer"}
        with patch("app.validate_job_description", return_value=JDRequirements(required_skills=["Python"])) as validator, patch("app.parse_resume", return_value=parsed) as parser, patch("app.ResumeJDMatcher") as matcher:
            matcher.return_value.analyze.side_effect = [dict(match_score=100, gaps=[]), dict(match_score=0, gaps=[])]
            def analyze():
                return app._analyze_upload(io.BytesIO(b"synthetic cv bytes"), "cv.pdf", ".pdf", "Python developer", app.Settings())
            with ThreadPoolExecutor(2) as pool:
                results = list(pool.map(lambda _: analyze(), range(2)))
            # Simulate fresh calls/connections after the initial concurrent requests.
            results.extend(analyze() for _ in range(3))
            self.assertTrue(all(result == results[0] for result in results))
            validator.assert_called_once(); parser.assert_called_once(); matcher.return_value.analyze.assert_called_once()
            self.assertEqual(results[0]["match_score"], 100)

    def test_identity_changes_for_file_jd_version_and_model(self):
        def key(content=b"CV", jd="JD"):
            stream = io.BytesIO(content)
            result = comparison_key(stream, ".pdf", jd, 100)
            self.assertEqual(stream.tell(), 0)
            return result
        original = key()
        self.assertNotEqual(original, key(b"Other CV"))
        self.assertNotEqual(original, key(jd="Different JD"))
        with patch("reproducibility.analysis_version", return_value="new-version"):
            self.assertNotEqual(original, key())
        with patch.dict("os.environ", {"RESUME_PARSER_MODEL": "different-model"}):
            self.assertNotEqual(original, key())

    def test_failed_jd_validation_is_not_cached(self):
        with patch("app.validate_job_description", side_effect=RuntimeError("provider failed")), patch("app.parse_resume") as parser:
            with self.assertRaises(RuntimeError):
                app._analyze_upload(io.BytesIO(b"CV"), "cv.pdf", ".pdf", "JD", app.Settings())
            parser.assert_not_called()
        with db._transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM analysis_results").fetchone()[0], 0)
