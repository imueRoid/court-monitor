from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from server.app import create_app
from server.auth import Auth, COOKIE, verify_password
from server.config import Settings
from server.store import Store


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.settings = Settings(database=Path(self.folder.name)/"cases.sqlite3",password="initial-fixture-password")
        self.app = create_app(self.settings, start_worker=False)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.folder.cleanup()

    def login(self, client=None, password=None):
        client = client or self.client
        response = client.post("/api/login",json={"password":password or self.settings.password})
        self.assertEqual(response.status_code,200)
        return {"X-CSRF-Token":client.get("/api/bootstrap").json()["csrf_token"]}

    def test_password_is_salted_hash_and_session_token_is_hashed_in_db(self):
        self.login()
        store = self.app.state.store
        encoded = store.get_meta("admin_password_hash")
        self.assertTrue(verify_password(self.settings.password,encoded))
        self.assertNotIn(self.settings.password,encoded)
        with store.db() as db:
            digest = db.execute("SELECT token_hash FROM sessions").fetchone()[0]
        self.assertNotEqual(digest,self.client.cookies.get(COOKIE))
        cookie = self.client.post("/api/login",json={"password":self.settings.password}).headers["set-cookie"]
        self.assertIn("HttpOnly",cookie)
        self.assertIn("SameSite=lax",cookie)
        self.assertIn("Max-Age=7776000",cookie)

    def test_session_and_cases_survive_app_restart_without_bootstrap_password(self):
        headers = self.login()
        self.client.post("/api/cases",json={"court":"가상법원","case_number":"2024가단1","party":"가상인"},headers=headers)
        restarted = TestClient(create_app(replace(self.settings,password=""),start_worker=False))
        try:
            restarted.cookies.update(self.client.cookies)
            response = restarted.get("/api/cases")
            self.assertEqual(response.status_code,200)
            self.assertEqual(len(response.json()["cases"]),1)
        finally:
            restarted.close()

    def test_expiry_and_logout_revoke_session(self):
        headers = self.login()
        token = self.client.cookies.get(COOKIE)
        self.assertFalse(self.app.state.auth.valid_session(token,time.time()+91*86400))
        self.assertEqual(self.client.post("/api/logout",json={},headers=headers).status_code,200)
        self.assertFalse(self.app.state.auth.valid_session(token))
        self.assertEqual(self.client.get("/api/cases").status_code,401)

    def test_password_change_revokes_all_devices_and_is_not_reset_by_env_on_restart(self):
        headers = self.login()
        other = TestClient(self.app)
        try:
            self.login(other)
            self.assertEqual(self.client.post("/api/password",json={"current":self.settings.password,"new":"1234567"},headers=headers).status_code,422)
            response = self.client.post("/api/password",json={"current":self.settings.password,"new":"newpass8"},headers=headers)
            self.assertEqual(response.status_code,200)
            self.assertEqual(other.get("/api/cases").status_code,401)
            restarted = create_app(self.settings,start_worker=False)
            self.assertFalse(verify_password(self.settings.password,restarted.state.store.get_meta("admin_password_hash")))
            self.assertTrue(verify_password("newpass8",restarted.state.store.get_meta("admin_password_hash")))
        finally:
            other.close()

    def test_csrf_from_other_device_and_cross_origin_login_are_rejected(self):
        old_headers = self.login()
        self.login()
        self.assertEqual(self.client.post("/api/logout",json={},headers=old_headers).status_code,403)
        self.assertEqual(self.client.post("/api/login",json={"password":self.settings.password},headers={"Origin":"https://untrusted.example"}).status_code,403)

    def test_failed_login_is_throttled_and_throttle_survives_restart(self):
        for _ in range(5):
            self.assertEqual(self.client.post("/api/login",json={"password":"wrong"}).status_code,401)
        restarted = TestClient(create_app(self.settings,start_worker=False))
        try:
            self.assertEqual(restarted.post("/api/login",json={"password":self.settings.password}).status_code,429)
        finally:
            restarted.close()

    def test_single_file_compose_environment_and_repr_do_not_expose_secrets(self):
        with patch.dict("os.environ",{"ADMIN_PASSWORD":"envpass8","ADMIN_PASSWORD_FILE":"","GOOGLE_CREDENTIALS_JSON":"fixture-credential-content"},clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.password,"envpass8")
        self.assertEqual(settings.credentials_json,"fixture-credential-content")
        self.assertNotIn("envpass8",repr(settings))
        self.assertNotIn("fixture-credential-content",repr(settings))

    def test_first_visit_sets_password_once_without_compose_secrets(self):
        settings = replace(self.settings,database=Path(self.folder.name)/"empty.sqlite3",password="")
        other = TestClient(create_app(settings,start_worker=False))
        try:
            self.assertEqual(other.get("/",follow_redirects=False).headers["location"],"/setup")
            self.assertIn("처음 설정",other.get("/setup").text)
            self.assertEqual(other.post("/api/setup",json={"password":"1234567"}).status_code,422)
            self.assertEqual(other.post("/api/setup",json={"password":"setpass8"}).status_code,200)
            self.assertEqual(other.get("/api/cases").status_code,200)
            self.assertEqual(other.post("/api/setup",json={"password":"overwrite-password-long"}).status_code,409)
        finally:
            other.close()
