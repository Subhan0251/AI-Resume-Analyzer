import unittest

from fastapi.testclient import TestClient

from app import Settings, create_app


class FrontendTests(unittest.TestCase):
    def setUp(self):
        # Static routes do not require DB/model startup or an application access key.
        self.client = TestClient(create_app(Settings()))
        self.addCleanup(self.client.close)

    def test_workspace_and_assets_are_served(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn('id="job-description"', response.text)
        for asset, content_type in (("styles.css", "text/css"), ("app.js", "javascript")):
            response = self.client.get("/static/" + asset)
            self.assertEqual(response.status_code, 200)
            self.assertIn(content_type, response.headers["content-type"])

    def test_static_mount_cannot_expose_configuration(self):
        for path in ("/static/.env", "/static/%2e%2e/.env", "/static/app.py"):
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_analysis_remains_authenticated(self):
        self.assertEqual(self.client.post("/api/v1/analyze").status_code, 401)
