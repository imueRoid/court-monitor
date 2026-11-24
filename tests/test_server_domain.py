from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from server.config import Settings
from server.domain import Hearing, calendar_hearings, event_payload, extract_hearings, next_hearing, schedule, snapshot


ZONE = ZoneInfo("Asia/Seoul")


def moment(hour=11, day=24):
    return datetime(2026, 11, day, hour, 0, tzinfo=ZONE)


def observed(kind="선고", day="2026-11-24", clock="10:00", order="확인되지 않음"):
    return {"summary": {"판결 주문": order, "선고일자": day.replace("-", ".") if order != "확인되지 않음" else "확인되지 않음",
                        "상소제기 여부": "확인되지 않음", "상소사건번호": "확인되지 않음"},
            "hearings": [{"day": day, "clock": clock, "kind": kind, "location": "101호 법정", "result": ""}], "warnings": []}


class DomainTests(unittest.TestCase):
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
