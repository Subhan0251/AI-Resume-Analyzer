import io
import os
import tempfile
import threading
import unittest
from zipfile import ZipFile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pymupdf
import docx
from fastapi.testclient import TestClient

import app
from errors import InputLimitError, InvalidJobDescriptionError, JDValidationUnavailableError
from jd import JDRequirements


KEY = "test-key-" + "a" * 32
HEADERS = {"X-API-Key": KEY}
RESULT = {"parsed_fields": {}, "raw_normalized_text": "Resume text", "job_description": ""}


def pdf_bytes(scanned=False):
    with pymupdf.open() as document:
        page = document.new_page()
        if not scanned:
            page.insert_text((72, 72), "Resume: Python engineer with software project experience.")
        return document.tobytes()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.settings = app.Settings(api_key=KEY, requests_per_minute=100)
        cache = patch("app.db.get_cached_analysis", return_value=None)
        cache.start()
        self.addCleanup(cache.stop)
        validator = patch("app.validate_job_description", return_value=JDRequirements(required_skills=["Python"]))
        self.jd_validator = validator.start()
        self.addCleanup(validator.stop)
        for target in ("app.validate_configuration", "app.db.init_db"):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def client(self, **changes):
        return TestClient(app.create_app(replace(self.settings, **changes)))

    def upload(self, client, content=b"resume", filename="resume.pdf", **kwargs):
        kwargs.setdefault("data", {"job_description": "Python engineer required"})
        return client.post("/api/v1/analyze", headers=HEADERS,
                           files={"file": (filename, content)}, **kwargs)

    def test_job_description_required_before_processing(self):
        with self.client() as client, patch("app.parse_resume") as parse, patch("app.ResumeJDMatcher") as matcher, patch("app.db.save_canonical_analysis") as save:
            for data in ({}, {"job_description": ""}, {"job_description": " \t\n "}):
                with self.subTest(data=data):
                    self.assertEqual(self.upload(client, data=data).status_code, 422)
            parse.assert_not_called()
            matcher.assert_not_called()
            save.assert_not_called()

    def test_openapi_marks_job_description_required(self):
        schema = app.create_app(self.settings).openapi()
        body = schema["paths"]["/api/v1/analyze"]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]
        fields = schema["components"]["schemas"][body["$ref"].split("/")[-1]]
        self.assertIn("job_description", fields["required"])

    def test_invalid_jd_stops_before_cv_processing(self):
        self.jd_validator.side_effect = InvalidJobDescriptionError("Please provide a valid job description.")
        with self.client() as client, patch("app._save_upload_to_temp") as copy, patch("app.parse_resume") as parse, patch("app.ResumeJDMatcher") as matcher, patch("app.db.save_canonical_analysis") as save:
            response = self.upload(client, data={"job_description": "An unrelated English paragraph."})
            self.assertEqual(response.status_code, 422)
            for operation in (copy, parse, matcher, save):
                operation.assert_not_called()

    def test_validator_unavailable_stops_analysis(self):
        self.jd_validator.side_effect = JDValidationUnavailableError("Unable to validate the job description right now.")
        with self.client() as client, patch("app.parse_resume") as parse, patch("app.db.save_canonical_analysis") as save:
            self.assertEqual(self.upload(client).status_code, 503)
            parse.assert_not_called()
            save.assert_not_called()

    def test_authentication_before_upload(self):
        with self.client() as client, patch("app.parse_resume") as parse:
            response = client.post("/api/v1/analyze", content=b"invalid")
            self.assertEqual(response.status_code, 401)
            parse.assert_not_called()

    def test_scanned_pdf_returns_requested_error(self):
        with self.client() as client, patch("parser.DynamicResumeParser._extract_with_llm") as llm:
            response = self.upload(client, pdf_bytes(scanned=True))
            self.assertEqual(response.status_code, 400)
            self.assertIn("authentic, text-based PDF or DOCX", response.json()["detail"])
            llm.assert_not_called()

    def test_invalid_and_empty_uploads(self):
        with self.client() as client:
            for filename, content in (("resume.txt", b"bad"), ("resume.pdf", b"bad"), ("resume.docx", b"bad"), ("resume.pdf", b"")):
                self.assertEqual(self.upload(client, content, filename).status_code, 400)

    def test_file_and_jd_limits(self):
        with self.client(max_upload_bytes=4, max_jd_chars=4) as client:
            self.assertEqual(self.upload(client, b"12345", data={"job_description": "Job"}).status_code, 413)
            self.assertEqual(self.upload(client, b"123", data={"job_description": "12345"}).status_code, 413)

    def test_malformed_docx_xml_is_input_error(self):
        source = io.BytesIO()
        docx.Document().save(source)
        broken = io.BytesIO()
        with ZipFile(source) as archive, ZipFile(broken, "w") as output:
            for item in archive.infolist():
                output.writestr(item, b"<invalid" if item.filename == "word/document.xml" else archive.read(item))
        with self.client() as client:
            self.assertEqual(self.upload(client, broken.getvalue(), "resume.docx").status_code, 400)

    def test_matching_returns_persisted_suggestion_ids(self):
        gap = {"missing_skill": "SQL", "suggestion": "Learn SQL", "importance": "required"}
        saved_gap = dict(gap, id="persisted-id")
        with self.client() as client, patch("app.parse_resume", return_value=RESULT), patch("app.ResumeJDMatcher") as matcher, patch("app.db.save_canonical_analysis", side_effect=lambda key, response: dict(response, gaps=[saved_gap])) as save:
            matcher.return_value.analyze.return_value = {"match_score": 40, "gaps": [gap]}
            response = self.upload(client, data={"job_description": "  SQL required \n"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["gaps"][0]["id"], "persisted-id")
            self.assertEqual(save.call_args.args[1]["gaps"], [gap])
            matcher.assert_called_once_with(RESULT, "SQL required", validated_requirements=self.jd_validator.return_value)
            self.jd_validator.assert_called_once_with("SQL required")

    def test_declared_and_streamed_body_limits(self):
        with self.client(max_body_bytes=128) as client:
            self.assertEqual(client.post("/api/v1/analyze", headers=HEADERS, content=b"x" * 129).status_code, 413)
            headers = dict(HEADERS, **{"Content-Type": "multipart/form-data; boundary=test"})
            body = b'--test\r\nContent-Disposition: form-data; name="file"; filename="r.pdf"\r\n\r\n' + b"x" * 200 + b"\r\n--test--\r\n"
            response = client.post("/api/v1/analyze", headers=headers, content=iter([body[:100], body[100:]]))
            self.assertEqual(response.status_code, 413)

    def test_rate_limit(self):
        with self.client(requests_per_minute=1) as client:
            self.assertEqual(self.upload(client, filename="r.txt").status_code, 400)
            self.assertEqual(self.upload(client, filename="r.txt").status_code, 429)

    def test_success_atomic_persistence_and_cleanup(self):
        paths = []
        def parse(path, jd):
            paths.append(path)
            self.assertTrue(os.path.exists(path))
            self.jd_validator.assert_called_once_with(jd)
            return RESULT
        with self.client() as client, patch("app.parse_resume", side_effect=parse), patch("app.ResumeJDMatcher") as matcher, patch("app.db.save_canonical_analysis", side_effect=lambda key, response: response) as save:
            matcher.return_value.analyze.return_value = {"match_score": 80, "gaps": []}
            response = self.upload(client)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["match_score"], 80)
            matcher.return_value.analyze.assert_called_once()
            save.assert_called_once()
        self.assertTrue(paths)
        self.assertTrue(all(not os.path.exists(path) for path in paths))

    def test_internal_errors_are_sanitized_and_cleanup_runs(self):
        paths = []
        def parse(path, jd):
            paths.append(path)
            raise RuntimeError("secret provider payload")
        with self.client() as client, patch("app.parse_resume", side_effect=parse):
            response = self.upload(client)
            self.assertEqual(response.status_code, 500)
            self.assertNotIn("secret", response.text)
            self.assertIn("incident_id", response.text)
        self.assertTrue(all(not os.path.exists(path) for path in paths))

    def test_concurrency_capacity_recovers(self):
        entered, release = threading.Event(), threading.Event()
        def parse(path, jd):
            entered.set()
            if not release.wait(10):
                raise RuntimeError("Test timed out")
            return RESULT
        with self.client(max_concurrent=1) as client, patch("app.parse_resume", side_effect=parse), patch("app.ResumeJDMatcher") as matcher, patch("app.db.save_canonical_analysis", side_effect=lambda key, response: response), ThreadPoolExecutor(1) as pool:
            matcher.return_value.analyze.return_value = {"match_score": 80, "gaps": []}
            first = pool.submit(self.upload, client)
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.upload(client).status_code, 503)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=10).status_code, 200)
            self.assertEqual(self.upload(client).status_code, 200)

    def test_partial_copy_is_removed(self):
        paths = []
        real_temp = tempfile.NamedTemporaryFile
        def temp(*args, **kwargs):
            file = real_temp(*args, **kwargs)
            paths.append(file.name)
            return file
        with patch("app.tempfile.NamedTemporaryFile", side_effect=temp):
            with self.assertRaises(InputLimitError):
                app._save_upload_to_temp(io.BytesIO(b"too big"), ".pdf", 3)
        self.assertTrue(all(not Path(path).exists() for path in paths))
