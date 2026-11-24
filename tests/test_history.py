import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from case_history import CaseHistory, HistoryError


def sample(number="2024가단1", party="가상인", terminal="원고승"):
    return {"query": {"court": "서울중앙지방법원", "case_number": number, "party": party},
            "queried_at": "2026-11-24T12:00:00+09:00",
            "general": {"data": {"dma_csBasCtt": {"csUltmtDvsNm": terminal, "csUltmtYmd": "20261002", "encCsNo": "not-for-storage"}}}}


class HistoryTests(unittest.TestCase):
    def test_restart_keeps_all_distinct_cases_and_updates_existing_case(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "app-data" / "history.json"
            store = CaseHistory(path)
            store.save_result(sample())
            store.save_result(sample("2024가단2"))
            store.save_result(sample(terminal="원고패"))
            new_store = CaseHistory(path)
            new_store.load()
            self.assertEqual(len(new_store.records), 2)
            self.assertIn("원고패", [r["summary"]["판결 주문"] for r in new_store.records.values()])
            self.assertNotIn("encCsNo", path.read_text("utf-8"))
            self.assertNotIn("not-for-storage", path.read_text("utf-8"))

    def test_same_case_with_different_party_is_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            store = CaseHistory(Path(folder) / "history.json")
            store.save_result(sample())
            store.save_result(sample(party="다른인"))
            self.assertEqual(len(store.records), 2)

    def test_failed_atomic_replace_preserves_previous_history(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.json"
            store = CaseHistory(path)
            store.save_result(sample())
            original = path.read_bytes()
            with patch("case_history.os.replace", side_effect=PermissionError("blocked")):
                with self.assertRaises(HistoryError):
                    store.save_result(sample("2024가단2"))
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(folder).glob("*.tmp")), [])

    def test_corrupt_existing_file_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.json"
            path.write_text("broken json", "utf-8")
            with self.assertRaises(HistoryError):
                CaseHistory(path).save_result(sample())
            self.assertEqual(path.read_text("utf-8"), "broken json")

    def test_summary_export_can_be_imported(self):
        with tempfile.TemporaryDirectory() as folder:
            store = CaseHistory(Path(folder) / "history.json")
            record = sample()
            record.pop("general")
            record["summary"] = {"판결 주문": "원고승", "선고일자": "2026.10.02", "상소제기 여부": "확인되지 않음", "상소사건번호": "확인되지 않음"}
            store.save_result(json.loads(json.dumps(record)))
            self.assertEqual(next(iter(store.records.values()))["summary"]["판결 주문"], "원고승")


if __name__ == "__main__":
    unittest.main()
