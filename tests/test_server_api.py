from pathlib import Path
import json
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

    def test_build_identity_is_consistent_and_contains_no_secrets(self):
        health = self.client.get("/health").json()
        self.assertEqual(health["status"], "ok")
        self.assertTrue(health["version"])
        self.login()
        build = self.client.get("/api/bootstrap").json()["build"]
        self.assertEqual({key: health[key] for key in build}, build)
        self.assertNotIn(self.settings.password, str(health))
        self.assertEqual(self.app.version, build["version"])

    def test_bootstrap_is_explicit_when_calendar_is_not_connected(self):
        headers = self.login()
        data = self.client.get("/api/bootstrap").json()
        self.assertFalse(data["calendar_configured"])
        self.assertEqual(data["csrf_token"], headers["X-CSRF-Token"])

    def test_case_search_paginates_matches_and_keeps_global_counts(self):
        self.login()
        store = self.app.state.store
        keys = [store.add_case("가상법원", f"2024가단{i}", "검색당사자" if i < 13 else "다른당사자", now=i)
                for i in range(1, 26)]
        first = self.client.get("/api/cases").json()
        self.assertEqual((len(first["cases"]), first["total"], first["pages"]), (10, 25, 3))
        self.assertEqual(first["cases"][0]["id"], keys[-1])
        second = self.client.get("/api/cases", params={"q": "검색당사자", "page": 2}).json()
        self.assertEqual((len(second["cases"]), second["total"], second["pages"]), (2, 12, 2))
        self.assertEqual(second["metrics"]["total"], 25)
        self.assertEqual(second["metrics"]["monitoring"], 25)
        self.assertFalse({r["id"] for r in first["cases"]} & {r["id"] for r in second["cases"]})
        last = self.client.get("/api/cases", params={"q": "검색당사자", "page": 999}).json()
        self.assertEqual(last["page"], 2)
        self.assertEqual(last["cases"], second["cases"])
        self.assertEqual(len(self.client.get("/api/export").json()["cases"]), 25)
        for params in ({"page": 0}, {"page_size": 0}, {"page_size": 101}, {"q": "a" * 201}):
            self.assertEqual(self.client.get("/api/cases", params=params).status_code, 422)

    def test_search_uses_related_values_and_treats_symbols_literally(self):
        self.login()
        store = self.app.state.store
        key = store.add_case("가상법원", "2024가단123", "가상당사자")
        store.add_case("다른법원", "2024가단124", "다른당사자")
        snapshot = {"summary": {"판결 주문": "원고패 일부 50%_확정", "상소사건번호": "2026나456"},
                    "hearings": [{"day": "2026-12-01", "clock": "10:00", "kind": "변론", "location": "가상301호"}]}
        with store.db(True) as db:
            db.execute("UPDATE cases SET snapshot=? WHERE id=?", (json.dumps(snapshot, ensure_ascii=False), key))
        # Search the JSON values without depending on a particular judgment phrase.
        for query in ("가상당사자", "2024 가단 123", "가상법원 원고패", "2026나456", "가상301호", "50%_"):
            data = self.client.get("/api/cases", params={"q": query}).json()
            self.assertEqual([r["id"] for r in data["cases"]], [key], query)
        for query in ("없는단어", "' OR 1=1 --", "summary"):
            data = self.client.get("/api/cases", params={"q": query, "page": 5}).json()
            self.assertEqual((data["cases"], data["total"], data["page"], data["pages"]), ([], 0, 1, 1))

    def test_bulk_delete_requires_login_and_csrf_and_removes_only_selected_cases(self):
        store = self.app.state.store
        keys = [store.add_case("가상법원", f"2024가단{i}", "가상당사자") for i in range(1, 4)]
        payload = {"ids": [keys[0], keys[2], keys[0], "0" * 32]}
        self.assertEqual(self.client.post("/api/cases/delete", json=payload).status_code, 401)
        headers = self.login()
        self.assertEqual(self.client.post("/api/cases/delete", json=payload).status_code, 403)
        result = self.client.post("/api/cases/delete", json=payload, headers=headers)
        self.assertEqual(result.json(), {"deleted": 2})
        self.assertEqual([r["id"] for r in store.list_cases()], [keys[1]])
        self.assertEqual(self.client.post("/api/cases/delete", json=payload, headers=headers).json(), {"deleted": 0})
        for ids in ([], ["invalid"], ["' OR 1=1 --"]):
            self.assertEqual(self.client.post("/api/cases/delete", json={"ids": ids}, headers=headers).status_code, 422)
