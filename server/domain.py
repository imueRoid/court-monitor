from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
import html
import re
from zoneinfo import ZoneInfo

from case_summary import format_date, summarize_case
from court_client import display_text


UNKNOWN = {"", "확인되지 않음", "판결선고", "미확인"}


@dataclass
class Hearing:
    day: str
    clock: str
    kind: str
    location: str = ""
    result: str = ""

    @property
    def signature(self):
        return f"{self.day}|{self.kind}" + ("" if self.kind == "선고" else f"|{self.clock}")

    def moment(self, timezone):
        return datetime.combine(date.fromisoformat(self.day), time.fromisoformat(self.clock or "09:00"), ZoneInfo(timezone))


def clean_day(value):
    formatted = format_date(value)
    return formatted.replace(".", "-") if formatted else ""


def clean_clock(value):
    text = str(value or "").strip()
    match = re.search(r"(?<!\d)([0-2]?\d):([0-5]\d)(?!\d)", text)
    if match:
        hours, minutes = map(int, match.groups())
    else:
        digits = re.sub(r"\D", "", text)
        if len(digits) not in (4, 6):
            return ""
        hours, minutes = int(digits[:2]), int(digits[2:4])
    return f"{hours:02}:{minutes:02}" if hours < 24 and minutes < 60 else ""


def hearing_kind(value):
    text = display_text(value)
    for word in ("선고", "변론준비", "변론", "공판준비", "공판", "조정", "심문", "심리", "화해"):
        if word in text:
            return word
    return text.strip() or "기일"


def extract_hearings(result):
    data = result.get("general", {}).get("data", {})
    hearings = {}
    cancelled = set()
    for row in data.get("dlt_rcntDxdyLst", []) or []:
        day = clean_day(row.get("dxdyYmd"))
        if not day:
            continue
        kind = hearing_kind(row.get("dxdyKndNm"))
        outcome = display_text(row.get("dxdyRsltNm"))
        if re.search(r"변경|취소|연기", outcome):
            cancelled.add((day, kind))
            continue
        item = Hearing(day, clean_clock(row.get("dxdyHm")), kind,
                       display_text(row.get("dxdyPlcNm")), outcome)
        hearings[item.signature] = item
    progress = list(data.get("dlt_hrngProgCurst", []) or [])
    for page in result.get("progress", []):
        progress.extend(page.get("data", {}).get("dlt_csProgCtt", []) or [])
    for row in progress:
        text = " ".join(display_text(row.get(k)) for k in ("progCtt", "progCtt1", "progDts"))
        # 송달·서류 접수를 기일로 오인하지 않는다.
        match = re.match(r"\s*(판결선고|(?:선고|변론준비|변론|공판준비|공판|조정|심문|심리|화해)기일)(?:\s|\(|$)", text)
        day = clean_day(row.get("progYmd"))
        if not match or not day:
            continue
        kind = hearing_kind(match[1])
        outcome = display_text(row.get("progRslt"))
        if re.search(r"변경|취소|연기", outcome) or (day, kind) in cancelled:
            continue
        item = Hearing(day, clean_clock(text), kind, "", outcome)
        # 날짜·종류가 같은 공식 기일목록의 시간·장소를 우선한다.
        if not any(h.day == day and h.kind == kind for h in hearings.values()):
            hearings[item.signature] = item
    return sorted(hearings.values(), key=lambda h: (h.day, h.clock, h.kind))


def extract_progress(result):
    """Normalize official progress fields; never expose the raw court response."""
    data = result.get("general", {}).get("data", {})
    rows = list(data.get("dlt_hrngProgCurst", []) or [])
    for page in result.get("progress", []):
        rows.extend(page.get("data", {}).get("dlt_csProgCtt", []) or [])
    found = {}
    def text(value):
        return display_text(value) if isinstance(value, (str, int, float)) else ""
    for row in rows:
        day = clean_day(row.get("progYmd"))
        content = text(row.get("progCtt"))
        if not content:
            parts = [text(row.get(key)) for key in ("progCtt1", "progCtt2", "progCtt3", "progDts")]
            content = " ".join(dict.fromkeys(part for part in parts if part))
        outcome = text(row.get("progRslt"))
        if day and (content or outcome):
            item = {"day": day, "content": content, "result": outcome}
            found.setdefault((day, content, outcome), item)
    return sorted(found.values(), key=lambda item: item["day"], reverse=True)


def has_verdict_date(observed):
    return bool(clean_day(observed.get("summary", {}).get("선고일자"))) or any(
        row.get("kind") == "선고" for row in observed.get("hearings", []))


def progress_interval(observed, now, timezone):
    """Include boundary dates because court progress rows have no time of day."""
    local = now.astimezone(ZoneInfo(timezone))
    today = local.date().isoformat()
    hearings = [Hearing(**row) for row in observed.get("hearings", [])]
    previous = [h for h in hearings if h.day < today or
                h.day == today and h.clock and h.moment(timezone) <= now]
    previous = max(previous, key=lambda h: (h.day, h.clock), default=None)
    upcoming = next_hearing(observed, now, timezone)
    end = upcoming.day if upcoming else today
    rows = [row for row in observed.get("progress", [])
            if previous and previous.day <= row["day"] <= end]
    return {"previous": asdict(previous) if previous else None,
            "next": asdict(upcoming) if upcoming else None,
            "through": end, "entries": rows}


def snapshot(result):
    summary = summarize_case(result)
    return {"summary": summary, "hearings": [asdict(h) for h in extract_hearings(result)],
            "progress": extract_progress(result), "warnings": result.get("warnings", [])}


def has_order(summary):
    return summary.get("판결 주문", "").strip() not in UNKNOWN


def schedule(settings, now, observed, previous):
    zone = ZoneInfo(settings.timezone)
    local = now.astimezone(zone)
    today = local.date().isoformat()
    hearings = [Hearing(**row) for row in observed["hearings"]]
    verdict_days = [h.day for h in hearings if h.kind == "선고"]
    verdict_day = clean_day(observed["summary"].get("선고일자")) or max(verdict_days, default="") or previous.get("verdict_day", "")
    day_count = previous.get("verdict_count", 0) if previous.get("verdict_count_day") == today else 0
    if has_order(observed["summary"]):
        return {"state": "done", "next_check": None, "verdict_day": verdict_day,
                "verdict_count": day_count, "verdict_count_day": today}
    start = local.replace(hour=settings.watch_start_hour, minute=0, second=0, microsecond=0)
    end = local.replace(hour=settings.watch_end_hour, minute=0, second=0, microsecond=0)
    baseline = local + timedelta(hours=settings.base_poll_hours)
    state = "monitoring"
    if verdict_day and verdict_day <= today:
        state = "awaiting_order"
        # 기일 시작 전 조회는 선고 후 확인 횟수로 계산하지 않는다.
        verdict = next((h for h in hearings if h.kind == "선고" and h.day == today), None)
        first_check = max(start, verdict.moment(settings.timezone) + timedelta(minutes=settings.hearing_delay_minutes)) if verdict else start
        if local >= first_check:
            day_count += 1
        if local < first_check and first_check < end:
            due = first_check
        elif local < end and day_count < settings.verdict_checks_per_day:
            due = min(local + timedelta(minutes=settings.verdict_retry_minutes), end)
        else:
            due = start + timedelta(days=1)
    else:
        due = baseline
        for hearing in hearings:
            moment = hearing.moment(settings.timezone) + timedelta(minutes=settings.hearing_delay_minutes)
            if moment > local:
                due = min(due, moment)
        # 일반 기일 당일에는 차회 기일 반영을 확인하기 위해 한 번 더 조회한다.
        if any(h.day == today for h in hearings):
            if local < start:
                due = min(due, start)
            elif local < end:
                due = min(due, local + timedelta(hours=3), end)
    return {"state": state, "next_check": due.timestamp(), "verdict_day": verdict_day,
            "verdict_count": day_count, "verdict_count_day": today}


def calendar_hearings(observed, now, timezone):
    today = now.astimezone(ZoneInfo(timezone)).date().isoformat()
    hearings = [Hearing(**row) for row in observed["hearings"]]
    upcoming = [h for h in hearings if h.moment(timezone) >= now]
    chosen = [h for h in hearings if h.day == today]
    if upcoming:
        # 차회 기일을 등록하되, 같은 날 지난 기일과 다음 기일을 함께 보존한다.
        chosen.extend(h for h in upcoming if h.day == upcoming[0].day)
    past_verdict = [h for h in hearings if h.kind == "선고" and h.day <= today]
    if past_verdict:
        chosen.append(past_verdict[-1])
    verdict_day = clean_day(observed["summary"].get("선고일자"))
    if verdict_day and has_order(observed["summary"]) and not any(h.kind == "선고" and h.day == verdict_day for h in chosen):
        chosen.append(Hearing(verdict_day, "", "선고"))
    return list({h.signature: h for h in chosen}.values())


def next_hearing(observed, now, timezone):
    today = now.astimezone(ZoneInfo(timezone)).date().isoformat()
    candidates = [Hearing(**row) for row in observed.get("hearings", [])]
    candidates = [h for h in candidates if h.day > today or h.day == today and (not h.clock or h.moment(timezone) >= now)]
    return min(candidates, key=lambda h: (h.day, h.clock), default=None)


def event_payload(case, hearing, summary, timezone):
    title = f"[{hearing.kind}] {case['court']} {case['case_number']}"
    lines = [f"법원: {case['court']}", f"사건번호: {case['case_number']}", f"당사자: {case['party']}", f"기일: {hearing.kind}"]
    if hearing.result:
        lines.append("기일 결과: " + hearing.result)
    verdict_day = clean_day(summary.get("선고일자"))
    if hearing.kind == "선고" and has_order(summary) and verdict_day == hearing.day:
        lines.extend(["", "판결 주문 / 종국결과", summary["판결 주문"], "", "상소제기 여부: " + summary.get("상소제기 여부", "확인되지 않음"),
                      "상소사건번호: " + summary.get("상소사건번호", "확인되지 않음")])
        title = "[선고·주문 확인] " + f"{case['court']} {case['case_number']}"
    payload = {"summary": title, "description": "<br>".join(html.escape(line) for line in lines),
               "location": hearing.location or case["court"], "visibility": "private",
               "extendedProperties": {"private": {"app": "court-monitor", "case_id": case["id"]}}}
    if hearing.clock:
        start = hearing.moment(timezone)
        payload.update(start={"dateTime": start.isoformat(), "timeZone": timezone},
                       end={"dateTime": (start + timedelta(minutes=30)).isoformat(), "timeZone": timezone})
    else:
        payload.update(start={"date": hearing.day}, end={"date": (date.fromisoformat(hearing.day) + timedelta(days=1)).isoformat()})
    return payload
