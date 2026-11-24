"""입력을 방해하지 않는 법원 이름 검색 목록."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
import unicodedata


def matching_courts(courts, query):
    def normalize(text):
        return "".join(unicodedata.normalize("NFC", text).split()).casefold()
    needle = normalize(query)
    return [court for court in courts if needle in normalize(court)]


class CourtPicker(ttk.Frame):
    def __init__(self, master, *, textvariable, width=37):
        super().__init__(master)
        self.variable = textvariable
        self.values = ()
        self.matches = []
        self.enabled = False
        self.popup = None
        self.listbox = None
        self._choosing = False
        self._focus_job = None
        self.entry = ttk.Entry(self, textvariable=textvariable, width=width)
        self.entry.pack(side="left", fill="x", expand=True)
        self.arrow = ttk.Button(self, text="▾", width=2, takefocus=False, command=self.toggle)
        self.arrow.pack(side="right")
        self.entry.bind("<Down>", lambda event: self.move(1))
        self.entry.bind("<Up>", lambda event: self.move(-1))
        self.entry.bind("<Return>", self.commit)
        self.entry.bind("<Escape>", lambda event: self.hide() or "break")
        self.entry.bind("<Tab>", lambda event: self.hide())
        self.entry.bind("<FocusOut>", self._focus_out)
        self.entry.bind("<FocusIn>", self._focus_in)
        self._trace = self.variable.trace_add("write", self._changed)
        self.owner = self.winfo_toplevel()
        self._owner_bindings = {
            "<ButtonPress-1>": self.owner.bind("<ButtonPress-1>", self._outside_click, add="+"),
            "<Configure>": self.owner.bind("<Configure>", self._owner_moved, add="+"),
            "<FocusOut>": self.owner.bind("<FocusOut>", self._focus_out, add="+"),
            "<Unmap>": self.owner.bind("<Unmap>", self._owner_unmapped, add="+"),
        }
        self.bind("<Destroy>", self._cleanup)
        self.set_enabled(False)

    def set_values(self, values):
        self.values = tuple(dict.fromkeys(values))
        if self.visible():
            self.show(matching_courts(self.values, self.variable.get()))

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        state = "normal" if self.enabled else "disabled"
        self.entry.configure(state=state)
        self.arrow.configure(state=state)
        if not self.enabled:
            self.hide()

    def visible(self):
        return self.popup is not None and bool(self.popup.winfo_viewable())

    def _changed(self, *_):
        if self.enabled and not self._choosing and self.focus_get() == self.entry:
            # 입력 문자열을 건드리지 않아 포커스와 한글 조합 상태를 유지한다.
            self.show(matching_courts(self.values, self.variable.get()))

    def _focus_in(self, _):
        if self.variable.get() in self.values:
            self.entry.selection_range(0, "end")

    def toggle(self):
        if not self.enabled:
            return
        if self.visible():
            self.hide()
            return
        self.entry.focus_set()
        query = self.variable.get()
        names = list(self.values) if query in self.values else matching_courts(self.values, query)
        self.show(names)

    def show(self, names):
        if not self.enabled or not self.values:
            return
        if self.popup is None:
            self.popup = tk.Toplevel(self)
            self.popup.withdraw()
            self.popup.overrideredirect(True)
            self.popup.transient(self.owner)
            self.popup.attributes("-topmost", True)
            self.listbox = tk.Listbox(self.popup, exportselection=False, activestyle="dotbox",
                                      font=self.entry.cget("font"), relief="solid", borderwidth=1,
                                      highlightthickness=0)
            scrollbar = ttk.Scrollbar(self.popup, orient="vertical", command=self.listbox.yview)
            self.listbox.configure(yscrollcommand=scrollbar.set)
            scrollbar.pack(side="right", fill="y")
            self.listbox.pack(side="left", fill="both", expand=True)
            self.listbox.bind("<ButtonRelease-1>", self._clicked)
            self.listbox.bind("<Return>", self.commit)
            self.listbox.bind("<Escape>", lambda event: self.hide() or "break")
            self.popup.bind("<FocusOut>", self._focus_out)
        self.matches = list(names)
        self.listbox.delete(0, "end")
        for name in self.matches:
            self.listbox.insert("end", name)
        if not self.matches:
            self.listbox.insert("end", "일치하는 법원이 없습니다.")
        self.listbox.configure(height=max(1, min(8, len(self.matches))))
        self.popup.update_idletasks()
        self._position()
        self.popup.deiconify()
        self.popup.lift()
        # grab_set/focus_set를 호출하지 않아 계속 한글을 입력할 수 있다.

    def _position(self):
        if self.popup is None:
            return
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height()
        height = self.popup.winfo_reqheight()
        if y + height > self.winfo_screenheight():
            y = max(0, self.winfo_rooty() - height)
        self.popup.geometry(f"{max(self.winfo_width(), 180)}x{height}+{x}+{y}")

    def move(self, step):
        if not self.enabled:
            return "break"
        if not self.visible():
            self.show(matching_courts(self.values, self.variable.get()))
        if self.matches:
            selection = self.listbox.curselection()
            index = max(0, min(len(self.matches) - 1, selection[0] + step)) if selection else (0 if step > 0 else len(self.matches) - 1)
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(index)
            self.listbox.activate(index)
            self.listbox.see(index)
        return "break"

    def _clicked(self, event):
        if self.matches:
            index = self.listbox.nearest(event.y)
            bounds = self.listbox.bbox(index)
            if bounds and bounds[1] <= event.y < bounds[1] + bounds[3]:
                self.choose(index)
        return "break"

    def commit(self, _=None):
        if self.visible() and self.matches:
            selection = self.listbox.curselection()
            self.choose(selection[0] if selection else 0)
        return "break"

    def choose(self, index):
        if not self.enabled or not 0 <= index < len(self.matches):
            return
        self._choosing = True
        try:
            self.variable.set(self.matches[index])
        finally:
            self._choosing = False
        self.hide()
        self.entry.focus_set()
        self.entry.selection_clear()
        self.entry.icursor("end")
        self.event_generate("<<CourtSelected>>")

    def hide(self):
        if self.popup is not None:
            self.popup.withdraw()

    def _outside_click(self, event):
        if event.widget not in (self.entry, self.arrow, self.listbox):
            self.hide()

    def _focus_out(self, _):
        if self._focus_job:
            self.after_cancel(self._focus_job)
        self._focus_job = self.after_idle(self._check_focus)

    def _check_focus(self):
        self._focus_job = None
        if self.focus_get() not in (self.entry, self.arrow, self.listbox):
            self.hide()

    def _owner_moved(self, event):
        if event.widget == self.owner and self.visible():
            self._position()

    def _owner_unmapped(self, event):
        if event.widget == self.owner:
            self.hide()

    def _cleanup(self, event):
        if event.widget != self:
            return
        self.variable.trace_remove("write", self._trace)
        if self._focus_job:
            self.after_cancel(self._focus_job)
        for sequence, binding in self._owner_bindings.items():
            self.owner.unbind(sequence, binding)
