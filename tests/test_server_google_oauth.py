from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from google.oauth2.credentials import Credentials
from server.app import create_app
from server.auth import COOKIE
from server.config import Settings
from server.google_oauth import CALLBACK, GoogleConnection, OAuthError, SCOPE, Vault, check_redirect
from server.google_calendar import GoogleCalendar
from server.store import Store


URI = "https://nas.example.com" + CALLBACK
CLIENT = {"web":{"client_id":"fixture.apps.googleusercontent.com","client_secret":"fixture-client-secret",
    "redirect_uris":[URI],"auth_uri":"https://untrusted.invalid/auth","token_uri":"https://untrusted.invalid/token"}}


def credential(token="fixture-access-token"):
    return Credentials(token, refresh_token="fixture-refresh-token", token_uri="https://oauth2.googleapis.com/token",
                       client_id="fixture.apps.googleusercontent.com",client_secret="fixture-client-secret",scopes=[SCOPE])


def response(status, data):
    return Mock(status_code=status,ok=200<=status<300,json=Mock(return_value=data))


class GoogleOAuthTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.folder.name)/"cases.sqlite3")
        self.session = Mock()
        self.session.post.return_value=response(200,{"id":"fixture-calendar@example.com","summary":"사건 관리"})
        self.factory = Mock()
        self.flow = Mock(credentials=credential())
        self.flow.authorization_url.return_value=("https://accounts.google.com/fixture", "ignored")
        self.factory.return_value=self.flow
        self.connection=GoogleConnection(self.store,flow_factory=self.factory,session_factory=Mock(return_value=self.session))
        self.connection.configure(CLIENT)

    def tearDown(self):
        self.folder.cleanup()

    def begin(self, token="fixture-session", now=1000):
        self.connection.begin(URI,token,now)
        return self.factory.call_args.kwargs["state"]

    def finish(self, state, token="fixture-session", now=1001):
        return self.connection.finish(state,"fixture-authorization-code",token,now)

    def test_authorization_uses_pkce_offline_and_only_app_created_scope(self):
        real=GoogleConnection(self.store)
        url=real.begin(URI,"session",1000)
        parsed=urlsplit(url); params=parse_qs(parsed.query)
        self.assertEqual(parsed.netloc,"accounts.google.com")
        self.assertEqual(params["scope"],[SCOPE])
        self.assertEqual(params["access_type"],["offline"])
        self.assertEqual(params["code_challenge_method"],["S256"])
        self.assertTrue(params["code_challenge"][0])
        stored=real.vault.get("google_client")
        self.assertEqual(stored["token_uri"],"https://oauth2.googleapis.com/token")

    def test_sensitive_client_tokens_and_verifier_are_encrypted_in_sqlite(self):
        state=self.begin()
        self.finish(state)
        with self.store.db() as db:
            values=" ".join(row[0] for row in db.execute("SELECT value FROM meta"))
        for secret in ("fixture-client-secret","fixture-refresh-token","fixture-access-token"):
            self.assertNotIn(secret,values)
        self.assertEqual(self.connection.vault.get("google_tokens")["refresh_token"],"fixture-refresh-token")
        self.assertTrue(self.connection.connected)

    def test_wrong_device_does_not_consume_legitimate_state(self):
        state=self.begin()
        with self.assertRaises(OAuthError):
            self.finish(state,"different-session")
        self.factory.return_value.fetch_token.assert_not_called()
        self.finish(state)
        self.assertTrue(self.connection.connected)

    def test_expired_and_replayed_callbacks_are_rejected(self):
        state=self.begin()
        with self.assertRaises(OAuthError): self.finish(state,now=1601)
        state=self.begin(now=2000)
        self.finish(state,now=2001)
        with self.assertRaises(OAuthError): self.finish(state,now=2002)

    def test_first_authorization_creates_user_calendar_and_reauthorization_reuses_it(self):
        self.finish(self.begin())
        self.assertEqual(self.session.post.call_count,1)
        self.session.get.return_value=response(200,{"id":"fixture-calendar@example.com","summary":"사건 관리"})
        self.finish(self.begin(now=2000),now=2001)
        self.assertEqual(self.session.post.call_count,1)
        self.assertEqual(self.connection.calendar_id,"fixture-calendar@example.com")

    def test_failed_calendar_creation_can_retry_without_another_google_login(self):
        self.session.post.return_value=response(403,{})
        with self.assertRaises(OAuthError): self.finish(self.begin())
        self.assertTrue(self.connection.status()["authorized"])
        self.assertFalse(self.connection.connected)
        self.session.post.return_value=response(200,{"id":"calendar@example.com"})
        self.connection.prepare_calendar()
        self.assertTrue(self.connection.connected)
        self.flow.fetch_token.assert_called_once()

    def test_disconnect_drops_tokens_but_preserves_calendar_and_existing_events(self):
        self.finish(self.begin())
        identifier=self.connection.calendar_id
        self.connection.disconnect()
        self.assertFalse(self.connection.connected)
        self.assertIsNone(self.store.get_meta("google_tokens"))
        self.assertEqual(self.connection.calendar_id,identifier)
        self.session.delete.assert_not_called()

    def test_late_refresh_cannot_restore_disconnected_credentials(self):
        self.finish(self.begin())
        generation=self.connection.generation
        self.connection.disconnect()
        self.connection.persist_refresh(credential("late-token"),generation)
        self.assertIsNone(self.store.get_meta("google_tokens"))

    def test_nas_restart_restores_encrypted_connection(self):
        self.finish(self.begin())
        restored=GoogleConnection(Store(self.store.path))
        self.assertTrue(restored.connected)
        self.assertEqual(restored.credentials().refresh_token,"fixture-refresh-token")

    def test_calendar_writes_use_db_target_and_persist_refreshed_token(self):
        self.finish(self.begin())
        calendar=GoogleCalendar(Settings(database=self.store.path),self.store)
        session=Mock()
        def write(*args,**kwargs):
            calendar.oauth_credentials.token="refreshed-access-token"
            return response(200,{"id":"event"})
        session.patch.side_effect=write
        with patch("server.google_calendar.AuthorizedSession",return_value=session):
            calendar.upsert("abcd0",{"summary":"판결선고"})
        self.assertIn("fixture-calendar%40example.com/events/abcd0",session.patch.call_args.args[0])
        self.assertEqual(self.connection.vault.get("google_tokens")["token"],"refreshed-access-token")

    def test_lost_vault_key_is_reported_without_overwriting_db(self):
        (Path(self.folder.name)/"credentials.key").unlink()
        with self.assertRaisesRegex(RuntimeError,"함께 복원"):
            Vault(self.store)

    def test_redirect_uri_and_client_type_validation(self):
        for uri in ("http://192.168.1.1:7708"+CALLBACK,"https://user:secret@nas.example.com"+CALLBACK,URI+"?bad=1","https://[bad"):
            with self.assertRaises(OAuthError):check_redirect(uri)
        self.assertEqual(check_redirect("http://localhost:7708"+CALLBACK),"http://localhost:7708"+CALLBACK)
        with self.assertRaises(OAuthError):self.connection.configure({"installed":CLIENT["web"]})
        with self.assertRaises(OAuthError):self.connection.begin("https://different.example.com"+CALLBACK,"session",1000)

    def test_connection_api_requires_login_csrf_and_never_returns_secrets(self):
        settings=Settings(database=self.store.path,password="fixture-admin-password")
        client=TestClient(create_app(settings,start_worker=False))
        try:
            self.assertEqual(client.get("/api/google/status").status_code,401)
            client.post("/api/login",json={"password":settings.password})
            bootstrap=client.get("/api/bootstrap").json()
            self.assertEqual(client.post("/api/google/client",json={"config":CLIENT}).status_code,403)
            headers={"X-CSRF-Token":bootstrap["csrf_token"]}
            result=client.post("/api/google/client",json={"config":CLIENT},headers=headers)
            self.assertEqual(result.status_code,200)
            self.assertNotIn("fixture-client-secret",result.text)
            self.assertNotIn("fixture-client-secret",client.get("/api/bootstrap").text)
            self.assertEqual(client.post("/api/google/start",json={"redirect_uri":URI},headers=headers).status_code,422)
            self.assertEqual(client.get("/guide/google").status_code,200)
        finally:
            client.close()
