from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from server.config import Settings
from server.google_calendar import CalendarError
from server.store import Store
from server.worker import Worker
from test_server_domain import observed


class StoreTests(unittest.TestCase):
    def test_detail_refresh_cooldown_survives_restart_and_does_not_duplicate_running_lookup(self):
        self.assertEqual(self.store.request_detail_refresh(self.key, self.now)["reason"], "requested")
        claim = self.store.claim_case(self.now)
        self.assertEqual(self.store.request_detail_refresh(self.key, self.now + 1)["reason"], "pending")
        self.store.fail_case(self.key, claim["lease_token"], "일시 오류", self.now + 2)
        restored = Store(self.path)
        self.assertEqual(restored.request_detail_refresh(self.key, self.now + 3)["reason"], "cooldown")
        self.assertEqual(restored.request_detail_refresh(self.key, self.now + 61)["reason"], "requested")
        self.assertIsNone(restored.request_detail_refresh("missing", self.now))

    def test_fresh_progress_is_reused_and_failed_refresh_keeps_saved_history(self):
        data = observed(kind="변론")
        data["progress"] = [{"day": "2026-11-23", "content": "준비서면 접수", "result": ""}]
        self.complete(data)
        self.assertEqual(self.store.request_detail_refresh(self.key, self.now + 1)["reason"], "fresh")
        self.assertEqual(self.store.request_detail_refresh(self.key, self.now + 61)["reason"], "requested")
        claim = self.store.claim_case(self.now + 61)
        self.store.fail_case(self.key, claim["lease_token"], "조회 실패", self.now + 62)
        self.assertEqual(Store(self.path).list_cases()[0]["observed"]["progress"], data["progress"])
        self.complete(observed(order="원고패"), self.now + 100)
        self.assertEqual(self.store.request_detail_refresh(self.key, self.now + 101)["reason"], "order")

    def test_cancelled_verdict_clears_saved_verdict_and_survives_restart(self):
        self.complete(observed())
        self.assertEqual(self.store.list_cases()[0]["state"], "awaiting_order")
        data = observed(kind="공판", day="2026-12-01")
        data["cancelled_hearings"] = [{"day": "2026-11-24", "kind": "선고"}]
        data["progress"] = [{"day": "2026-11-24", "content": "선고기일 취소", "result": "취소"}]
        self.complete(data, self.now + 61)
        record = Store(self.path).list_cases()[0]
        self.assertEqual(record["state"], "monitoring")
        self.assertEqual(record["verdict_day"], "")
        self.assertEqual(record["observed"]["cancelled_verdict_days"], ["2026-11-24"])
        self.assertEqual(record["observed"]["progress"], data["progress"])
        self.assertEqual(self.store.request_detail_refresh(self.key, self.now + 122)["reason"], "requested")

    def test_cancelled_past_verdict_calendar_event_is_updated_not_left_as_active(self):
        self.complete(observed())
        event = self.store.claim_event(self.now)
        self.store.event_synced(event, self.now)
        data = observed(kind="공판", day="2026-12-01")
        data["cancelled_hearings"] = [{"day": "2026-11-24", "kind": "선고"}]
        self.complete(data, self.now + 7200)
        pending = self.store.claim_event(self.now + 7200)
        payload = json.loads(pending["payload"])
        self.assertTrue(payload["summary"].startswith("[취소·변경]"))
        self.assertIn("기존 선고기일을 제외", payload["description"])
        self.assertEqual(self.store.list_cases()[0]["state"], "monitoring")
        self.store.event_synced(pending, self.now + 7200)
        self.complete(data, self.now + 7261)
        stored = self.store.case_events(self.key)
        self.assertEqual(sum(json.loads(row["payload"])["summary"].startswith("[취소·변경]") for row in stored), 1)

    def test_existing_database_upgrades_without_losing_cases_or_schedules(self):
        with self.store.db(True) as db:
            db.execute("ALTER TABLE cases DROP COLUMN detail_refresh_at")
        restored = Store(self.path)
        record = restored.list_cases()[0]
        self.assertEqual((record["id"], record["next_check"]), (self.key, self.now))
        self.assertEqual(restored.request_detail_refresh(self.key, self.now)["reason"], "requested")

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "cases.sqlite3"
        self.store = Store(self.path)
        self.settings = Settings(database=self.path, password="a-long-fixture-password")
        self.now = datetime(2026, 11, 24, 9, tzinfo=ZoneInfo("Asia/Seoul")).timestamp()
        self.key = self.store.add_case("가상법원", "2024가단1", "가상인", self.now)

    def tearDown(self):
        self.folder.cleanup()

    def complete(self, data, now=None):
        now = self.now if now is None else now
        self.store.request_refresh(self.key, now)
        claim = self.store.claim_case(now)
        self.store.complete(self.key, claim["lease_token"], data, self.settings, now)

    def test_single_claim_and_stale_lease_cannot_overwrite(self):
        first = self.store.claim_case(self.now)
        second_store = Store(self.path)
        self.assertIsNone(second_store.claim_case(self.now))
        second = second_store.claim_case(self.now + 601)
        self.assertFalse(self.store.complete(self.key, first["lease_token"], observed(), self.settings, self.now + 602))
        self.assertTrue(second_store.complete(self.key, second["lease_token"], observed(), self.settings, self.now + 602))

    def test_delete_removes_events_logs_and_ignores_late_worker_results_after_restart(self):
        self.complete(observed())
        event = self.store.claim_event(self.now)
        self.store.request_refresh(self.key, self.now)
        claim = self.store.claim_case(self.now)
        other = self.store.add_case("가상법원", "2024가단2", "남은당사자", self.now)
        self.assertEqual(self.store.delete_cases([self.key, self.key]), 1)
        self.assertFalse(self.store.complete(self.key, claim["lease_token"], observed(), self.settings, self.now + 1))
        self.store.fail_case(self.key, claim["lease_token"], "뒤늦은 오류", self.now + 1)
        self.store.event_failed(event, "뒤늦은 오류", self.now + 1)
        self.store.event_synced(event, self.now + 1)
        restored = Store(self.path)
        self.assertEqual([r["id"] for r in restored.list_cases()], [other])
        self.assertEqual(restored.case_events(self.key), [])
        self.assertEqual(restored.logs(), [])
        self.assertIsNone(restored.claim_event(self.now + 86400))

    def test_next_hearing_sort_precedes_pagination_and_puts_missing_dates_last(self):
        moment = datetime.fromtimestamp(self.now, ZoneInfo("Asia/Seoul"))
        hearings = [
            [("2026-11-24", "10:00")],
            [("2026-11-24", "11:00")],
            [("2026-11-25", "10:00")],
            [("2026-11-23", "10:00")],
            [("2026-11-24", "08:00"), ("2026-11-25", "12:00")],
            [("2026-11-24", "")],
            [("2026-11-24", "09:00")],
        ]
        keys = []
        for number, dates in enumerate(hearings, 2):
            key = self.store.add_case("가상법원", f"2024가단{number}", "정렬당사자", self.now + number)
            keys.append(key)
            snapshot = {"summary": {}, "hearings": [{"day": day, "clock": clock, "kind": "변론"} for day, clock in dates]}
            with self.store.db(True) as db:
                db.execute("UPDATE cases SET snapshot=? WHERE id=?", (json.dumps(snapshot), key))
        expected = [keys[5], keys[6], keys[0], keys[1], keys[2], keys[4], keys[3], self.key]
        actual = []
        for page in range(1, 5):
            result = self.store.case_page(page=page, page_size=2, now=moment)
            actual.extend(row["id"] for row in result["cases"])
        self.assertEqual(actual, expected)
        searched = self.store.case_page(query="정렬당사자", page_size=2, now=moment)
        self.assertEqual([row["id"] for row in searched["cases"]], expected[:2])
        # Once a hearing time passes, the next future hearing determines its position.
        later = self.store.case_page(page_size=20, now=moment.replace(hour=10, minute=30))
        self.assertEqual([row["id"] for row in later["cases"]][:4], [keys[5], keys[1], keys[2], keys[4]])

    def test_manual_refresh_during_running_job_is_preserved(self):
        job = self.store.claim_case(self.now)
        self.store.request_refresh(self.key, self.now + 1)
        self.store.complete(self.key, job["lease_token"], observed(), self.settings, self.now + 2)
        self.assertIsNotNone(self.store.claim_case(self.now + 2))

    def test_pause_and_one_manual_refresh_do_not_resume_auto_polling(self):
        self.store.set_active(self.key, False, self.now)
        self.assertIsNone(self.store.claim_case(self.now))
        self.store.request_refresh(self.key, self.now)
        job = self.store.claim_case(self.now)
        self.store.complete(self.key, job["lease_token"], observed(), self.settings, self.now)
        self.assertIsNone(self.store.claim_case(self.now + 86400))
        self.assertEqual(self.store.list_cases()[0]["active"], 0)

    def test_same_verdict_event_is_updated_with_order_after_restart(self):
        self.complete(observed())
        event = self.store.claim_event(self.now)
        self.store.event_synced(event, self.now)
        self.store = Store(self.path)
        self.complete(observed(order="원고승"), self.now + 3600)
        events = self.store.case_events(self.key)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["id"], event["id"])
        self.assertIn("원고승", json.loads(events[0]["payload"])["description"])
        self.assertEqual(self.store.list_cases()[0]["state"], "done")

    def test_future_hearing_reschedule_moves_existing_event(self):
        self.complete(observed(kind="변론", day="2026-11-25"))
        original = self.store.case_events(self.key)[0]["id"]
        self.complete(observed(kind="변론", day="2026-11-26"), self.now + 60)
        events = self.store.case_events(self.key)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["id"], original)
        self.assertIn("2026-11-26", events[0]["signature"])

    def test_unchanged_snapshot_does_not_write_calendar_again(self):
        self.complete(observed())
        event = self.store.claim_event(self.now)
        self.store.event_synced(event, self.now)
        self.complete(observed(), self.now + 60)
        self.assertIsNone(self.store.claim_event(self.now + 60))

    def test_failed_court_lookup_keeps_last_result_and_pending_calendar(self):
        self.complete(observed())
        old = self.store.list_cases()[0]["observed"]
        self.store.request_refresh(self.key, self.now + 60)
        job = self.store.claim_case(self.now + 60)
        self.store.fail_case(self.key, job["lease_token"], "연결 오류", self.now + 60)
        row = self.store.list_cases()[0]
        self.assertEqual(row["observed"], old)
        self.assertEqual(row["calendar_pending"], 1)
        self.assertGreater(row["next_check"], self.now + 60)

    def test_calendar_failure_retries_without_court_refetch(self):
        self.complete(observed(order="원고승"))
        calendar = Mock(configured=True, calendar_id="")
        calendar.upsert.side_effect = CalendarError("temporary error")
        factory = Mock()
        worker = Worker(self.store, self.settings, calendar=calendar, client_factory=factory)
        worker.tick(self.now)
        self.assertEqual(self.store.list_cases()[0]["calendar_pending"], 1)
        calendar.upsert.side_effect = None
        worker = Worker(Store(self.path), self.settings, calendar=calendar, client_factory=factory)
        worker.tick(self.now + 301)
        self.assertEqual(self.store.list_cases()[0]["calendar_pending"], 0)
        factory.assert_not_called()

    def test_registration_is_deduplicated(self):
        duplicate = self.store.add_case("가상법원", "2024가단1", "가상인", self.now)
        self.assertEqual(duplicate, self.key)
        self.assertEqual(len(self.store.list_cases()), 1)

    def test_restart_after_watch_hours_defers_verdict_job(self):
        self.complete(observed())
        late = datetime(2026, 11, 24, 22, tzinfo=ZoneInfo("Asia/Seoul")).timestamp()
        self.assertIsNone(Store(self.path).claim_case(late, self.settings))
        due = self.store.list_cases()[0]["next_check"]
        self.assertEqual(datetime.fromtimestamp(due, ZoneInfo("Asia/Seoul")).hour, 9)

    def test_failed_verdict_check_counts_toward_daily_limit(self):
        self.complete(observed())
        self.store.request_refresh(self.key, self.now + 3600)
        job = self.store.claim_case(self.now + 3600)
        self.store.fail_case(self.key, job["lease_token"], "인증 실패", self.now + 3600, self.settings)
        self.assertEqual(self.store.list_cases()[0]["verdict_count"], 1)

    def test_two_valid_hearings_on_same_day_are_not_overwritten(self):
        data = observed(kind="변론", day="2026-11-25")
        data["hearings"].append({**data["hearings"][0], "clock": "11:00"})
        self.complete(data)
        self.assertEqual(len(self.store.case_events(self.key)), 2)

    def test_future_hearing_type_change_moves_obsolete_event(self):
        self.complete(observed(kind="변론", day="2026-11-25"))
        key = self.store.case_events(self.key)[0]["id"]
        self.complete(observed(kind="선고", day="2026-11-26"), self.now + 60)
        events = self.store.case_events(self.key)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["id"], key)
        self.assertIn("선고", events[0]["signature"])
