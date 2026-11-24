"""로컬 사건검색 프로그램. python app.py로 실행."""
from __future__ import annotations

import argparse
import json
import queue
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from case_history import CaseHistory, HistoryError
from case_summary import summarize_case
from captcha_ocr import CaptchaRecognizer
from court_client import CourtClient, CourtError
from court_picker import CourtPicker


def display_time(value):
    try:
        return datetime.fromisoformat(value).strftime("%Y.%m.%d %H:%M:%S")
    except (ValueError, TypeError):
        return "조회 시각 미상"


def result_report(result):
    query = result["query"]
    lines = [f"{query['court']} {query['case_number']}", f"조회 당사자: {query['party']}",
             "최근 조회: " + display_time(result.get("queried_at")), ""]
    for label, value in summarize_case(result).items():
        lines.extend([label, value, ""])
    return "\n".join(lines)


def verify_ocr(image_path: Path, report_path: Path) -> int:
    try:
        answer = CaptchaRecognizer().recognize(image_path.read_bytes())
        outcome = {"ok": True, "answer": answer}
    except Exception as exc:
        outcome = {"ok": False, "error": str(exc)}
    try:
        report_path.write_text(json.dumps(outcome, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        if sys.stderr is not None:
            print(f"OCR 진단 결과를 저장할 수 없습니다: {exc}", file=sys.stderr)
        return 1
    return 0 if outcome["ok"] else 1


class App(tk.Tk):
    def __init__(self, initial=None, history_path=None):
        super().__init__()
        self.title("나의 사건검색 · 판결 주문/결과 확인")
        self.geometry("1120x820")
        self.minsize(850, 650)
        self.option_add("*Font", ("맑은 고딕", 10))
        self.client = CourtClient()
        self.recognizer = CaptchaRecognizer()
        self.history = CaseHistory(history_path)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="court-worker")
        self.events = queue.Queue()
        self.busy = False
        self.ready = False
        self.result = None
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._build(initial or {})
        try:
            self.history.load()
            self.refresh_history()
        except HistoryError as exc:
            self.history_note.set(str(exc))
        self._buttons()
        self._drain_after = self.after(100, self._drain)
        self._init_after = self.after(150, self.initialize)

    def _build(self, initial):
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        outer = ttk.Frame(self, padding=20)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="나의 사건검색", font=("맑은 고딕", 21, "bold")).pack(anchor="w")
        ttk.Label(outer, text="판결 주문 · 선고일자 · 상소제기 여부 · 상소사건번호").pack(anchor="w", pady=(3, 15))
        form = ttk.LabelFrame(outer, text="조회할 사건", padding=15)
        form.pack(fill="x")
        for i in range(3):
            form.columnconfigure(i, weight=1)
        self.court = tk.StringVar(value=initial.get("court", ""))
        self.number = tk.StringVar(value=initial.get("case", ""))
        self.party = tk.StringVar(value=initial.get("party", ""))
        for i, label in enumerate(("법원", "사건번호", "당사자명 (2글자 이상)")):
            ttk.Label(form, text=label).grid(row=0, column=i, sticky="w", padx=(0, 12))
        self.court_box = CourtPicker(form, textvariable=self.court, width=37)
        self.court_box.grid(row=1, column=0, sticky="ew", padx=(0, 12), pady=(5, 10))
        self.number_box = ttk.Entry(form, textvariable=self.number)
        self.number_box.grid(row=1, column=1, sticky="ew", padx=(0, 12), pady=(5, 10))
        self.party_box = ttk.Entry(form, textvariable=self.party)
        self.party_box.grid(row=1, column=2, sticky="ew", pady=(5, 10))
        self.party_box.bind("<Return>", lambda _: self.search())
        ttk.Label(form, text="예: 2024가단12345").grid(row=2, column=1, sticky="w")
        footer = ttk.Frame(form)
        footer.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        ttk.Label(footer, text="조회한 사건은 조회 이력에 자동 저장됩니다.").pack(side="left")
        self.search_button = ttk.Button(footer, text="사건 조회", command=self.search)
        self.search_button.pack(side="right")

        toolbar = ttk.Frame(outer)
        toolbar.pack(fill="x", pady=(12, 8))
        self.status = tk.StringVar(value="법원 사이트에 연결 중입니다…")
        ttk.Label(toolbar, textvariable=self.status, wraplength=720).pack(side="left")
        self.export_button = ttk.Button(toolbar, text="결과 저장", command=self.export, state="disabled")
        self.export_button.pack(side="right")
        self.connect_button = ttk.Button(toolbar, text="다시 연결", command=self.initialize)
        self.connect_button.pack(side="right", padx=7)

        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill="both", expand=True)
        frame = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(frame, text="조회 결과")
        view = ScrolledText(frame, wrap="word", font=("맑은 고딕", 10), padx=12, pady=12,
                            relief="flat", state="disabled")
        view.pack(fill="both", expand=True)
        self.views = {"조회 결과": view}
        self._set_text("조회 결과", "사건을 조회하면 판결 주문, 선고일자, 상소제기 여부, 상소사건번호가 표시됩니다.")

        history_frame = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(history_frame, text="조회 이력")
        self.history_note = tk.StringVar(value="저장된 사건이 없습니다.")
        ttk.Label(history_frame, textvariable=self.history_note, wraplength=1000).pack(anchor="w", pady=(0, 8))
        list_frame = ttk.Frame(history_frame)
        list_frame.pack(fill="both", expand=True)
        columns = ("court", "case", "party", "time", "judgment")
        self.history_tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="browse")
        for name, label, width in zip(columns, ("법원", "사건번호", "당사자", "최근 조회", "판결 주문"), (185, 155, 105, 165, 330)):
            self.history_tree.heading(name, text=label)
            self.history_tree.column(name, width=width, minwidth=70)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.history_tree.yview)
        self.history_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.history_tree.pack(side="left", fill="both", expand=True)
        self.history_tree.bind("<ButtonRelease-1>", self._history_click)
        self.history_tree.bind("<Return>", lambda _: self.open_history_case())
        actions = ttk.Frame(history_frame)
        actions.pack(fill="x", pady=(10, 0))
        self.open_history_button = ttk.Button(actions, text="선택 사건 조회", command=self.open_history_case)
        self.open_history_button.pack(side="right")
        self.import_button = ttk.Button(actions, text="기존 결과 JSON 가져오기", command=self.import_history)
        self.import_button.pack(side="left")

    def _set_text(self, name, text):
        view = self.views[name]
        view.configure(state="normal")
        view.delete("1.0", "end")
        view.insert("1.0", text)
        view.configure(state="disabled")

    def _buttons(self):
        state = "disabled" if self.busy else "normal"
        self.connect_button.configure(state=state)
        self.search_button.configure(state=state if self.ready else "disabled")
        self.court_box.set_enabled(self.ready and not self.busy)
        for widget in (self.number_box, self.party_box):
            widget.configure(state=state)
        self.export_button.configure(state="normal" if self.result and not self.busy else "disabled")
        self.open_history_button.configure(state=state if self.history.records else "disabled")
        self.import_button.configure(state=state)

    def _work(self, fn, done, failure=None):
        if self.busy:
            return
        self.busy = True
        self._buttons()
        def run():
            try:
                self.events.put((done, fn()))
            except Exception as exc:
                message = str(exc) if isinstance(exc, CourtError) else f"처리 오류 ({type(exc).__name__}): {exc}"
                self.events.put((failure or self._error, message))
        self.executor.submit(run)

    def _drain(self):
        try:
            while True:
                done, value = self.events.get_nowait()
                self.busy = False
                try:
                    done(value)
                except Exception as exc:
                    self._error(f"화면 표시 오류: {exc}")
                self._buttons()
        except queue.Empty:
            pass
        self._drain_after = self.after(100, self._drain)

    def _error(self, message):
        self.status.set(message)
        messagebox.showerror("사건검색", message, parent=self)

    def initialize(self):
        if self.busy:
            return
        self.ready = False
        self.status.set("법원 목록을 받는 중입니다…")
        def load():
            self.client.close()
            self.client = CourtClient()
            return self.client.initialize()
        def done(courts):
            self.ready = True
            self.court_box.set_values(courts)
            if self.court.get() not in courts:
                self.court.set("서울중앙지방법원" if "서울중앙지방법원" in courts else next(iter(courts)))
            self.status.set("조회 준비 완료 · 사건 조회를 누르세요.")
        self._work(load, done)

    def search(self, keep_cached=False):
        if self.busy or not self.ready:
            return
        court, number, party = self.court.get(), self.number.get(), self.party.get()
        try:
            self.client.validate(court, number, party)
        except CourtError as exc:
            self._error(str(exc))
            return
        if not keep_cached:
            self.result = None
            self._set_text("조회 결과", "조회 중…")
        self.tabs.select(0)
        self.status.set("대법원 서버에서 최신 사건정보를 조회하는 중입니다…")
        def failed(message):
            if not keep_cached:
                self._set_text("조회 결과", "조회 실패\n\n" + message)
            self._error(message)
            if keep_cached:
                self.status.set("최신 조회 실패 · 화면에는 저장된 이전 결과가 표시됩니다.")
        self._work(lambda: self.client.search_auto(court, number, party, self.recognizer), self.show_result, failed)

    def show_result(self, result, remember=True):
        self.result = result
        self._set_text("조회 결과", result_report(result))
        self.tabs.select(0)
        self.status.set("조회 완료" + (" · 일부 항목 확인 필요" if result.get("warnings") else ""))
        if remember:
            try:
                self.history.save_result(result)
                self.refresh_history()
            except HistoryError as exc:
                self.status.set(str(exc))
                self.history_note.set(str(exc))
        self._buttons()

    def refresh_history(self):
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
        for key, record in self.history.items():
            query = record["query"]
            judgment = record["summary"].get("판결 주문", "확인되지 않음").replace("\n", " / ")
            self.history_tree.insert("", "end", iid=key, values=(query["court"], query["case_number"], query["party"],
                display_time(record.get("queried_at")), judgment[:100]))
        self.history_note.set(f"저장된 사건 {len(self.history.records)}건 · 사건을 누르면 저장 결과를 표시하고 최신 정보를 다시 조회합니다.")

    def _history_click(self, event):
        if self.history_tree.identify_region(event.x, event.y) != "cell":
            return
        item = self.history_tree.identify_row(event.y)
        if item:
            self.open_history_case(item)

    def open_history_case(self, key=None):
        if self.busy:
            return
        selection = self.history_tree.selection()
        key = key or (selection[0] if selection else None)
        record = self.history.records.get(key)
        if not record:
            return
        query = record["query"]
        self.court.set(query["court"])
        self.number.set(query["case_number"])
        self.party.set(query["party"])
        self.show_result(record, remember=False)
        if self.ready:
            self.search(keep_cached=True)
        else:
            self.status.set("저장된 이전 결과 · 다시 연결 후 사건 조회를 누르면 최신 정보를 확인할 수 있습니다.")

    def import_history(self):
        if self.busy:
            return
        paths = filedialog.askopenfilenames(parent=self, title="기존 조회 결과 가져오기", filetypes=[("조회 결과 JSON", "*.json")])
        if not paths:
            return
        errors, count = [], 0
        for path in paths:
            try:
                self.history.save_result(json.loads(Path(path).read_text("utf-8-sig")))
                count += 1
            except (OSError, ValueError, TypeError, AttributeError, HistoryError) as exc:
                errors.append(f"{Path(path).name}: {exc}")
        self.refresh_history()
        self._buttons()
        self.history_note.set(f"{count}개 파일을 가져왔습니다. 저장된 사건 {len(self.history.records)}건.")
        if errors:
            self._error("일부 파일을 가져오지 못했습니다.\n" + "\n".join(errors))

    def export(self):
        if not self.result:
            return
        path = filedialog.asksaveasfilename(parent=self, title="조회 결과 저장", defaultextension=".txt",
            initialfile=f"사건검색_{self.result['query']['case_number']}.txt",
            initialdir=str(Path.home() / "Downloads") if (Path.home() / "Downloads").is_dir() else str(Path.home()),
            filetypes=[("조회 결과 텍스트", "*.txt"), ("조회 결과 JSON", "*.json")])
        if not path:
            return
        try:
            summary = {"query": self.result["query"], "summary": summarize_case(self.result),
                       "queried_at": self.result.get("queried_at", "")}
            content = json.dumps(summary, ensure_ascii=False, indent=2) if Path(path).suffix.lower() == ".json" else result_report(self.result)
            Path(path).write_text(content, encoding="utf-8")
        except OSError:
            self._error("파일을 저장하지 못했습니다. 쓰기가 허용된 다른 폴더를 선택하세요.")
            return
        self.status.set("저장 완료: " + path)

    def destroy(self):
        for name in ("_drain_after", "_init_after"):
            timer = getattr(self, name, None)
            if timer:
                self.after_cancel(timer)
        if hasattr(self, "executor"):
            self.executor.shutdown(wait=False, cancel_futures=True)
        super().destroy()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="나의 사건검색 데스크톱 프로그램")
    parser.add_argument("--court", default="")
    parser.add_argument("--case", default="")
    parser.add_argument("--party", default="")
    parser.add_argument("--verify-ocr", nargs=2, metavar=("IMAGE", "REPORT"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.verify_ocr:
        source, report = map(Path, args.verify_ocr)
        raise SystemExit(verify_ocr(source, report))
    App({"court": args.court, "case": args.case, "party": args.party}).mainloop()
