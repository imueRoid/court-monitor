import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import App, result_report, verify_ocr


class ViewTests(unittest.TestCase):
    def test_diagnostic_write_failure_is_handled_without_console(self):
        with patch("app.CaptchaRecognizer") as reader, patch.object(Path, "read_bytes", return_value=b"image"), \
             patch.object(Path, "write_text", side_effect=FileNotFoundError("blocked")), patch("app.sys.stderr", None):
            reader.return_value.recognize.return_value = "605050"
            self.assertEqual(verify_ocr(Path("image.png"), Path("missing/report.json")), 1)

    def test_only_requested_summary_is_displayed_and_exported(self):
        # 실행파일 UI와 같은 실제 Tk 위젯으로, 네트워크를 사용하지 않고 검증한다.
        sample = {"query": {"court": "가상법원", "case_number": "2025고합1", "party": "가상인"},
                  "queried_at": "2026-11-24T12:00:00+09:00",
                  "general": {"data": {"new_server_field": "원문 그대로", "dma_csBasCtt": {
                      "btprtUltmtThrstCtt": "유기징역 2년\n집행유예 4년", "btprtUltmtYmd": "20261002"}}},
                  "progress": [], "outcomes": [], "labels": {}, "warnings": []}
        with tempfile.TemporaryDirectory() as history_folder, patch.object(App, "initialize", lambda self: None):
            app = App(history_path=Path(history_folder) / "history.json")
            app.withdraw()
            try:
                app.show_result(sample)
                view = app.views["조회 결과"].get("1.0", "end")
                for text in ("판결 주문", "선고일자", "상소제기 여부", "상소사건번호", "유기징역 2년", "2026.10.02"):
                    self.assertIn(text, view)
                for text in ("btprtUltmtThrstCtt", "general.", "관련 필드", "원문 그대로"):
                    self.assertNotIn(text, view)
                self.assertEqual(len(app.tabs.tabs()), 2)
                self.assertEqual(len(app.history.records), 1)
                self.assertFalse(hasattr(app, "captcha_label"))
                with tempfile.TemporaryDirectory() as folder:
                    path = Path(folder) / "테스트결과.json"
                    with patch("app.filedialog.asksaveasfilename", return_value=str(path)):
                        app.export()
                    exported = json.loads(path.read_text("utf-8"))
                    self.assertEqual(set(exported), {"query", "summary", "queried_at"})
                    self.assertEqual(len(exported["summary"]), 4)
                self.assertNotIn("원문 그대로", result_report(sample))
            finally:
                app.destroy()

    def test_startup_does_not_fetch_captcha(self):
        initialize = App.initialize
        with tempfile.TemporaryDirectory() as folder, patch.object(App, "initialize", lambda self: None):
            app = App(history_path=Path(folder) / "history.json")
            app.withdraw()
            try:
                with patch("app.CourtClient") as factory:
                    client = factory.return_value
                    client.initialize.return_value = {"서울중앙지방법원": "000210"}
                    app._work = lambda fn, done, failure=None: done(fn())
                    initialize(app)
                    client.captcha.assert_not_called()
                    client.search_auto.assert_not_called()
                    self.assertTrue(app.ready)
            finally:
                app.destroy()

    def test_history_selection_shows_saved_result_and_starts_fresh_lookup(self):
        sample = {"query": {"court": "서울중앙지방법원", "case_number": "2024가단1", "party": "가상인"},
                  "summary": {"판결 주문": "원고승", "선고일자": "2026.10.01", "상소제기 여부": "확인되지 않음", "상소사건번호": "확인되지 않음"}}
        with tempfile.TemporaryDirectory() as folder, patch.object(App, "initialize", lambda self: None):
            app = App(history_path=Path(folder) / "history.json")
            app.withdraw()
            try:
                app.show_result(sample)
                app.ready = True
                key = next(iter(app.history.records))
                with patch.object(app, "search") as search:
                    app.open_history_case(key)
                    search.assert_called_once_with(keep_cached=True)
                self.assertEqual(app.number.get(), "2024가단1")
                self.assertIn("원고승", app.views["조회 결과"].get("1.0", "end"))
            finally:
                app.destroy()


if __name__ == "__main__":
    unittest.main()
