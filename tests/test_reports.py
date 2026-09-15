import json
from pathlib import Path
import unittest
import unicodedata
from unittest.mock import patch
import pymupdf
from fastapi.testclient import TestClient
from app import create_app, Settings
from reports import render_pdf


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.report = json.loads((Path(__file__).resolve().parents[1] / "frontend/sample-report.json").read_text(encoding="utf-8"))

    def test_pdf_is_paginated_readable_and_contains_all_gaps(self):
        data = render_pdf(self.report)
        self.assertTrue(data.startswith(b"%PDF"))
        with pymupdf.open(stream=data, filetype="pdf") as document:
            self.assertGreater(len(document), 1)
            text = unicodedata.normalize("NFKC", " ".join(page.get_text() for page in document))
            text = " ".join(text.split())
            self.assertIn("Requirements", text)
            for gap in self.report["gaps"]:
                self.assertIn(gap["missing_skill"], text)
            for row in self.report["scorecard"]["criteria"]:
                self.assertIn(row["requirement"], text)
            self.assertIn("Assessment rubric", text)

    def test_untrusted_html_is_literal_pdf_text(self):
        self.report["filename"] = '<script>alert("x")</script>'
        with pymupdf.open(stream=render_pdf(self.report), filetype="pdf") as document:
            text = "".join(page.get_text() for page in document)
            self.assertIn("<script>", text)

    def test_every_page_has_consistent_contrast_and_content_margins(self):
        with pymupdf.open(stream=render_pdf(self.report), filetype="pdf") as document:
            for page in document:
                # A cover background must never be replayed over later content.
                for drawing in page.get_drawings():
                    fill = drawing.get("fill")
                    self.assertFalse(fill and max(fill) < 0.5)
                for block in page.get_text("dict")["blocks"]:
                    for line in block.get("lines", []):
                        for span in line["spans"]:
                            self.assertGreaterEqual(span["bbox"][0], 43)
                            self.assertLessEqual(span["bbox"][2], 552)
                            self.assertLessEqual(span["bbox"][3], 815)
                            self.assertNotEqual(span["color"], 0xFFFFFF)

    def test_long_evidence_continues_without_losing_text(self):
        row = self.report["scorecard"]["criteria"][0]
        row["cv_evidence"] = [{"line": 1, "kind": "work", "quote":
            " ".join(f"Evidence passage {i}." for i in range(400))}]
        with pymupdf.open(stream=render_pdf(self.report), filetype="pdf") as document:
            text = " ".join(" ".join(page.get_text(clip=pymupdf.Rect(44, 55, 551, 790))
                                      for page in document).split())
            for i in range(400):
                self.assertIn(f"Evidence passage {i}.", text)
            self.assertIn("Assessment rubric", text)

    def test_pdf_route_authenticates_and_uses_saved_report(self):
        key = "synthetic-report-key-" + "x" * 32
        with patch("app.db.init_db"), patch("app.validate_configuration"), TestClient(create_app(Settings(api_key=key))) as client, patch("app.db.get_report", return_value=self.report) as get:
            response = client.post("/api/v1/report/pdf", json={"session_id": "id"})
            self.assertEqual(response.status_code, 401)
            get.assert_not_called()
            response = client.post("/api/v1/report/pdf", headers={"X-API-Key": key}, json={"session_id": "id"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "application/pdf")
            self.assertEqual(response.headers["cache-control"], "no-store")
            get.assert_called_once_with("id")

    def test_missing_and_legacy_reports_have_clear_errors(self):
        key = "test-key-" + "x" * 32
        with patch("app.db.init_db"), patch("app.validate_configuration"), TestClient(create_app(Settings(api_key=key))) as client:
            for report, status in ((None, 404), ({"match_score": 50}, 409)):
                with patch("app.db.get_report", return_value=report):
                    response = client.post("/api/v1/report/pdf", headers={"X-API-Key": key}, json={"session_id": "id"})
                    self.assertEqual(response.status_code, status)
