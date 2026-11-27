from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


@unittest.skipUnless(sys.platform == "win32", "Native Windows Tk UI")
class DesktopUiTests(unittest.TestCase):
    def setUp(self):
        from desktop_monitor import MonitorApp
        self.folder = tempfile.TemporaryDirectory()
        self.startup = Mock()
        self.startup.enabled.return_value = False
        self.app = MonitorApp(self.folder.name, start_worker=False, with_tray=False,
                              startup_manager=self.startup, check_updates=False)
        self.app.withdraw()

    def tearDown(self):
        if not self.app.closing:
            self.app.quit_app()
        self.folder.cleanup()

    def test_update_notice_opens_the_versioned_release(self):
        from desktop_updates import release_page

        self.app.after_cancel(self.app._drain_job)
        self.app.events.put(("update", ("1.0.50", False)))
        self.app._drain()
        self.assertEqual(self.app.update_banner.winfo_manager(), "pack")
        self.assertIn("1.0.50", self.app.update_notice.get())
        with patch("desktop_monitor.webbrowser.open") as open_page:
            self.app._open_update()
        open_page.assert_called_once_with(release_page("1.0.50"))
        self.app._show_update_result(None, False)
        self.assertFalse(self.app.update_banner.winfo_manager())

    def test_saved_no_hearing_cancellation_reopens_and_shows_trial_in_progress(self):
        key = self.app.store.add_case("가상법원", "2024가단1", "가상당사자")
        checked = datetime(2026, 2, 2, 1, tzinfo=timezone.utc).timestamp()
        snapshot = {
            "summary": {"선고일자": "2026.01.15"},
            "hearings": [{"day": "2026-01-15", "kind": "선고", "clock": "14:00"}],
            "progress": [{"day": "2026-01-31", "content": "판결선고기일", "result": "무변론판결취소"}],
        }
        with self.app.store.db(True) as db:
            db.execute("UPDATE cases SET state='awaiting_order', verdict_day='2026-01-15', snapshot=? WHERE id=?",
                       (json.dumps(snapshot), key))
        self.app.store.resume_no_hearing_cancelled_cases(self.app.settings, now=checked)
        self.app.refresh()
        self.assertEqual(self.app.tree.item(key, "values")[4:6], ("변론 진행중", "기일 미확인"))
        job = self.app.store.claim_case(checked, self.app.settings)
        self.assertEqual(job["id"], key)
        snapshot.pop("progress")
        self.app.store.complete(key, job["lease_token"], snapshot, self.app.settings, checked)
        self.app.refresh()
        self.assertEqual(self.app.tree.item(key, "values")[4:6], ("변론 진행중", "기일 미확인"))

    def test_preferences_are_in_settings_tab_and_default_off(self):
        self.assertFalse(self.app.auto_start.get())
        self.assertFalse(self.app.background.get())
        self.assertIn(self.app.settings_tab, [self.app.nametowidget(name) for name in self.app.tabs.tabs()])
        self.app.tray = Mock()
        self.app.auto_start.set(True)
        self.app.background.set(False)
        self.app.save_settings()
        self.startup.set.assert_called_with(True)
        self.assertEqual(self.app.store.get_meta("desktop_preferences"), {"auto_start":True, "background":False})

    def test_close_hides_only_when_background_enabled_and_tray_available(self):
        self.app.tray = Mock()
        self.app.preferences = {"background":True}
        self.app.close_window()
        self.assertFalse(self.app.closing)
        self.assertEqual(self.app.state(), "withdrawn")
        self.app.show_window()
        self.assertEqual(self.app.state(), "normal")
        self.app.preferences = {"background":False}
        worker = self.app.worker = Mock()
        self.app.close_window()
        self.assertTrue(self.app.closing)
        worker.stop.assert_called_once()

    def test_tray_failure_does_not_allow_hidden_unreachable_app(self):
        self.app.background.set(True)
        with patch("desktop_monitor.messagebox.showerror") as error:
            self.app.save_settings()
        error.assert_called_once()
        self.assertIsNone(self.app.store.get_meta("desktop_preferences"))
        self.app.preferences = {"background":True}
        self.app.close_window()
        self.assertTrue(self.app.closing)

    def test_search_pagination_order_details_and_bulk_selection(self):
        keys = []
        for number in range(1, 24):
            key = self.app.store.add_case("가상법원", f"2024가단{number}", "가상당사자", now=number)
            keys.append(key)
        with self.app.store.db(True) as db:
            db.execute("UPDATE cases SET snapshot=? WHERE id=?", (json.dumps({"summary":{"판결 주문":"임의 종국결과"}, "hearings":[{"day":"2099-12-01","clock":"10:00","kind":"변론"}]}), keys[0]))
        self.app.refresh()
        self.assertEqual(self.app.tree.get_children()[0], keys[0])
        self.assertEqual(len(self.app.records), 20)
        self.app.selected_ids.add(keys[0])
        self.app.change_page(1)
        self.assertEqual(len(self.app.records), 3)
        self.app.toggle_page()
        self.assertEqual(len(self.app.selected_ids), 4)
        self.app.query.set(" 임意不存在 ")
        self.app.change_page(0)
        self.assertEqual(len(self.app.records), 0)
        self.assertEqual(len(self.app.selected_ids), 4)
        self.app.query.set("임의 종국결과")
        self.app.change_page(0)
        self.app.tree.selection_set(keys[0])
        self.app.show_detail()
        self.assertIn("임의 종국결과", self.app.detail.get("1.0", "end"))
        with patch("desktop_monitor.messagebox.askyesno", return_value=True):
            self.app.delete_selected()
        self.assertEqual(len(self.app.store.list_cases()), 19)
        self.assertFalse(self.app.selected_ids)
        self.assertFalse((Path(self.folder.name) / "credentials.key").exists())
