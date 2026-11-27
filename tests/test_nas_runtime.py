"""Exercise the Rust listener, legacy session, and application rollback."""
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import unittest

import requests

from server.store import Store
from server.auth import Auth
from server.config import Settings
from server.google_oauth import Vault


ROOT = Path(__file__).resolve().parents[1]
BINARY = os.environ.get("COURT_MONITOR_NAS_BINARY")


@unittest.skipUnless(BINARY, "requires a built NAS server")
class NativeNasTests(unittest.TestCase):
    def test_legacy_command_session_pending_update_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            data = fixture / "data"
            data.mkdir()
            database = data / "cases.sqlite3"
            mounted = fixture / "mounted"
            mounted.symlink_to(data, target_is_directory=True)
            unavailable = fixture / "unavailable"
            (unavailable / database.name).mkdir(parents=True)
            store = Store(database)
            key = store.add_case("가상법원", "2024구합1", "가상당사자")
            store.set_active(key, False)
            legacy_auth = Auth(store, Settings(database=database, password="fixture-password"))
            legacy_session = legacy_auth.new_session()
            store.set_meta("google_mode", "oauth")
            store.set_meta("google_calendar", {"id": "fixture-calendar", "summary": "기존 캘린더"})
            vault = Vault(store)
            with socket.socket() as socket_probe:
                socket_probe.bind(("127.0.0.1", 0))
                port = socket_probe.getsockname()[1]
            base = f"http://127.0.0.1:{port}"
            runtime = "a" * 64
            environment = {
                **os.environ,
                "COURT_MONITOR_VERSION": "1.0.1", "COURT_MONITOR_RUNTIME": runtime,
                "COURT_AUTO_UPDATE": "false", "DATABASE_PATH": str(mounted / database.name),
                "TOKIO_WORKER_THREADS": "1",
                "HTTPS_PROXY": "http://127.0.0.1:1", "https_proxy": "http://127.0.0.1:1",
                "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1",
            }
            session = requests.Session()
            session.trust_env = False
            session.cookies.set("court_session", legacy_session, domain="127.0.0.1", path="/")

            def point_database_at(target):
                replacement = fixture / "replacement"
                replacement.symlink_to(target, target_is_directory=True)
                replacement.replace(mounted)

            def wait_health(expected_status):
                for _ in range(25):
                    response = session.get(base + "/health", timeout=1)
                    if response.status_code == expected_status:
                        return response.json()
                    time.sleep(0.25)
                self.fail(f"Expected /health {expected_status}, received {response.status_code}")

            def start(expected_version, create=None):
                binary = str(Path(BINARY).resolve())
                with tempfile.TemporaryFile(mode="w+") as logs:
                    server = subprocess.Popen([binary, "python", "-m", "uvicorn",
                        "server.app:create_app", "--factory", "--host", "127.0.0.1",
                        "--port", str(port), "--workers", "1", "--no-access-log"],
                        cwd=ROOT, env=environment, stdout=logs, stderr=logs)
                    try:
                        for _ in range(80):
                            if server.poll() is not None:
                                break
                            try:
                                result = session.get(base + "/health", timeout=0.5)
                                if result.ok and result.json()["version"] == expected_version:
                                    break
                            except requests.RequestException:
                                pass
                            time.sleep(0.15)
                        else:
                            self.fail(f"Version {expected_version} did not start")
                        self.assertIsNone(server.poll())
                        self.assertEqual(session.get(base + "/health", timeout=2).json()["version"], expected_version)
                        self.assertEqual(session.get(base + "/static/update.js", timeout=2).status_code, 200)
                        if create:
                            create()
                        response = session.get(base + "/api/cases", timeout=2)
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.json()["cases"][0]["case_category"], "행정")
                    except Exception:
                        logs.seek(0)
                        print(logs.read())
                        raise
                    finally:
                        server.terminate()
                        try:
                            server.wait(timeout=25)
                        except subprocess.TimeoutExpired:
                            server.kill()
                            server.wait()

            def login():
                self.assertEqual(requests.get(base + "/api/update", timeout=2).status_code, 401)
                self.assertEqual(requests.get(base + "/api/diagnostics", timeout=2).status_code, 401)
                bootstrap = session.get(base + "/api/bootstrap", timeout=2).json()
                self.assertTrue(bootstrap["diagnostics_available"])
                self.assertEqual(bootstrap["csrf_token"], legacy_auth.csrf_token(legacy_session))
                self.assertEqual(bootstrap["google"]["calendar_id"], "fixture-calendar")
                redirect_uri = base + "/oauth/google/callback"
                vault.set("google_client", {"client_id": "fixture.apps.googleusercontent.com",
                    "client_secret": "fixture-only", "redirect_uris": [redirect_uri],
                    "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token"})
                headers = {"X-CSRF-Token": bootstrap["csrf_token"], "Origin": base}
                connected = session.post(base + "/api/google/start", headers=headers,
                    json={"redirect_uri": redirect_uri}, timeout=3)
                self.assertEqual(connected.status_code, 200)
                from urllib.parse import parse_qs, urlsplit
                query = parse_qs(urlsplit(connected.json()["url"]).query)
                self.assertEqual(query["code_challenge_method"], ["S256"])
                callback = session.get(redirect_uri, params={"state":query["state"][0],"error":"access_denied"},
                    allow_redirects=False, timeout=2)
                self.assertEqual(callback.headers["Location"], "/?google=cancelled")
                unauthorized = session.post(base + "/api/update", json={"version": "1.0.2"}, timeout=2)
                self.assertEqual(unauthorized.status_code, 403)
                self.assertTrue(session.post(base + "/api/login", headers={"Origin": base},
                    json={"password": "fixture-password"}, timeout=10).ok)

                with sqlite3.connect(database) as connection:
                    for _ in range(30):
                        heartbeat = connection.execute(
                            "SELECT value FROM meta WHERE key='worker_heartbeat'"
                        ).fetchone()
                        if heartbeat:
                            break
                        time.sleep(0.1)
                    self.assertIsNotNone(heartbeat)
                    connection.execute("BEGIN IMMEDIATE")
                    time.sleep(2.3)
                    self.assertEqual(session.get(base + "/health", timeout=1).status_code, 200)
                    connection.rollback()

                point_database_at(unavailable)
                try:
                    self.assertEqual(wait_health(503)["status"], "database_unavailable")
                    self.assertEqual(session.get(base + "/static/update.js", timeout=1).status_code, 200)
                finally:
                    point_database_at(data)
                self.assertEqual(wait_health(200)["status"], "ok")

            def diagnostics_after_rollback():
                response = session.get(base + "/api/diagnostics", timeout=2)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                log = response.json()["log"]
                self.assertIn("Starting app 1.0.1", log)
                self.assertIn("App 1.0.3 failed to start", log)
                self.assertNotIn(legacy_session, log)
                self.assertNotIn("fixture-only", log)

            def stage(version, *, broken=False):
                updates = data / "app-updates"
                source = updates / "releases" / version
                source.mkdir(parents=True)
                executable = source / "court-monitor-nas"
                if broken:
                    executable.write_text("invalid executable", encoding="utf-8")
                    executable.chmod(0o755)
                else:
                    shutil.copy2(BINARY, executable)
                    shutil.copytree(ROOT / "server" / "static", source / "server" / "static")
                (source / "release.json").write_text(json.dumps({
                    "version": version, "revision": "b" * 40, "created": "2026-01-01T00:00:00Z",
                    "runtime": runtime, "sha256": "c" * 64,
                }), encoding="utf-8")
                (updates / "pending").write_text(version, encoding="utf-8")

            start("1.0.1", login)
            stage("1.0.2")
            start("1.0.2")
            stage("1.0.3", broken=True)
            start("1.0.2", diagnostics_after_rollback)
            self.assertEqual((data / "app-updates" / "failed").read_text(), "1.0.3")
            self.assertFalse((data / "app-updates" / "pending").exists())
