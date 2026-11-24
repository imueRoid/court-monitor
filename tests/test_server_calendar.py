from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from server.config import Settings
from server.google_calendar import CalendarError, GoogleCalendar
from server.prepare import prepare


def response(status):
    return Mock(status_code=status, ok=200 <= status < 300, headers={}, json=Mock(return_value={"id": "event"}))


class CalendarTests(unittest.TestCase):
    def test_single_file_compose_can_supply_credentials_json_without_a_file(self):
        settings = Settings(calendar_id="dedicated@example.com", credentials_file=Path("does-not-exist.json"),
                            credentials_json='{"type":"service_account"}')
        calendar = GoogleCalendar(settings)
        self.assertTrue(calendar.configured)
        self.assertEqual(calendar._credential_info(), {"type":"service_account"})

    def test_insert_conflict_uses_same_id_and_updates_instead_of_duplicate(self):
        calendar = GoogleCalendar(Settings(calendar_id="dedicated@example.com"))
        session = Mock()
        session.patch.side_effect = [response(404), response(200)]
        session.post.return_value = response(409)
        calendar.session = session
        calendar.upsert("a1234", {"summary": "판결선고"})
        self.assertEqual(session.post.call_args.kwargs["json"]["id"], "a1234")
        self.assertEqual(session.patch.call_count, 2)

    def test_permission_error_has_actionable_message(self):
        calendar = GoogleCalendar(Settings(calendar_id="dedicated@example.com"))
        calendar.session = Mock()
        calendar.session.patch.return_value = response(403)
        with self.assertRaisesRegex(CalendarError, "쓰기 권한"):
            calendar.upsert("a1234", {})

    def test_google_retry_after_is_preserved(self):
        calendar = GoogleCalendar(Settings(calendar_id="dedicated@example.com"))
        calendar.session = Mock()
        limited = response(429)
        limited.headers = {"Retry-After": "900"}
        calendar.session.patch.return_value = limited
        with self.assertRaises(CalendarError) as error:
            calendar.upsert("a1234", {})
        self.assertEqual(error.exception.retry_after, 900)

    def test_patch_clears_incompatible_date_fields_without_mutating_payload(self):
        calendar = GoogleCalendar(Settings(calendar_id="dedicated@example.com"))
        calendar.session = Mock()
        calendar.session.patch.return_value = response(200)
        for value, removed in (({"date": "2026-11-24"}, "dateTime"),
                               ({"dateTime": "2026-11-24T10:00:00+09:00", "timeZone": "Asia/Seoul"}, "date")):
            payload = {"start": dict(value), "end": dict(value)}
            calendar.upsert("a1234", payload)
            sent = calendar.session.patch.call_args.kwargs["json"]
            self.assertIn(removed, sent["start"])
            self.assertIsNone(sent["start"][removed])
            self.assertEqual(payload["start"], value)

    def test_prepare_preserves_existing_credentials_and_password(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".env.example").write_text("GOOGLE_CALENDAR_ID=\n", "utf-8")
            password = prepare(root)
            first = password.read_text("utf-8")
            prepare(root)
            self.assertEqual(password.read_text("utf-8"), first)
            self.assertGreaterEqual(len(first.strip()), 32)
