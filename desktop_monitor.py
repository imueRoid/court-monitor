"""Windows-native Court Monitor. No web application server or Docker is started."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
import uuid
import webbrowser
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw

from court_picker import CourtPicker
from desktop_runtime import (APP_NAME, VERSION, DesktopGoogleConnection, SingleInstance, Startup,
                             WindowsVault, data_directory, pc_settings, render_guide, save_preferences, validate_case)
from desktop_updates import check_update, release_page
from server.domain import next_hearing
from server.google_calendar import GoogleCalendar
from server.store import Store
from server.worker import Worker


def tray_image():
    image = Image.new("RGBA", (64, 64), "#174e43")
    draw = ImageDraw.Draw(image)
    draw.rectangle((15, 22, 49, 50), fill="white")
    draw.rectangle((15, 22, 49, 28), fill="#b6d9c7")
    for x in (24, 40):
        draw.line((x, 14, x, 27), fill="white", width=5)
    draw.line((23, 38, 29, 44, 42, 32), fill="#17664e", width=4)
    return image


class MonitorApp(tk.Tk):
    def __init__(self, folder=None, *, start_worker=True, with_tray=True, startup=False,
                 startup_manager=None, check_updates=True):
        super().__init__()
        self.withdraw()
        self.title("사건 일정 관리 · PC")
        self.geometry("1240x850")
        self.minsize(940, 650)
        self.option_add("*Font", ("맑은 고딕", 10))
        self.folder = Path(folder or data_directory())
        self.settings = pc_settings(self.folder)
        self.store = Store(self.settings.database)
        self.store.resume_no_hearing_cancelled_cases(self.settings)
        self.startup_manager = startup_manager or Startup()
        self.events = queue.Queue()
        self.selected_ids, self.records = set(), {}
        self.page, self.pages, self.selected, self.task_count = 1, 1, None, 0
        self.closing, self.tray, self.worker = False, None, None
        self._update_job = None
        self._checking_update = False
        self._new_version = None
        self.signal = self.store.get_meta("desktop_show_requested")
        self.connection = DesktopGoogleConnection(self.store)
        self.preferences = self.store.get_meta("desktop_preferences", {"auto_start":False, "background":False})
        self._build()
        if with_tray:
            try:
                import pystray
                self.tray = pystray.Icon("court-monitor-pc", tray_image(), "사건 일정 관리 · PC",
                    pystray.Menu(pystray.MenuItem("사건 관리 열기", lambda *_: self.events.put(("show", None)), default=True),
                                 pystray.MenuItem("완전히 종료", lambda *_: self.events.put(("quit", None)))))
                self.tray.run_detached()
            except Exception:
                self.tray = None
        self.protocol("WM_DELETE_WINDOW", self.close_window)
        if start_worker:
            calendar = GoogleCalendar(self.settings)
            calendar.connection = self.connection
            self.worker = Worker(self.store, self.settings, calendar=calendar)
            self.worker.start()
        self.refresh()
        self._drain_job = self.after(100, self._drain)
        self._poll_job = self.after(3000, self._poll)
        if not (startup and self.preferences.get("background") and self.tray):
            self.show_window()
        if check_updates:
            self._poll_updates()

    def _build(self):
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        heading = ttk.Frame(outer)
        heading.pack(anchor="w")
        ttk.Label(heading, text="사건 일정 관리", font=("맑은 고딕", 22, "bold")).pack(side="left")
        ttk.Label(heading, text=f"v{VERSION}", font=("맑은 고딕", 10)).pack(side="left", anchor="s", padx=(8, 0), pady=(0, 3))
        ttk.Label(outer, text="PC에서 사건을 확인하고 Google Calendar에 기일과 선고 결과를 반영합니다.").pack(anchor="w", pady=(4, 12))
        self.update_banner = ttk.Frame(outer)
        self.update_notice = tk.StringVar()
        ttk.Label(self.update_banner, textvariable=self.update_notice).pack(side="left", padx=(0, 10))
        ttk.Button(self.update_banner, text="설치 파일 받기", command=self._open_update).pack(side="left")
        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill="both", expand=True)
        cases = ttk.Frame(self.tabs, padding=12)
        self.settings_tab = ttk.Frame(self.tabs, padding=18)
        logs = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(cases, text="사건 관리")
        self.tabs.add(self.settings_tab, text="설정")
        self.tabs.add(logs, text="작업 기록")
        form = ttk.LabelFrame(cases, text="사건 등록", padding=12)
        form.pack(fill="x")
        for column in range(3):
            form.columnconfigure(column, weight=1)
        self.court, self.number, self.party = tk.StringVar(), tk.StringVar(), tk.StringVar()
        for column, label in enumerate(("법원", "사건번호", "당사자명")):
            ttk.Label(form, text=label).grid(row=0, column=column, sticky="w")
        self.court_picker = CourtPicker(form, textvariable=self.court)
        self.court_picker.grid(row=1, column=0, sticky="ew", padx=(0, 10), pady=6)
        self.court_picker.set_enabled(True)
        ttk.Entry(form, textvariable=self.number).grid(row=1, column=1, sticky="ew", padx=(0, 10))
        ttk.Entry(form, textvariable=self.party).grid(row=1, column=2, sticky="ew", padx=(0, 10))
        ttk.Button(form, text="사건 등록", command=self.add_case).grid(row=1, column=3)
        filters = ttk.Frame(cases)
        filters.pack(fill="x", pady=12)
        ttk.Label(filters, text="사건 검색").pack(side="left")
        self.query, self.page_size = tk.StringVar(), tk.StringVar(value="20")
        ttk.Entry(filters, textvariable=self.query, width=35).pack(side="left", padx=8)
        self._search_job = None
        self.query.trace_add("write", self._search_changed)
        ttk.Label(filters, text="페이지당").pack(side="left", padx=(12, 5))
        size = ttk.Combobox(filters, textvariable=self.page_size, values=("10", "20", "50"), state="readonly", width=4)
        size.pack(side="left")
        size.bind("<<ComboboxSelected>>", lambda _: self.change_page(0))
        ttk.Label(filters, text="차회 기일이 빠른 순 · 미확인 기일은 마지막").pack(side="right")
        table = ttk.Frame(cases)
        table.pack(fill="both", expand=True)
        columns = ("check", "number", "court", "party", "state", "hearing", "checked")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="browse", height=10)
        for key, title, width in zip(columns, ("선택", "사건번호", "법원", "당사자", "상태", "차회 기일 ↑", "최근 확인"), (42, 135, 165, 90, 120, 220, 140)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=40, stretch=key != "check")
        scroll = ttk.Scrollbar(table, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<ButtonRelease-1>", self._row_click)
        self.tree.bind("<<TreeviewSelect>>", lambda _: self.show_detail())
        self.tree.bind("<space>", self._toggle_keyboard)
        actions = ttk.Frame(cases)
        actions.pack(fill="x", pady=10)
        for label, command in (("현재 페이지 선택/해제", self.toggle_page), ("선택 해제", self.clear_selection),
                               ("선택 사건 삭제", self.delete_selected), ("지금 확인", self.request_refresh),
                               ("중지/재개", self.toggle_active), ("가져오기", self.import_cases), ("내보내기", self.export_cases)):
            ttk.Button(actions, text=label, command=command).pack(side="left", padx=(0, 5))
        pages = ttk.Frame(cases)
        pages.pack(fill="x")
        self.count_text, self.page_text = tk.StringVar(), tk.StringVar()
        ttk.Label(pages, textvariable=self.count_text).pack(side="left")
        self.next_button = ttk.Button(pages, text="다음", command=lambda: self.change_page(1))
        self.next_button.pack(side="right")
        ttk.Label(pages, textvariable=self.page_text).pack(side="right", padx=12)
        self.previous_button = ttk.Button(pages, text="이전", command=lambda: self.change_page(-1))
        self.previous_button.pack(side="right")
        self.detail = ScrolledText(cases, height=7, wrap="word", font=("맑은 고딕", 10), state="disabled")
        self.detail.pack(fill="x", pady=(10, 0))
        self._write(self.detail, "사건번호를 누르면 판결 주문·선고일자·상소 정보를 표시합니다.")
        self.status_text = tk.StringVar(value="준비 중")
        ttk.Label(outer, textvariable=self.status_text, wraplength=1160).pack(anchor="w", pady=(10, 0))
        self._build_settings()
        self.log_view = ScrolledText(logs, wrap="word", state="disabled", font=("맑은 고딕", 10))
        self.log_view.pack(fill="both", expand=True)

    def _build_settings(self):
        ttk.Label(self.settings_tab, text="실행 및 백그라운드 동작", font=("맑은 고딕", 14, "bold")).pack(anchor="w")
        self.auto_start = tk.BooleanVar(value=self.startup_manager.enabled())
        self.background = tk.BooleanVar(value=self.preferences.get("background", False))
        ttk.Checkbutton(self.settings_tab, text="Windows에 로그인하면 자동 실행", variable=self.auto_start).pack(anchor="w", pady=(12, 6))
        ttk.Checkbutton(self.settings_tab, text="창을 닫아도 트레이에서 계속 실행", variable=self.background).pack(anchor="w", pady=6)
        ttk.Label(self.settings_tab, text="두 기능을 모두 켜면 Windows 로그인 후 창을 띄우지 않고 일정 확인을 시작합니다.\n백그라운드 기능을 끄면 창의 X 버튼으로 완전히 종료됩니다. 트레이 메뉴에서도 종료할 수 있습니다.\nPC가 꺼져 있거나 절전 중이면 확인하지 못하며, 실행·복귀 후 저장된 예약부터 이어서 처리합니다.", wraplength=1000, justify="left").pack(anchor="w", pady=8)
        ttk.Button(self.settings_tab, text="실행 설정 저장", command=self.save_settings).pack(anchor="w", pady=(0, 16))
        ttk.Separator(self.settings_tab).pack(fill="x", pady=8)
        ttk.Label(self.settings_tab, text="Google Calendar 연결", font=("맑은 고딕", 14, "bold")).pack(anchor="w", pady=10)
        self.google_text = tk.StringVar()
        ttk.Label(self.settings_tab, textvariable=self.google_text, wraplength=1000).pack(anchor="w", pady=8)
        google_buttons = ttk.Frame(self.settings_tab)
        google_buttons.pack(anchor="w")
        for label, command in (("데스크톱 OAuth JSON 등록", self.configure_google), ("Google 계정 연결", self.connect_google),
                               ("캘린더 준비 재시도", self.retry_calendar), ("연결 해제", self.disconnect_google),
                               ("설정 방법 보기", self.open_guide)):
            ttk.Button(google_buttons, text=label, command=command).pack(side="left", padx=(0, 8))
        ttk.Label(self.settings_tab, text="기존 NAS용 OAuth JSON 대신 '데스크톱 앱' 유형의 JSON을 등록하세요.\n연결하면 이 PC가 관리할 전용 캘린더를 자동 생성합니다. Google 로그인은 기본 브라우저에서 진행합니다.", wraplength=1000).pack(anchor="w", pady=12)
        ttk.Separator(self.settings_tab).pack(fill="x", pady=8)
        ttk.Label(self.settings_tab, text=f"데이터 저장 위치: {self.folder}\n사건·예약·설정은 PC의 SQLite DB에 보관하며, Google 인증정보는 Windows 사용자 계정으로 암호화합니다.", wraplength=1000).pack(anchor="w", pady=12)
        tools = ttk.Frame(self.settings_tab)
        tools.pack(anchor="w")
        ttk.Button(tools, text="데이터 폴더 열기", command=lambda: os.startfile(self.folder)).pack(side="left", padx=(0, 8))
        self.update_button = ttk.Button(tools, text="업데이트 확인", command=self._request_update)
        self.update_button.pack(side="left", padx=(0, 8))
        self.update_status = tk.StringVar()
        ttk.Label(tools, textvariable=self.update_status).pack(side="left")

    @staticmethod
    def _write(widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _task(self, function, message):
        if self.task_count:
            self.status_text.set("진행 중인 작업이 끝나면 다시 시도하세요.")
            return
        self.task_count += 1
        self.status_text.set(message)
        def run():
            try:
                result = function()
                self.events.put(("success", result if isinstance(result, str) else "작업을 완료했습니다."))
            except Exception as exc:
                self.events.put(("error", str(exc)))
        threading.Thread(target=run, name="desktop-action", daemon=True).start()

    def _drain(self):
        while not self.events.empty():
            kind, value = self.events.get_nowait()
            if kind == "show":
                self.show_window()
            elif kind == "quit":
                self.quit_app()
                return
            elif kind == "update":
                self._show_update_result(*value)
            else:
                self.task_count = max(0, self.task_count - 1)
                self.status_text.set(value)
                if kind == "error":
                    messagebox.showerror("작업을 완료하지 못했습니다", value, parent=self)
                self.refresh()
        if not self.closing:
            self._drain_job = self.after(100, self._drain)

    def _poll_updates(self):
        self._request_update()
        self._update_job = self.after(6 * 60 * 60 * 1000, self._poll_updates)

    def _request_update(self):
        if self._checking_update:
            return
        self._checking_update = True
        self.update_button.state(["disabled"])
        self.update_status.set("업데이트 확인 중…")

        def run():
            try:
                self.events.put(("update", (check_update(VERSION), False)))
            except (OSError, ValueError):
                self.events.put(("update", (None, True)))

        threading.Thread(target=run, name="desktop-update", daemon=True).start()

    def _show_update_result(self, version, failed):
        self._checking_update = False
        self.update_button.state(["!disabled"])
        if failed:
            self.update_status.set("업데이트를 확인하지 못했습니다. 나중에 다시 시도하세요.")
            return
        self._new_version = version
        if version:
            self.update_notice.set(f"새 Windows 버전 {version}을 설치할 수 있습니다.")
            self.update_banner.pack(fill="x", pady=(0, 8), before=self.tabs)
            self.update_status.set(f"새 버전 {version} 사용 가능")
        else:
            self.update_banner.pack_forget()
            self.update_status.set("최신 버전입니다.")

    def _open_update(self):
        if self._new_version:
            webbrowser.open(release_page(self._new_version))

    def _poll(self):
        signal = self.store.get_meta("desktop_show_requested")
        if signal != self.signal:
            self.signal = signal
            self.show_window()
        self.refresh()
        if not self.closing:
            self._poll_job = self.after(3000, self._poll)

    def refresh(self):
        now = datetime.now(timezone.utc)
        zone = ZoneInfo(self.settings.timezone)
        today = now.astimezone(zone).date().isoformat()
        data = self.store.case_page(self.query.get(), self.page, int(self.page_size.get()), now=now)
        self.page, self.pages = data["page"], data["pages"]
        self.records = {row["id"]:row for row in data["cases"]}
        selected = self.selected
        self.tree.delete(*self.tree.get_children())
        labels = {"pending":"조회 대기", "monitoring":"변론 진행중", "awaiting_order":"주문 확인중", "done":"주문·종국결과 확인"}
        for row in self.records.values():
            hearing = next_hearing(row["observed"], now, self.settings.timezone)
            day = f"{hearing.day} {hearing.clock} {hearing.kind}" if hearing else "기일 미확인"
            checked = datetime.fromtimestamp(row["last_checked"], zone).strftime("%m.%d %H:%M") if row["last_checked"] else "—"
            status = labels.get(row["state"], row["state"])
            if row["state"] != "done" and hearing and hearing.kind == "선고":
                status = "변론 종결됨" if hearing.day > today else "주문 확인중"
            if not row["active"]:
                status = "일시중지"
            self.tree.insert("", "end", iid=row["id"], values=("☑" if row["id"] in self.selected_ids else "☐", row["case_number"], row["court"], row["party"], status, day, checked))
        if selected in self.records:
            self.tree.selection_set(selected)
            self.show_detail()
        else:
            self.selected = None
            self._write(self.detail, "사건번호를 누르면 판결 주문·선고일자·상소 정보를 표시합니다.")
        self.count_text.set(f"전체 {data['metrics']['total']}건 · 검색 {data['total']}건 · 선택 {len(self.selected_ids)}건 (다른 페이지 포함)")
        self.page_text.set(f"{self.page} / {self.pages} 페이지")
        self.previous_button.configure(state="normal" if self.page > 1 else "disabled")
        self.next_button.configure(state="normal" if self.page < self.pages else "disabled")
        self.court_picker.set_values(self.store.get_meta("courts", []))
        connection = self.connection.status()
        self.google_text.set("연결됨 · " + connection["calendar_name"] if connection["connected"] else "Google Calendar가 연결되지 않았습니다. OAuth JSON 등록 후 Google 계정을 연결하세요.")
        log_lines = [datetime.fromtimestamp(item["at"], ZoneInfo(self.settings.timezone)).strftime("%m.%d %H:%M:%S") + "  " + item["message"] for item in self.store.logs()]
        self._write(self.log_view, "\n".join(log_lines) or "아직 작업 기록이 없습니다.")

    def _search_changed(self, *_):
        if self._search_job:
            self.after_cancel(self._search_job)
        self._search_job = self.after(300, lambda: self.change_page(0))

    def change_page(self, step):
        self.page = max(1, min(self.pages, self.page + step)) if step else 1
        self.selected = None
        self.refresh()

    def _row_click(self, event):
        key = self.tree.identify_row(event.y)
        if key and self.tree.identify_column(event.x) == "#1":
            self._toggle(key)

    def _toggle_keyboard(self, _):
        if self.tree.selection():
            self._toggle(self.tree.selection()[0])
        return "break"

    def _toggle(self, key):
        if key in self.selected_ids:
            self.selected_ids.remove(key)
        else:
            self.selected_ids.add(key)
        self.refresh()

    def show_detail(self):
        selection = self.tree.selection()
        if not selection or selection[0] not in self.records:
            return
        self.selected = selection[0]
        row = self.records[self.selected]
        lines = [f"{row['court']} {row['case_number']} · {row['party']}", ""]
        for key in ("판결 주문", "선고일자", "상소제기 여부", "상소사건번호"):
            lines.append(f"{key}: {row['observed'].get('summary', {}).get(key) or '확인되지 않음'}")
        errors = [row["last_error"], row["calendar_error"], *(row["observed"].get("warnings", []))]
        lines.extend(item for item in errors if item)
        self._write(self.detail, "\n".join(lines))

    def add_case(self):
        try:
            case = validate_case(self.court.get(), self.number.get(), self.party.get())
            self.store.add_case(*case)
            self.number.set("")
            self.party.set("")
            self.status_text.set("사건을 등록했습니다. PC가 실행 중이면 순서대로 자동 조회합니다.")
            self.change_page(0)
        except Exception as exc:
            messagebox.showerror("사건 등록", str(exc), parent=self)

    def toggle_page(self):
        keys = set(self.records)
        if keys <= self.selected_ids:
            self.selected_ids -= keys
        else:
            self.selected_ids |= keys
        self.refresh()

    def clear_selection(self):
        self.selected_ids.clear()
        self.refresh()

    def delete_selected(self):
        keys = list(self.selected_ids)
        if not keys:
            messagebox.showinfo("사건 선택", "왼쪽 선택 칸을 눌러 삭제할 사건을 선택하세요.", parent=self)
            return
        if messagebox.askyesno("선택 사건 삭제", f"선택한 {len(keys)}건과 저장된 조회 내용을 삭제할까요?\n복구할 수 없습니다. Google Calendar의 기존 일정은 유지됩니다.", parent=self):
            count = self.store.delete_cases(keys)
            self.selected_ids.clear()
            self.status_text.set(f"{count}건을 삭제했습니다.")
            self.refresh()

    def request_refresh(self):
        keys = self.selected_ids or set(self.tree.selection())
        for key in keys:
            self.store.request_refresh(key)
        self.status_text.set(f"{len(keys)}건의 조회를 예약했습니다.")
        self.refresh()

    def toggle_active(self):
        keys = self.selected_ids or set(self.tree.selection())
        rows = {row["id"]:row for row in self.store.list_cases()}
        for key in keys:
            if key in rows:
                self.store.set_active(key, not rows[key]["active"])
        self.refresh()

    def import_cases(self):
        filename = filedialog.askopenfilename(parent=self, filetypes=[("사건 JSON", "*.json")])
        if not filename:
            return
        def run():
            path = Path(filename)
            if path.stat().st_size > 10 * 1024 * 1024:
                raise ValueError("10MB 이하의 사건 JSON을 선택하세요.")
            content = json.loads(path.read_text("utf-8-sig"))
            records = content.get("cases", content.get("records", [content])) if isinstance(content, dict) else content
            cases = [validate_case(**{k:row.get("query", row)[k] for k in ("court", "party")}, number=row.get("query", row)["case_number"]) for row in records]
            for case in cases:
                self.store.add_case(*case)
            return f"{len(cases)}건을 가져왔습니다. 조회와 Google 연결은 이 PC에서 진행합니다."
        self._task(run, "사건 목록을 가져오고 있습니다…")

    def export_cases(self):
        filename = filedialog.asksaveasfilename(parent=self, defaultextension=".json", initialfile="court-cases.json", filetypes=[("JSON", "*.json")])
        if filename:
            try:
                data = {"exported_at":time.time(), "cases":[{"query":{key:row[key] for key in ("court", "case_number", "party")}, "summary":row["observed"].get("summary", {})} for row in self.store.list_cases()]}
                Path(filename).write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
                self.status_text.set("사건 목록을 저장했습니다. Google 인증정보는 포함하지 않습니다.")
            except OSError as exc:
                messagebox.showerror("내보내기", str(exc), parent=self)

    def save_settings(self):
        if self.background.get() and not self.tray:
            messagebox.showerror("설정 저장", "트레이 아이콘을 시작하지 못했습니다. 프로그램을 다시 실행한 뒤 백그라운드 기능을 켜 주세요.", parent=self)
            return
        try:
            save_preferences(self.store, self.startup_manager, auto_start=self.auto_start.get(), background=self.background.get())
            self.preferences = self.store.get_meta("desktop_preferences")
            self.status_text.set("실행 설정을 저장했습니다. Windows 자동 실행은 다음 로그인부터 적용됩니다.")
        except Exception as exc:
            messagebox.showerror("설정 저장", str(exc), parent=self)

    def configure_google(self):
        filename = filedialog.askopenfilename(parent=self, title="데스크톱 앱 OAuth JSON", filetypes=[("JSON", "*.json")])
        if filename:
            def run():
                if Path(filename).stat().st_size > 100000:
                    raise ValueError("Google Cloud에서 받은 OAuth JSON을 선택하세요.")
                self.connection.configure(json.loads(Path(filename).read_text("utf-8-sig")))
                return "OAuth JSON을 등록했습니다. Google 계정 연결을 누르세요."
            self._task(run, "OAuth 설정을 저장합니다…")

    def connect_google(self):
        self._task(lambda: (self.connection.authorize() and "Google Calendar를 연결했습니다."), "브라우저에서 Google 계정을 선택하고 승인하세요. 최대 3분 기다립니다…")

    def retry_calendar(self):
        self._task(lambda: (self.connection.prepare_calendar() and "전용 캘린더를 준비했습니다."), "전용 캘린더를 확인합니다…")

    def disconnect_google(self):
        if self.task_count:
            self.status_text.set("진행 중인 작업이 끝나면 다시 시도하세요.")
            return
        if messagebox.askyesno("Google 연결 해제", "이 PC의 Google 연결을 해제할까요? Google Calendar의 기존 일정은 유지됩니다.", parent=self):
            self.connection.disconnect()
            self.refresh()

    def open_guide(self):
        guide = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "WINDOWS-PC.md"
        try:
            rendered = self.folder / "guide.html"
            rendered.write_text(render_guide(guide.read_text("utf-8")), "utf-8")
            webbrowser.open(rendered.as_uri())
        except OSError:
            messagebox.showinfo("Google 연결", "Google Cloud → API 및 서비스 → Google Calendar API 사용 → OAuth 동의 화면과 테스트 사용자 등록 → OAuth 클라이언트 유형 '데스크톱 앱' → JSON 다운로드 → 이 앱의 설정에서 등록 → Google 계정 연결", parent=self)

    def show_window(self):
        self.deiconify()
        self.lift()

    def close_window(self):
        if self.preferences.get("background") and self.tray:
            self.withdraw()
        else:
            self.quit_app()

    def quit_app(self):
        if self.closing:
            return
        self.closing = True
        self.withdraw()
        if self.tray:
            self.tray.stop()
        for job in (getattr(self, "_drain_job", None), getattr(self, "_poll_job", None),
                    self._search_job, self._update_job):
            if job:
                self.after_cancel(job)
        if self.worker:
            self.worker.stop()
        self.destroy()


def self_test(report):
    try:
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "test.sqlite3")
            vault = WindowsVault(store)
            vault.set("fixture", {"text":"packaged Windows test"})
            assert vault.get("fixture")["text"] == "packaged Windows test"
            key = store.add_case(*validate_case("가상법원", "2024가단1", "가상인"))
            assert Store(store.path).list_cases()[0]["id"] == key
            from captcha_ocr import CaptchaRecognizer
            import io
            image = Image.new("RGB", (180, 70), "white")
            ImageDraw.Draw(image).text((10, 10), "123456", fill="black", font_size=42)
            raw = io.BytesIO()
            image.save(raw, "PNG")
            answer = CaptchaRecognizer().recognize(raw.getvalue())
            outcome = {"ok":True, "version":VERSION, "sqlite":True, "windows_encryption":True, "ocr_digits":len(answer)}
    except Exception as exc:
        outcome = {"ok":False, "error":str(exc)}
    try:
        Path(report).parent.mkdir(parents=True, exist_ok=True)
        Path(report).write_text(json.dumps(outcome), "utf-8")
    except OSError:
        return 1
    return 0 if outcome["ok"] else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--startup", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--report")
    args = parser.parse_args()
    if args.self_test:
        return self_test(args.report or str(Path(tempfile.gettempdir()) / "court-monitor-pc-check.json"))
    folder = data_directory()
    folder.mkdir(parents=True, exist_ok=True)
    instance = SingleInstance(folder)
    if instance.already_running:
        if not args.startup:
            Store(folder / "cases.sqlite3").set_meta("desktop_show_requested", uuid.uuid4().hex)
        instance.close()
        return 0
    handler = RotatingFileHandler(folder / "application.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    logging.basicConfig(handlers=[handler], level=logging.WARNING)
    try:
        app = MonitorApp(folder, startup=args.startup)
        app.mainloop()
    except Exception:
        logging.exception("PC application failed")
        messagebox.showerror("사건 일정 관리", f"프로그램을 시작하지 못했습니다.\n{folder / 'application.log'} 파일을 확인하세요.")
        return 1
    finally:
        instance.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
