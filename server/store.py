from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo

from .domain import Hearing, calendar_hearings, event_payload, schedule


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS cases (
                    id TEXT PRIMARY KEY, court TEXT NOT NULL, case_number TEXT NOT NULL, party TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'pending',
                    snapshot TEXT NOT NULL DEFAULT '{}', last_checked REAL, next_check REAL,
                    last_error TEXT NOT NULL DEFAULT '', failures INTEGER NOT NULL DEFAULT 0,
                    verdict_day TEXT NOT NULL DEFAULT '', verdict_count INTEGER NOT NULL DEFAULT 0,
                    verdict_count_day TEXT NOT NULL DEFAULT '', manual INTEGER NOT NULL DEFAULT 0,
                    lease_until REAL NOT NULL DEFAULT 0, lease_token TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
                    UNIQUE(court, case_number, party));
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), signature TEXT NOT NULL,
                    hearing TEXT NOT NULL, start_ts REAL NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
                    synced_hash TEXT NOT NULL DEFAULT '', synced_at REAL, next_sync REAL NOT NULL,
                    last_error TEXT NOT NULL DEFAULT '', failures INTEGER NOT NULL DEFAULT 0,
                    lease_until REAL NOT NULL DEFAULT 0, lease_token TEXT NOT NULL DEFAULT '',
                    UNIQUE(case_id, signature));
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, at REAL NOT NULL,
                    level TEXT NOT NULL, message TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS cases_due ON cases(next_check);
                CREATE INDEX IF NOT EXISTS events_due ON events(next_sync);
            """)

    @contextmanager
    def db(self, write=False):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except Exception:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def set_meta(self, key, value):
        with self.db(True) as db:
            db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, encode(value)))

    def get_meta(self, key, default=None):
        with self.db() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_calendar_target(self, target):
        previous = self.get_meta("calendar_target", "")
        if previous != target:
            with self.db(True) as db:
                db.execute("UPDATE events SET synced_hash='', next_sync=?, last_error=''", (time.time(),))
            self.set_meta("calendar_target", target)

    def add_case(self, court, number, party, now=None):
        now = time.time() if now is None else now
        with self.db(True) as db:
            row = db.execute("SELECT id FROM cases WHERE court=? AND case_number=? AND party=?", (court, number, party)).fetchone()
            if row:
                return row[0]
            key = uuid.uuid4().hex
            db.execute("INSERT INTO cases(id,court,case_number,party,next_check,created) VALUES (?,?,?,?,?,?)", (key, court, number, party, now, now))
            return key

    def list_cases(self):
        with self.db() as db:
            rows = db.execute("SELECT * FROM cases ORDER BY created DESC").fetchall()
            events = db.execute("SELECT case_id,payload_hash,synced_hash,last_error FROM events").fetchall()
        output = []
        for row in rows:
            record = {k: row[k] for k in ("id", "court", "case_number", "party", "active", "state", "last_checked", "next_check", "last_error", "verdict_day", "verdict_count")}
            record["observed"] = json.loads(row["snapshot"])
            related = [e for e in events if e["case_id"] == row["id"]]
            record["calendar_pending"] = sum(e["payload_hash"] != e["synced_hash"] for e in related)
            record["calendar_error"] = next((e["last_error"] for e in related if e["last_error"]), "")
            output.append(record)
        return output

    def request_refresh(self, key, now=None):
        with self.db(True) as db:
            count = db.execute("UPDATE cases SET manual=1,next_check=? WHERE id=?", (time.time() if now is None else now, key)).rowcount
        return bool(count)

    def set_active(self, key, active, now=None):
        with self.db(True) as db:
            count = db.execute("UPDATE cases SET active=?,next_check=CASE WHEN ? THEN ? ELSE next_check END WHERE id=?",
                               (int(active), int(active), time.time() if now is None else now, key)).rowcount
        return bool(count)

    def claim_case(self, now, settings=None):
        with self.db(True) as db:
            rows = db.execute("SELECT * FROM cases WHERE (active=1 OR manual=1) AND next_check IS NOT NULL AND next_check<=? AND lease_until<=? ORDER BY next_check", (now, now)).fetchall()
            for row in rows:
                if settings and row["state"] == "awaiting_order" and not row["manual"]:
                    local = datetime.fromtimestamp(now, ZoneInfo(settings.timezone))
                    start = local.replace(hour=settings.watch_start_hour, minute=0, second=0, microsecond=0)
                    capped = row["verdict_count_day"] == local.date().isoformat() and row["verdict_count"] >= settings.verdict_checks_per_day
                    if local.hour < settings.watch_start_hour or local.hour >= settings.watch_end_hour or capped:
                        due = start if local < start and not capped else start + timedelta(days=1)
                        db.execute("UPDATE cases SET next_check=? WHERE id=?", (due.timestamp(), row["id"]))
                        continue
                token = uuid.uuid4().hex
                db.execute("UPDATE cases SET lease_until=?,lease_token=?,manual=0 WHERE id=?", (now + 600, token, row["id"]))
                return {**dict(row), "lease_token": token}
            return None

    def complete(self, key, token, observed, settings, now):
        with self.db(True) as db:
            row = db.execute("SELECT * FROM cases WHERE id=? AND lease_token=?", (key, token)).fetchone()
            if not row:
                return False
            moment = datetime.fromtimestamp(now, timezone.utc)
            plan = schedule(settings, moment, observed, dict(row))
            due = now if row["manual"] else plan["next_check"]
            db.execute("""UPDATE cases SET state=?,snapshot=?,last_checked=?,next_check=?,last_error='',failures=0,
                verdict_day=?,verdict_count=?,verdict_count_day=?,lease_until=0,lease_token='' WHERE id=?""",
                (plan["state"], encode(observed), now, due, plan["verdict_day"], plan["verdict_count"], plan["verdict_count_day"], key))
            signatures = {Hearing(**h).signature for h in observed["hearings"]}
            for hearing in calendar_hearings(observed, moment, settings.timezone):
                self._observe_event(db, dict(row), hearing, observed["summary"], settings.timezone, now,
                                    signatures, not observed.get("warnings"))
            self._log(db, key, now, "info", "사건정보 조회 완료" + (" · 주문 확인" if plan["state"] == "done" else ""))
            return True

    def _observe_event(self, db, case, hearing, summary, timezone_name, now, signatures, allow_move):
        existing = db.execute("SELECT * FROM events WHERE case_id=? AND signature=?", (case["id"], hearing.signature)).fetchone()
        if not existing and allow_move and hearing.moment(timezone_name).timestamp() > now:
            # 새 응답에서도 유효한 다른 기일을 덮어쓰지 않는다.
            candidates = db.execute("SELECT * FROM events WHERE case_id=? AND start_ts>? ORDER BY start_ts", (case["id"], now)).fetchall()
            existing = next((e for e in candidates if e["signature"] not in signatures), None)
        if existing:
            previous = Hearing(**json.loads(existing["hearing"]))
            if previous.day == hearing.day:
                hearing.clock = hearing.clock or previous.clock
                hearing.location = hearing.location or previous.location
        payload = event_payload(case, hearing, summary, timezone_name)
        encoded = encode(payload)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        if existing:
            if existing["payload_hash"] != digest or existing["signature"] != hearing.signature:
                db.execute("UPDATE events SET signature=?,hearing=?,start_ts=?,payload=?,payload_hash=?,next_sync=?,last_error='',failures=0 WHERE id=?",
                           (hearing.signature, encode(asdict(hearing)), hearing.moment(timezone_name).timestamp(), encoded, digest, now, existing["id"]))
        else:
            db.execute("INSERT INTO events(id,case_id,signature,hearing,start_ts,payload,payload_hash,next_sync) VALUES (?,?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, case["id"], hearing.signature, encode(asdict(hearing)), hearing.moment(timezone_name).timestamp(), encoded, digest, now))

    def fail_case(self, key, token, message, now, settings=None):
        with self.db(True) as db:
            row = db.execute("SELECT * FROM cases WHERE id=? AND lease_token=?", (key, token)).fetchone()
            if not row:
                return
            failures = row["failures"] + 1
            due = now if row["manual"] else now + min(21600, 900 * 2 ** min(failures - 1, 6))
            count, count_day = row["verdict_count"], row["verdict_count_day"]
            if settings and row["state"] == "awaiting_order":
                local = datetime.fromtimestamp(now, ZoneInfo(settings.timezone))
                today = local.date().isoformat()
                count = (count if count_day == today else 0) + 1
                count_day = today
                due_local = datetime.fromtimestamp(due, local.tzinfo)
                if not row["manual"] and (count >= settings.verdict_checks_per_day or due_local.hour >= settings.watch_end_hour or due_local.date() > local.date()):
                    due = (local.replace(hour=settings.watch_start_hour, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()
            db.execute("UPDATE cases SET last_error=?,failures=?,next_check=?,verdict_count=?,verdict_count_day=?,lease_until=0,lease_token='' WHERE id=?",
                       (message[:1000], failures, due, count, count_day, key))
            self._log(db, key, now, "error", message[:1000])

    def claim_event(self, now):
        with self.db(True) as db:
            row = db.execute("SELECT * FROM events WHERE payload_hash!=synced_hash AND next_sync<=? AND lease_until<=? ORDER BY next_sync LIMIT 1", (now, now)).fetchone()
            if not row:
                return None
            token = uuid.uuid4().hex
            db.execute("UPDATE events SET lease_token=?,lease_until=? WHERE id=?", (token, now + 180, row["id"]))
            return {**dict(row), "lease_token": token}

    def event_synced(self, event, now):
        with self.db(True) as db:
            db.execute("UPDATE events SET synced_hash=?,synced_at=?,last_error='',failures=0,lease_until=0,lease_token='' WHERE id=? AND lease_token=?",
                       (event["payload_hash"], now, event["id"], event["lease_token"]))

    def event_failed(self, event, message, now, retry_after=0):
        delay = max(retry_after, min(3600, 300 * 2 ** min(event["failures"], 5)))
        with self.db(True) as db:
            db.execute("UPDATE events SET last_error=?,failures=failures+1,next_sync=?,lease_until=0,lease_token='' WHERE id=? AND lease_token=?",
                       (message[:1000], now + delay, event["id"], event["lease_token"]))
            self._log(db, event["case_id"], now, "error", "캘린더 동기화 실패: " + message[:800])

    @staticmethod
    def _log(db, case_id, now, level, message):
        db.execute("INSERT INTO logs(case_id,at,level,message) VALUES (?,?,?,?)", (case_id, now, level, message))
        db.execute("DELETE FROM logs WHERE id<=(SELECT COALESCE(MAX(id),0)-5000 FROM logs)")

    def logs(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 100")]

    def case_events(self, key):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT id,signature,payload,synced_at,last_error FROM events WHERE case_id=? ORDER BY start_ts DESC", (key,))]
