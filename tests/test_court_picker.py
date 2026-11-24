import tkinter as tk
from tkinter import ttk
import unittest

from court_picker import CourtPicker, matching_courts


COURTS = ("서울중앙지방법원", "서울남부지방법원", "서울고등법원", "부산지방법원", "부산지방법원 동부지원")


class CourtFilterTests(unittest.TestCase):
    def test_matches_any_part_of_name_and_ignores_spaces(self):
        self.assertEqual(matching_courts(COURTS, "남부"), ["서울남부지방법원"])
        self.assertEqual(matching_courts(COURTS, "서울 남부"), ["서울남부지방법원"])
        self.assertEqual(matching_courts(COURTS, "지방법원"), [COURTS[0], COURTS[1], COURTS[3], COURTS[4]])
        self.assertEqual(matching_courts(COURTS, "존재하지않음"), [])


class CourtPickerTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.geometry("520x150")
        self.variable = tk.StringVar(self.root)
        self.picker = CourtPicker(self.root, textvariable=self.variable)
        self.picker.pack(fill="x", padx=10, pady=10)
        self.other = ttk.Entry(self.root)
        self.other.pack()
        self.picker.set_values(COURTS)
        self.picker.set_enabled(True)
        self.root.update()

    def tearDown(self):
        self.root.destroy()

    def test_typing_filters_without_stealing_focus_and_keyboard_selects(self):
        self.picker.entry.focus_force()
        self.root.update()
        for query in ("서", "서울", "서울남"):
            self.variable.set(query)
            self.root.update()
            self.assertEqual(self.root.focus_get(), self.picker.entry)
            self.assertEqual(self.picker.matches, matching_courts(COURTS, query))
        self.picker.move(1)
        self.picker.commit()
        self.root.update()
        self.assertEqual(self.variable.get(), "서울남부지방법원")
        self.assertFalse(self.picker.visible())

    def test_click_selects_and_empty_match_is_not_a_court(self):
        self.picker.show(["서울고등법원", "부산지방법원"])
        self.root.update()
        bounds = self.picker.listbox.bbox(1)
        self.picker.listbox.event_generate("<ButtonRelease-1>", x=8, y=bounds[1] + bounds[3] // 2)
        self.root.update()
        self.assertEqual(self.variable.get(), "부산지방법원")
        self.variable.set("없는법원")
        self.root.update()
        self.picker.commit()
        self.assertEqual(self.variable.get(), "없는법원")

    def test_history_update_does_not_open_list_and_busy_state_closes_it(self):
        self.other.focus_force()
        self.root.update()
        self.variable.set("서울고등법원")
        self.root.update()
        self.assertFalse(self.picker.visible())
        self.picker.show(COURTS)
        self.root.update()
        self.assertTrue(self.picker.visible())
        self.picker.set_enabled(False)
        self.root.update()
        self.assertFalse(self.picker.visible())
        self.picker.show(COURTS)
        self.assertFalse(self.picker.visible())


if __name__ == "__main__":
    unittest.main()
