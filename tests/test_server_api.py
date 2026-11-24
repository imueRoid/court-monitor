from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from server.app import create_app
from server.config import Settings


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.settings = Settings(database=Path(self.folder.name) / "cases.sqlite3", password="test-password-is-long-enough")
        self.app = create_app(self.settings, start_worker=False)
        self.client = TestClient(self.app)

    def login(self):
        response = self.client.post("/api/login", json={"password": self.settings.password})
        self.assertEqual(response.status_code, 200)
        return {"X-CSRF-Token": self.client.get("/api/bootstrap").json()["csrf_token"]}

    def tearDown(self):
        self.client.close()
        self.folder.cleanup()

    def test_authentication_is_required_but_health_is_public(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 303)
        self.assertIn("관리자 로그인", self.client.get("/login").text)
        self.assertEqual(self.client.get("/api/cases").status_code, 401)
        self.assertEqual(self.client.post("/api/login", json={"password":"wrong"}).status_code, 401)
        self.login()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-frame-options"], "DENY")

    def test_mutation_requires_csrf_and_case_format_is_validated(self):
        payload = {"court": "가상법원", "case_number": "2024가단1", "party": "가상인"}
        headers = self.login()
        self.assertEqual(self.client.post("/api/cases", json=payload).status_code, 403)
        response = self.client.post("/api/cases", json=payload, headers=headers)
        self.assertEqual(response.status_code, 200)
        invalid = {**payload, "case_number": "not-a-case"}
        self.assertEqual(self.client.post("/api/cases", json=invalid, headers=headers).status_code, 422)
        self.assertEqual(len(self.app.state.store.list_cases()), 1)

    def test_import_refresh_and_pause(self):
        headers = self.login()
        case = {"court": "가상법원", "case_number": "2024가단1", "party": "가상인"}
        response = self.client.post("/api/cases/import", json={"cases": [case, case]}, headers=headers)
        key = response.json()["ids"][0]
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(self.client.post(f"/api/cases/{key}/refresh", json={}, headers=headers).status_code, 200)
        self.client.post(f"/api/cases/{key}/active", json={"active": False}, headers=headers)
        records = self.client.get("/api/cases").json()["cases"]
        self.assertEqual(records[0]["active"], 0)
        self.assertNotIn("lease_token", records[0])
        exported = self.client.get("/api/export").json()
        self.assertNotIn(self.settings.password, str(exported))

    def test_bootstrap_is_explicit_when_calendar_is_not_connected(self):
        headers = self.login()
        data = self.client.get("/api/bootstrap").json()
        self.assertFalse(data["calendar_configured"])
        self.assertEqual(data["csrf_token"], headers["X-CSRF-Token"])
