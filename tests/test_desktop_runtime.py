import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from desktop_runtime import (DesktopGoogleConnection, OAuthError, SCOPE, SingleInstance, WindowsVault,
                             launch_command, save_preferences, validate_case)
from server.google_oauth import Vault
from server.store import Store


class DesktopRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.folder.name) / "cases.sqlite3")

    def tearDown(self):
        self.folder.cleanup()

    def test_settings_are_independent_and_rollback_startup_when_database_write_fails(self):
        startup = Mock()
        startup.enabled.return_value = False
        save_preferences(self.store, startup, auto_start=True, background=False)
        self.assertEqual(self.store.get_meta("desktop_preferences"), {"auto_start":True, "background":False})
        startup.set.assert_called_with(True)
        with patch.object(self.store, "set_meta", side_effect=OSError("fixture")):
            with self.assertRaises(OSError):
                save_preferences(self.store, startup, auto_start=True, background=True)
        startup.set.assert_called_with(False)

    def test_startup_quotes_executable_with_spaces(self):
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", r"C:\My Apps\CourtMonitorPC.exe"):
            command = launch_command()
            self.assertEqual(command, [r"C:\My Apps\CourtMonitorPC.exe", "--startup"])
            self.assertEqual(subprocess.list2cmdline(command), '"C:\\My Apps\\CourtMonitorPC.exe" --startup')

    def test_case_validation_matches_server_and_normalizes_spaces(self):
        self.assertEqual(validate_case(" 가상법원 ", "2024 가단 123", "가 상 인"), ("가상법원", "2024가단123", "가상인"))
        with self.assertRaises(Exception):
            validate_case("가상법원", "invalid", "가상인")

    def connection(self, **kwargs):
        return DesktopGoogleConnection(self.store, vault_factory=Vault, **kwargs)

    def test_desktop_oauth_rejects_web_clients_and_pins_official_endpoints(self):
        connection = self.connection()
        with self.assertRaises(OAuthError):
            connection.configure({"web":{"client_id":"fixture.apps.googleusercontent.com", "client_secret":"fixture"}})
        connection.configure({"installed":{"client_id":"fixture.apps.googleusercontent.com", "client_secret":"fixture-secret",
                                             "token_uri":"https://untrusted.invalid/token", "auth_uri":"https://untrusted.invalid/auth"}})
        client = connection.vault.get("desktop_google_client")
        self.assertEqual(client["token_uri"], "https://oauth2.googleapis.com/token")
        self.assertEqual(client["auth_uri"], "https://accounts.google.com/o/oauth2/auth")
        self.assertTrue(connection.status()["client_configured"])
        self.assertNotIn("fixture-secret", self.store.path.read_bytes().decode("latin1"))

    def test_native_oauth_uses_loopback_pkce_and_existing_calendar_pipeline(self):
        credentials = Mock(refresh_token="fixture-refresh", granted_scopes=[SCOPE])
        credentials.to_json.return_value = json.dumps({"token":"fixture", "refresh_token":"fixture-refresh"})
        flow = Mock()
        flow.run_local_server.return_value = credentials
        factory = Mock(return_value=flow)
        connection = self.connection(flow_factory=factory)
        connection.configure({"installed":{"client_id":"fixture.apps.googleusercontent.com", "client_secret":"fixture"}})
        with patch.object(connection, "prepare_calendar", return_value="fixture-calendar") as prepare:
            self.assertEqual(connection.authorize(), "fixture-calendar")
        self.assertTrue(factory.call_args.kwargs["autogenerate_code_verifier"])
        self.assertEqual(factory.call_args.kwargs["scopes"], [SCOPE])
        self.assertEqual(flow.run_local_server.call_args.kwargs["bind_addr"], "127.0.0.1")
        self.assertEqual(flow.run_local_server.call_args.kwargs["port"], 0)
        self.assertEqual(flow.run_local_server.call_args.kwargs["timeout_seconds"], 180)
        self.assertEqual(connection.vault.get("google_pending_tokens")["refresh_token"], "fixture-refresh")
        prepare.assert_called_once()

    def test_changed_configuration_cannot_restore_stale_google_authorization(self):
        flow = Mock()
        factory = Mock(return_value=flow)
        connection = self.connection(flow_factory=factory)
        client = {"installed":{"client_id":"fixture.apps.googleusercontent.com", "client_secret":"fixture"}}
        connection.configure(client)
        credentials = Mock(refresh_token="fixture-refresh", granted_scopes=[SCOPE])
        def change_config(**kwargs):
            connection.configure(client)
            return credentials
        flow.run_local_server.side_effect = change_config
        with self.assertRaisesRegex(OAuthError, "설정이 변경"):
            connection.authorize()
        self.assertIsNone(self.store.get_meta("google_pending_tokens"))

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI")
    def test_windows_encryption_restores_after_restart_and_rejects_tampering(self):
        vault = WindowsVault(self.store)
        vault.set("test_secret", {"refresh_token":"fixture-secret"})
        restored = WindowsVault(Store(self.store.path))
        self.assertEqual(restored.get("test_secret"), {"refresh_token":"fixture-secret"})
        self.assertNotIn(b"fixture-secret", self.store.path.read_bytes())
        with self.assertRaises(OAuthError):
            restored.decode("dpapi:v1:not-base64")

    @unittest.skipUnless(sys.platform == "win32", "Windows single instance")
    def test_second_launch_detects_existing_instance_and_releases_after_exit(self):
        first = SingleInstance(self.folder.name)
        second = SingleInstance(self.folder.name)
        try:
            self.assertFalse(first.already_running)
            self.assertTrue(second.already_running)
        finally:
            first.close()
            second.close()
        third = SingleInstance(self.folder.name)
        try:
            self.assertFalse(third.already_running)
        finally:
            third.close()
