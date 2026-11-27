from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from server.config import Settings
from server.domain import Hearing, calendar_hearings, event_payload, extract_hearings, next_hearing, schedule, snapshot, extract_progress, progress_interval, has_verdict_date, normalize_observed


ZONE = ZoneInfo("Asia/Seoul")


def moment(hour=11, day=24):
    return datetime(2026, 11, day, hour, 0, tzinfo=ZONE)


def observed(kind="선고", day="2026-11-24", clock="10:00", order="확인되지 않음"):
    return {"summary": {"판결 주문": order, "선고일자": day.replace("-", ".") if order != "확인되지 않음" else "확인되지 않음",
                        "상소제기 여부": "확인되지 않음", "상소사건번호": "확인되지 않음"},
            "hearings": [{"day": day, "clock": clock, "kind": kind, "location": "101호 법정", "result": ""}], "warnings": []}


class DomainTests(unittest.TestCase):
    def test_progress_normalizes_official_fields_deduplicates_and_sorts(self):
        row = {"progYmd": "20261123", "progCtt": "<b>준비서면</b><br>접수", "progRslt": "완료"}
        result = {"general": {"data": {"dlt_hrngProgCurst": [row]}},
                  "progress": [{"data": {"dlt_csProgCtt": [
                      row, {"progYmd": "20261124", "progCtt1": "통지서", "progCtt2": "송달", "progCtt3": "송달"},
                      {"progYmd": "invalid", "progCtt": "제외"},
                      {"progYmd": "20261124", "secret": "노출 금지"}]}}]}
        progress = extract_progress(result)
        self.assertEqual(progress, [
            {"day": "2026-11-24", "content": "통지서 송달", "result": ""},
            {"day": "2026-11-23", "content": "준비서면\n접수", "result": "완료"}])
        self.assertEqual(snapshot(result)["progress"], progress)

    def test_progress_interval_uses_previous_and_next_hearing_not_row_limit(self):
        data = {"summary": {}, "hearings": [
            {"day": "2026-11-20", "clock": "10:00", "kind": "변론"},
            {"day": "2026-11-26", "clock": "10:00", "kind": "변론"},
            {"day": "2026-12-01", "clock": "10:00", "kind": "변론"}],
            "progress": [{"day": day, "content": "접수", "result": ""}
                         for day in ("2026-12-01", "2026-11-26", "2026-11-23", "2026-11-20", "2026-11-19")]}
        interval = progress_interval(data, moment(), "Asia/Seoul")
        self.assertEqual(interval["previous"]["day"], "2026-11-20")
        self.assertEqual(interval["next"]["day"], "2026-11-26")
        self.assertEqual([row["day"] for row in interval["entries"]], ["2026-11-23", "2026-11-20"])
        data["hearings"] = data["hearings"][:1]
        interval = progress_interval(data, moment(), "Asia/Seoul")
        self.assertIsNone(interval["next"])
        self.assertEqual([row["day"] for row in interval["entries"]], ["2026-11-23", "2026-11-20"])
        data["hearings"] = []
        self.assertEqual([row["day"] for row in progress_interval(data, moment(), "Asia/Seoul")["entries"]],
                         ["2026-11-23", "2026-11-20", "2026-11-19"])

    def test_no_previous_hearing_shows_all_actions_from_beginning_without_count_limit(self):
        data = {"hearings": [{"day": "2026-11-24", "clock": "14:00", "kind": "변론"}],
                "progress": [{"day": "2026-11-24", "content": "다음 기일 당일", "result": ""},
                             {"day": "2026-11-23", "content": "미래 기일 전 제출", "result": "접수"}] +
                            [{"day": "2026-10-01", "content": f"서류 제출 {index}", "result": ""}
                             for index in range(25)] +
                            [{"day": "2026-09-01", "content": "소장 접수", "result": ""},
                             {"day": "2026-09-02", "content": "소장 부본 송달", "result": "도달"}]}
        interval = progress_interval(data, moment(), "Asia/Seoul")
        self.assertIsNone(interval["previous"])
        self.assertEqual(len(interval["entries"]), 28)
        self.assertTrue(all(row["day"] < "2026-11-24" for row in interval["entries"]))
        self.assertIn("소장 부본 송달", [row["content"] for row in interval["entries"]])
        data["hearings"] = []
        interval = progress_interval(data, moment(), "Asia/Seoul")
        self.assertEqual(len(interval["entries"]), 29)
        self.assertIsNone(interval["next"])

    def test_today_hearing_moves_boundary_only_after_its_time(self):
        data = {"hearings": [{"day": "2026-11-23", "clock": "10:00", "kind": "공판"},
                             {"day": "2026-11-24", "clock": "14:00", "kind": "공판"}]}
        before = progress_interval(data, moment(11), "Asia/Seoul")
        self.assertEqual(before["previous"]["day"], "2026-11-23")
        self.assertEqual(before["next"]["day"], "2026-11-24")
        after = progress_interval(data, moment(15), "Asia/Seoul")
        self.assertEqual(after["previous"]["day"], "2026-11-24")
        self.assertIsNone(after["next"])

    def test_scheduled_and_actual_verdict_dates_hide_progress(self):
        self.assertFalse(has_verdict_date({"summary": {"선고일자": "확인되지 않음"}}))
        self.assertTrue(has_verdict_date({"summary": {"선고일자": "2026.11.24"}}))
        self.assertTrue(has_verdict_date(observed(day="2026-11-30")))

    def test_progress_cancellation_removes_structured_verdict_and_saved_schedule(self):
        for row in ({"progYmd": "20261124", "progCtt": "선고기일(10:00)", "progRslt": "취소"},
                    {"progYmd": "20261124", "progCtt": "선고기일(10:00) 변경"}):
            with self.subTest(row=row):
                result = {"general": {"data": {"dlt_rcntDxdyLst": [
                    {"dxdyYmd": "20261124", "dxdyHm": "1000", "dxdyKndNm": "선고기일"}]}},
                    "progress": [{"data": {"dlt_csProgCtt": [row]}}]}
                data = snapshot(result)
                self.assertEqual(data["hearings"], [])
                self.assertEqual(data["cancelled_verdict_days"], ["2026-11-24"])
                self.assertFalse(has_verdict_date(data))
                plan = schedule(Settings(), moment(), data, {"verdict_day": "2026-11-24"})
                self.assertEqual((plan["state"], plan["verdict_day"]), ("monitoring", ""))
                self.assertEqual(calendar_hearings(data, moment(), "Asia/Seoul"), [])
                self.assertEqual(data["progress"][0]["day"], "2026-11-24")

    def test_no_hearing_cancellation_supersedes_older_scheduled_verdict_without_next_hearing(self):
        result = {"general": {"data": {
            "dma_csBasCtt": {"jdgmnPrnYmd": "20261120", "jdgmnOrdCtt": "원고승"},
            "dlt_rcntDxdyLst": [
                {"dxdyYmd": "20261120", "dxdyKndNm": "판결선고기일"},
                {"dxdyYmd": "20261124", "dxdyKndNm": "판결선고기일", "dxdyRsltNm": "무변론판결취소"},
            ],
        }}}
        data = snapshot(result)
        self.assertEqual(data["no_hearing_cancelled_through"], "2026-11-24")
        self.assertFalse(has_verdict_date(data))
        self.assertIsNone(next_hearing(data, moment(), "Asia/Seoul"))
        plan = schedule(Settings(), moment(), data, {"verdict_day": "2026-11-20"})
        self.assertEqual((plan["state"], plan["verdict_day"]), ("monitoring", ""))

    def test_later_trial_supersedes_old_verdict_but_keeps_new_verdict(self):
        data = observed()
        data["hearings"].append({"day": "2026-11-26", "clock": "14:00", "kind": "공판"})
        self.assertFalse(has_verdict_date(data))
        self.assertEqual(next_hearing(data, moment(8), "Asia/Seoul").kind, "공판")
        self.assertEqual(schedule(Settings(), moment(), data, {"verdict_day": "2026-11-24"})["verdict_day"], "")
        data["hearings"].append({"day": "2026-12-10", "clock": "14:00", "kind": "선고"})
        normalized = normalize_observed(data)
        self.assertEqual([h["day"] for h in normalized["hearings"] if h["kind"] == "선고"], ["2026-12-10"])
        self.assertTrue(has_verdict_date(normalized))
        self.assertEqual(schedule(Settings(), moment(), normalized, {})["verdict_day"], "2026-12-10")
        self.assertEqual(normalize_observed(normalized), normalized)

    def test_confirmed_judgment_is_preserved_despite_later_trial_date(self):
        data = observed(order="원고패")
        data["hearings"].append({"day": "2026-12-01", "clock": "14:00", "kind": "변론"})
        normalized = normalize_observed(data)
        self.assertEqual(normalized["summary"]["판결 주문"], "원고패")
        self.assertEqual(normalized["summary"]["선고일자"], "2026.11.24")
        self.assertEqual(schedule(Settings(), moment(), normalized, {})["state"], "done")

    def test_structured_hearings_and_cancelled_hearing(self):
        result = {"general": {"data": {"dlt_rcntDxdyLst": [
            {"dxdyYmd": "20261125", "dxdyHm": "0930", "dxdyKndNm": "변론기일", "dxdyPlcNm": "101호", "dxdyRsltNm": "변경"},
            {"dxdyYmd": "20261201", "dxdyHm": "1400", "dxdyKndNm": "선고기일", "dxdyPlcNm": "102호"}]}},
            "progress": [{"data": {"dlt_csProgCtt": [{"progYmd": "20261125", "progCtt": "변론기일(09:30)", "progRslt": ""},
                {"progYmd": "20261126", "progCtt": "선고기일통지서 발송"}]}}]}
        hearings = extract_hearings(result)
        self.assertEqual(len(hearings), 1)
        self.assertEqual(hearings[0], Hearing("2026-12-01", "14:00", "선고", "102호", ""))

    def test_progress_fallback_and_timed_structured_entry_wins(self):
        result = {"general": {"data": {"dlt_rcntDxdyLst": [{"dxdyYmd": "20261124", "dxdyHm": "1030", "dxdyKndNm": "선고기일", "dxdyPlcNm": "101호"}]}},
                  "progress": [{"data": {"dlt_csProgCtt": [{"progYmd": "20261124", "progCtt": "선고기일(10:30)"},
                      {"progYmd": "20261125", "progCtt": "변론기일(11:20)"}]}}]}
        hearings = extract_hearings(result)
        self.assertEqual(len(hearings), 2)
        self.assertEqual(hearings[0].location, "101호")
        self.assertEqual(hearings[1].clock, "11:20")

    def test_verdict_retries_and_daily_cap_survive_restart(self):
        settings = Settings()
        plan = schedule(settings, moment(), observed(), {"verdict_count": 7, "verdict_count_day": "2026-11-24"})
        self.assertEqual(plan["verdict_count"], 8)
        self.assertEqual(datetime.fromtimestamp(plan["next_check"], ZONE), moment(9, 25))
        restarted = schedule(settings, moment(9, 25), observed(), plan)
        self.assertEqual(restarted["verdict_count"], 1)
        self.assertEqual(datetime.fromtimestamp(restarted["next_check"], ZONE), moment(9, 25) + timedelta(minutes=30))

    def test_before_scheduled_verdict_waits_until_hearing_plus_delay(self):
        plan = schedule(Settings(), moment(8), observed(clock="14:00"), {})
        self.assertEqual(plan["verdict_count"], 0)
        self.assertEqual(datetime.fromtimestamp(plan["next_check"], ZONE), moment(14) + timedelta(minutes=30))

    def test_confirmed_order_stops_court_polling(self):
        plan = schedule(Settings(), moment(), observed(order="원고승"), {})
        self.assertEqual(plan["state"], "done")
        self.assertIsNone(plan["next_check"])

    def test_future_hearing_and_baseline(self):
        plan = schedule(Settings(), moment(), observed(kind="변론", day="2026-11-26"), {})
        self.assertEqual(datetime.fromtimestamp(plan["next_check"], ZONE), moment() + timedelta(hours=24))

    def test_next_day_hearing_is_added_after_today_hearing(self):
        data = observed(kind="변론", clock="09:00")
        data["hearings"].append({"day": "2026-11-25", "clock": "11:00", "kind": "변론", "location": "", "result": ""})
        dates = [h.day for h in calendar_hearings(data, moment(15), "Asia/Seoul")]
        self.assertEqual(dates, ["2026-11-24", "2026-11-25"])

    def test_calendar_timed_event_and_order_escape(self):
        case = {"id": "case-1", "court": "가상법원", "case_number": "2024가단1", "party": "가상인"}
        data = observed(order="원고승 & <script>example</script>")
        payload = event_payload(case, Hearing(**data["hearings"][0]), data["summary"], "Asia/Seoul")
        self.assertIn("+09:00", payload["start"]["dateTime"])
        self.assertIn("&lt;script&gt;", payload["description"])
        self.assertIn("선고·주문 확인", payload["summary"])
        self.assertNotIn("attendees", payload)

    def test_all_day_event_exclusive_end(self):
        case = {"id": "x", "court": "가상법원", "case_number": "2024가단1", "party": "가상인"}
        payload = event_payload(case, Hearing("2026-11-24", "", "변론"), {}, "Asia/Seoul")
        self.assertEqual(payload["end"], {"date": "2026-11-25"})

    def test_next_hearing_skips_already_passed_time_today(self):
        data = observed(kind="변론", clock="09:00")
        data["hearings"].append({"day": "2026-11-25", "clock": "10:00", "kind": "변론", "location": "", "result": ""})
        self.assertEqual(next_hearing(data, moment(15), "Asia/Seoul").day, "2026-11-25")
