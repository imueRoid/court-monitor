import json
import logging
import threading
import time

from captcha_ocr import CaptchaRecognizer
from court_client import CourtClient
from .domain import snapshot
from .google_calendar import GoogleCalendar


logger = logging.getLogger("court-monitor")


class Worker:
    def __init__(self, store, settings, calendar=None, client_factory=CourtClient, recognizer=None):
        self.store = store
        self.settings = settings
        self.calendar = calendar or GoogleCalendar(settings, store)
        self.client_factory = client_factory
        self.reader = recognizer or CaptchaRecognizer()
        self.client = None
        self.stop_event = threading.Event()
        self.thread = None
        self.last_court_request = 0
        self.store.set_calendar_target(self.calendar.calendar_id)

    def start(self):
        self.thread = threading.Thread(target=self.run, name="court-monitor", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def _client(self):
        if self.client is None:
            client = self.client_factory()
            try:
                client.initialize()
            except Exception:
                client.close()
                raise
            self.client = client
            self.store.set_meta("courts", list(client.courts))
        return self.client

    def tick(self, now=None):
        now = time.time() if now is None else now
        self.store.set_meta("worker_heartbeat", now)
        did_work = False
        if now - self.last_court_request >= self.settings.request_gap_seconds:
            case = self.store.claim_case(now, self.settings)
            if case:
                did_work = True
                self.last_court_request = now
                try:
                    result = self._client().search_auto(case["court"], case["case_number"], case["party"], self.reader)
                    observed = snapshot(result)
                    self.store.complete(case["id"], case["lease_token"], observed, self.settings, now)
                except Exception as exc:
                    self.store.fail_case(case["id"], case["lease_token"], str(exc), now, self.settings)
                    if self.client:
                        self.client.close()
                        self.client = None
        if self.calendar.configured:
            self.store.set_calendar_target(self.calendar.calendar_id)
            event = self.store.claim_event(now)
            if event:
                did_work = True
                try:
                    self.calendar.upsert(event["id"], json.loads(event["payload"]))
                    self.store.event_synced(event, now)
                except Exception as exc:
                    self.store.event_failed(event, str(exc), now, getattr(exc, "retry_after", 0))
        return did_work

    def run(self):
        # 목록 준비 실패가 웹 관리 화면까지 중단시키지 않게 한다.
        try:
            self._client()
        except Exception:
            logger.warning("법원 목록을 준비하지 못했습니다. 사건 조회 시 다시 시도합니다.")
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("작업 루프 오류")
            self.stop_event.wait(2)
        if self.client:
            self.client.close()
