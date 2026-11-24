"""대한민국 법원 사건검색 클라이언트. 직접 입력 및 로컬 OCR을 지원한다."""
from __future__ import annotations

import base64
import html
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import requests

BASE = "https://ssgo.scourt.go.kr"
ENTRY = BASE + "/ssgo/index.on?cortId=www"
W2 = "{http://www.inswave.com/websquare}"
XF = "{http://www.w3.org/2002/xforms}"


class CourtError(Exception):
    """네트워크, 보안문자 또는 조회 결과 오류."""

    def __init__(self, message, code=""):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CaseNumber:
    year: str
    kind: str
    serial: str

    @classmethod
    def parse(cls, value: str) -> "CaseNumber":
        value = re.sub(r"\s+", "", value)
        match = re.fullmatch(r"(\d{4})([가-힣]+)(\d{1,7})", value)
        if not match or not 1900 <= int(match[1]) <= datetime.now().year:
            raise CourtError("사건번호를 연도·사건부호·일련번호 형식으로 입력하세요.")
        if int(match[3]) == 0:
            raise CourtError("사건 일련번호는 1 이상이어야 합니다.")
        return cls(match[1], match[2], str(int(match[3])))

    def __str__(self) -> str:
        return f"{self.year}{self.kind}{self.serial}"


def display_text(value: Any) -> str:
    """서버 문자열은 HTML로 실행하지 않고 일반 텍스트로 표시한다."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", str(value), flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def leaf_fields(value: Any, path: str = ""):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from leaf_fields(child, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from leaf_fields(child, f"{path}[{index}]")
    else:
        yield path, value


def outcome_fields(data: Any, labels: dict[str, str] | None = None) -> list[dict]:
    """원문 경로를 유지. 주문, 형량, 종국결과, 코드 값을 구별한다."""
    labels = labels or {}
    result = []
    for path, raw in leaf_fields(data):
        if raw is None or raw == "":
            continue
        key = path.rsplit(".", 1)[-1].split("[")[0]
        label = labels.get(key, key)
        # 이름 전체의 rslt 같은 넓은 매칭은 주문으로 확정하지 않는다.
        if key == "btprtUltmtThrstCtt" or re.search(r"주문|j(?:dgmt|dgm|dgmn).*?(?:ord|ctt)|jmun|pan(?:gyeol)?j?umun|judg.*?(?:order|text)", key + " " + label, re.I):
            category = "판결 주문 필드"
        elif re.search(r"형량|선고형|벌금|징역|집행유예|sntnc|sentence|punish|penalty|fineAmt", key + " " + label, re.I):
            category = "형·처분 관련 필드"
        elif re.search(r"종국|결과|판결|선고|ultmt|jdgm|judg|rslt|dsps", key + " " + label, re.I):
            category = "판결·결과 관련 필드"
        elif isinstance(raw, str) and re.search(r"징역|금고|벌금|집행유예|무죄|선고유예|공소기각|면소|형의\s*면제|피고인을", display_text(raw)):
            # 서버가 새 필드명을 추가해도 관련 원문을 놓치지 않는다.
            # 문자열만으로 주문이라고 단정하지는 않는다.
            category = "형·처분 표현을 포함한 필드"
        else:
            continue
        if key.lower().endswith(("cd", "code")):
            category = "결과 코드 (원문)"
        result.append({"category": category, "path": path, "label": label,
                       "value": display_text(raw), "raw": raw})
    return result


class CourtClient:
    def __init__(self, timeout: int = 30):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
            "Referer": ENTRY, "Origin": BASE,
        })
        self.timeout = timeout
        self.courts: dict[str, str] = {}
        self.kinds: dict[str, str] = {}
        self.tasks: dict[str, str] = {}
        self.routes: dict[str, dict[str, str]] = {}
        self.labels: dict[str, str] = {}
        self._schemas: dict[str, ET.Element] = {}
        self._captcha_token = ""
        self._captcha_image = b""
        self.ban_words: list[str] = []

    def close(self):
        self.session.close()

    def _request(self, method: str, path: str, **kwargs):
        if not path.startswith("/ssgo/") or urlparse(path).netloc:
            raise CourtError("공식 사건검색 경로만 사용할 수 있습니다.")
        try:
            response = self.session.request(method, BASE + path, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise CourtError("법원 서버에 연결할 수 없습니다. 인터넷 연결과 사이트 점검 여부를 확인하세요.") from exc
        # 본문을 먼저 읽어 HTTP 오류에 담긴 공식 오류 안내도 전달한다.
        response.encoding = "utf-8"
        return response

    def _get_text(self, path: str) -> str:
        response = self._request("GET", path)
        if not response.ok or "web firewall" in response.text.lower():
            raise CourtError("법원 사이트가 접속을 제한했거나 점검 중입니다. 잠시 후 다시 시도하세요.")
        return response.text

    def _post(self, path: str, body: dict) -> dict:
        prefix = "mf_ssgoTopMainTab_contents_content1_body_"
        if path.endswith("/getCaptchaInf.on"):
            submission_id = prefix + "sbm_captcha"
        elif path.endswith("/selectHmpgMain.on"):
            submission_id = prefix + "sbm_search"
        else:
            tab = "ssgoTab2" if "ProgCtt.on" in path else "ssgoTab1"
            submission_id = prefix + f"wfSsgoDetail_ssgoCsDetailTab_contents_{tab}_body_sbm_search"
        response = self._request("POST", path, json=body, headers={
            "Accept": "application/json", "Content-Type": "application/json;charset=UTF-8",
            "submissionid": submission_id})
        try:
            payload = response.json()
        except ValueError as exc:
            raise CourtError("서버가 JSON 대신 다른 화면을 반환했습니다. 사이트 점검 또는 접속 제한을 확인하세요.") from exc
        if not isinstance(payload, dict):
            raise CourtError("서버 응답 형식이 변경되었습니다.")
        errors = payload.get("errors")
        if not response.ok or payload.get("status", 200) != 200 or errors:
            message = errors.get("errorMessage") if isinstance(errors, dict) else None
            raise CourtError(display_text(message or payload.get("message") or f"HTTP {response.status_code}"),
                             errors.get("errorCode", "") if isinstance(errors, dict) else "")
        if not isinstance(payload.get("data"), dict):
            raise CourtError("서버 응답에 사건 데이터가 없습니다.")
        return payload

    def initialize(self):
        entry = self._get_text("/ssgo/index.on?cortId=www")
        cbx = self._get_text("/ssgo/cm/js/biz/ssgocbx.js")
        common = self._get_text("/ssgo/cm/js/ssgo.js")
        self.courts = {name: code for code, name in re.findall(
            r'addItem\(\s*"(\d{6})"\s*,\s*"([^"\n]+)"\s*\)', cbx)}
        self.kinds = {name: code for code, name in re.findall(
            r'addItem\(\s*"(\d{3})"\s*,\s*"([가-힣]+)"\s*\)', cbx)}
        self.tasks = dict(re.findall(r'"ctt\.csDvsNm\.([가-힣]+)"\s*:\s*"([A-Z]+)"', entry))
        # 공식 업무별 URL 테이블에서 일반/진행 화면만 읽는다. JS를 실행하지 않는다.
        for task, general, progress in re.findall(
            r'\b([A-Z]{2})\s*:\s*\{\s*ID\s*:\s*"[A-Z]{2}".*?'
            r'CS_GNRL_CTT\s*:\s*"([^"]+)"\s*,\s*CS_(?:PROG|ROG)_CTT\s*:\s*"([^"]*)"', common, re.S
        ):
            self.routes[task] = {"general": "/ssgo" + general,
                                 "progress": "/ssgo" + progress if progress else ""}
        match = re.search(r'"clt\.ssgo\.ban\.word\.list"\s*:\s*"([^"]+)"', entry)
        self.ban_words = match[1].split(",") if match else []
        if not self.courts or not self.kinds or not self.routes:
            raise CourtError("법원 목록 또는 조회 화면 구조가 변경되었습니다. 프로그램 업데이트가 필요합니다.")
        self._post("/ssgo/ssgo10l/selectHmpgMain.on", {"dma_search": {
            "cortCd": "", "cdScope": "ALL", "csNoHistLst": "", "csDvsCd": "",
            "csYr": "", "csSerial": "", "btprNm": "", "answer": "", "fullCsNo": ""}})
        return self.courts

    def captcha(self) -> bytes:
        self._captcha_token = ""
        self._captcha_image = b""
        data = self._post("/ssgo/ssgo10l/getCaptchaInf.on", {})["data"].get("dma_captchaInf", {})
        image = data.get("image", "")
        token = data.get("answer", "")
        if not image or not token:
            raise CourtError("보안문자를 받지 못했습니다. 새로고침하세요.")
        try:
            image_bytes = base64.b64decode(image.split(",")[-1], validate=True)
        except (ValueError, TypeError) as exc:
            raise CourtError("보안문자 이미지 형식이 변경되었습니다.") from exc
        self._captcha_token = token
        self._captcha_image = image_bytes
        return image_bytes

    def search_auto(self, court: str, number: str, party: str, recognizer) -> dict:
        self.validate(court, number, party)
        for attempt in range(3):
            try:
                # 조회 버튼을 누를 때마다 반드시 새 도전을 받아 사용한다.
                image = self.captcha()
                answer = recognizer.recognize(image)
                return self.search(court, number, party, answer)
            except CourtError as exc:
                self._captcha_token, self._captcha_image = "", b""
                retryable = bool(re.search(r"보안문자|자동\s*입력|캡[차챠처]|captcha", str(exc), re.I)) or exc.code == "W_0107"
                if not retryable:
                    raise
                if attempt == 2:
                    raise CourtError("자동 인증을 3회 시도했지만 조회하지 못했습니다. 잠시 후 사건 조회를 다시 눌러 주세요.", exc.code) from exc
        raise CourtError("보안문자를 확인하고 다시 조회하세요.")

    def schema(self, path: str) -> ET.Element:
        if path not in self._schemas:
            try:
                root = ET.fromstring(self._get_text(path))
            except ET.ParseError as exc:
                raise CourtError("공식 조회 화면 구조를 읽지 못했습니다.") from exc
            self._schemas[path] = root
            for tag in ("key", "column"):
                for item in root.iter(W2 + tag):
                    if item.get("id") and item.get("name"):
                        self.labels.setdefault(item.get("id"), item.get("name"))
        return self._schemas[path]

    def _query_screen(self, path: str, params: dict, page: dict | None = None) -> dict:
        root = self.schema(path)
        submission = next((s for s in root.iter(XF + "submission") if s.get("id") == "sbm_search"), None)
        if submission is None:
            raise CourtError("공식 화면에서 조회 요청을 찾지 못했습니다.")
        body = {}
        ref = submission.get("ref", "")
        for item in root.iter(W2 + "dataMap"):
            name = item.get("id", "")
            if name not in ref:
                continue
            values = page if name == "dma_pageInfo" else params
            body[name] = {k.get("id"): (values or {}).get(k.get("id"), "")
                          for k in item.iter(W2 + "key")}
        return self._post(submission.get("action", ""), body)

    def validate(self, court: str, number: str, party: str) -> tuple[CaseNumber, str]:
        case = CaseNumber.parse(number)
        party = re.sub(r"\s+", "", party)
        if court not in self.courts:
            raise CourtError("목록에서 법원을 선택하세요.")
        if len(party) < 2:
            raise CourtError("당사자명을 2글자 이상 입력하세요.")
        if party in self.ban_words:
            raise CourtError("법원 사이트에서 제한하는 검색어입니다. 당사자명을 더 길게 입력하세요.")
        if case.kind not in self.kinds or self.tasks.get(case.kind) not in self.routes:
            raise CourtError("지원하지 않는 사건구분이거나 공식 사이트 구조가 변경되었습니다.")
        return case, party

    def search(self, court: str, number: str, party: str, answer: str) -> dict:
        case, party = self.validate(court, number, party)
        answer = answer.strip()
        if not re.fullmatch(r"[0-9A-Za-z]{6}", answer):
            raise CourtError("보안문자 6자리를 입력하세요.")
        if not self._captcha_token:
            raise CourtError("보안문자를 먼저 새로고침하세요.")
        route = self.routes[self.tasks[case.kind]]
        params = {"cortCd": self.courts[court], "csYear": case.year,
                  "csDvsCd": self.kinds[case.kind], "csDvsNm": case.kind,
                  "csSerial": case.serial, "btprtNm": party,
                  "captchaAnswer": answer + self._captcha_token}
        self._captcha_token = ""  # 시도 후에는 반드시 새 보안문자를 받는다.
        self._captcha_image = b""
        general = self._query_screen(route["general"], params)
        basic = general["data"].get("dma_csBasCtt")
        if not isinstance(basic, dict) or not basic.get("csNo"):
            raise CourtError("조회된 사건이 없습니다. 법원·사건번호·당사자명을 확인하세요.")
        # 일반 조회에서 반환한 세션·암호화 사건번호를 그대로 진행 조회에 전달한다.
        params.update({"csNo": basic["csNo"], "encCsNo": basic.get("encCsNo", ""),
                       "btprtSeq": basic.get("btprtSeq", ""), "captchaAnswer": "",
                       "progCttDvs": "0", "inqScop": "01"})
        progress_pages = []
        warnings = []
        if route["progress"]:
            try:
                progress_pages.append(self._query_screen(route["progress"], params, {
                    "pageNo": 1, "pageSize": 500, "totalYn": "Y"}))
            except CourtError as exc:
                warnings.append("일반내용은 조회되었으나 진행내용 조회 실패: " + str(exc))
        # 응답에 포함된 하위 화면의 공식 필드명도 읽는다. 실패해도 사건 결과는 유지한다.
        root = self.schema(route["general"])
        text = "".join(root.itertext())
        for child in dict.fromkeys(re.findall(r'/ui/ssgo003/SSGO003[A-Z0-9]+\.xml', text)):
            try:
                self.schema("/ssgo" + child)
            except CourtError:
                warnings.append("일부 필드 한글 이름을 받지 못했습니다: " + child)
        result = {"queried_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                  "query": {"court": court, "case_number": str(case), "party": party},
                  "general": general, "progress": progress_pages,
                  "warnings": warnings, "labels": dict(self.labels), "case_kinds": dict(self.kinds),
                  "case_type": self.tasks[case.kind]}
        result["outcomes"] = outcome_fields({"general": general["data"],
            "progress": [p["data"] for p in progress_pages]}, self.labels)
        from case_summary import summarize_case
        result["summary"] = summarize_case(result, self.kinds)
        return result
