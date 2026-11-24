"""서버 응답에서 사용자가 볼 네 가지 사건 결과만 구성한다."""
from __future__ import annotations

import re
from datetime import datetime

from court_client import display_text


def format_date(value) -> str:
    digits = re.sub(r"\D", "", str(value or ""))[:8]
    if len(digits) != 8:
        return ""
    try:
        return datetime.strptime(digits, "%Y%m%d").strftime("%Y.%m.%d")
    except ValueError:
        return ""


def _first(data, *keys):
    return next((data[k] for k in keys if data.get(k) not in (None, "")), "")


def _case_number(value, kinds) -> str:
    text = re.sub(r"\s+", "", display_text(value))
    if re.fullmatch(r"\d{4}[가-힣]+\d+", text):
        return text
    if re.fullmatch(r"\d{14}", text):
        kind = next((name for name, code in kinds.items() if code == text[4:7]), "")
        if kind and int(text[7:]):
            return f"{text[:4]}{kind}{int(text[7:])}"
    return ""


def summarize_case(result: dict, kinds: dict | None = None) -> dict[str, str]:
    kinds = kinds or result.get("case_kinds", {})
    data = result.get("general", {}).get("data", {})
    basic = data.get("dma_csBasCtt") or {}
    if not basic and isinstance(result.get("summary"), dict):
        return {name: display_text(result["summary"].get(name)) or "확인되지 않음"
                for name in ("판결 주문", "선고일자", "상소제기 여부", "상소사건번호")}
    # 사용자가 실제 응답으로 확인한 형사사건 주문 필드를 최우선으로 사용한다.
    judgment = display_text(_first(basic, "btprtUltmtThrstCtt", "jdgmnOrdCtt", "jdgmnThrstCtt", "jdgmnCtt"))
    judgment = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", judgment)

    date = format_date(_first(basic, "jdgmnPrnYmd", "jdgmnYmd", "sntncYmd"))
    # 형사사건의 주문 또는 판결선고 결과와 함께 제공된 종국일자를 사용한다.
    if not date and (judgment or "선고" in display_text(basic.get("crmcsUltmtDvsNm"))):
        date = format_date(basic.get("btprtUltmtYmd"))
    if not date:
        completed = []
        for page in result.get("progress", []):
            for row in page.get("data", {}).get("dlt_csProgCtt", []) or []:
                text = " ".join(display_text(row.get(k)) for k in ("progCtt", "progCtt1", "progRslt"))
                # 예정된 선고기일을 실제 선고일자로 사용하지 않는다.
                if "판결선고" in text and not re.search(r"변경|연기|취소|예정", text):
                    day = format_date(row.get("progYmd"))
                    if day and day <= datetime.now().strftime("%Y.%m.%d"):
                        completed.append(day)
        date = max(completed, default="")

    # 공식 종국결과 필드의 값을 그대로 사용한다. 승패 문구나 사건구분을 추측하지 않는다.
    civil_result = display_text(basic.get("csUltmtDvsNm"))
    if civil_result:
        if not date:
            date = format_date(basic.get("csUltmtYmd"))
        if not judgment and date:
            judgment = civil_result

    upper_raw = _first(basic, "hcrtUserCsNo", "hcrtCsNo", "apelUserCsNo", "applUserCsNo")
    upper_number = _case_number(upper_raw, kinds)
    upper_court = display_text(_first(basic, "hcrtCortNm", "apelCortNm", "applCortNm"))
    stages = data.get("dlt_inscrtDtsLst", []) or []
    for row in stages:
        if upper_raw and (str(row.get("csNo", "")) == str(upper_raw) or
                          _case_number(row.get("userCsNo"), kinds) == upper_number and upper_number):
            upper_number = _case_number(row.get("userCsNo"), kinds) or upper_number
            upper_court = display_text(row.get("cortNm")) or upper_court
            break

    # 상급심 번호가 별도로 없을 때는 심급목록에서 바로 다음 심급만 찾는다.
    if not upper_number:
        current = result.get("query", {}).get("case_number", "")
        match = re.fullmatch(r"\d{4}([가-힣]+)\d+", current)
        next_kinds = {
            "고합": {"노"}, "고단": {"노"}, "고정": {"노"}, "노": {"도"},
            "가합": {"나"}, "가단": {"나"}, "가소": {"나"}, "나": {"다"},
            "구합": {"누"}, "구단": {"누"}, "누": {"두"},
        }.get(match[1] if match else "", set())
        for row in stages:
            number = _case_number(row.get("userCsNo") or row.get("csNo"), kinds)
            higher = re.fullmatch(r"\d{4}([가-힣]+)\d+", number)
            if higher and higher[1] in next_kinds:
                upper_number, upper_court = number, display_text(row.get("cortNm"))
                break

    # 항소/상고 제기자 코드는 공식 SSGO10GF01 화면의 매핑을 따른다.
    who = {"01": "검사", "02": "피고인", "03": "변호인", "04": "대리인",
           "05": "보조인", "06": "증인", "07": "감정인", "08": "통역인",
           "09": "번역인", "10": "청구인", "11": "피의자", "12": "배상신청인",
           "13": "직권", "14": "쌍방", "99": "기타"}
    appeals = []
    for kind, code_key, date_key in (
        ("항소", "apelCrmcsLwstRltnrDvsCd", "acsApelPrpndYmd"),
        ("상고", "applCrmcsLwstRltnrDvsCd", "acsApplPrpndYmd"),
    ):
        code, day = str(basic.get(code_key) or ""), format_date(basic.get(date_key))
        if code in who or day:
            detail = " · ".join(x for x in (who.get(code, ""), day) if x)
            appeals.append(f"{kind} 제기됨" + (f" ({detail})" if detail else ""))
    if not appeals and upper_number:
        appeals.append("상소사건 확인됨")
    if not appeals:
        flag = _first(basic, "aplPrpndYn", "apelPrpndYn", "applPrpndYn")
        if flag == "Y":
            appeals.append("제기됨")
        elif flag == "N":
            appeals.append("제기 내역 없음")
        else:
            appeals.append("제기 내역 확인되지 않음")
    appeal_result = display_text(basic.get("aplPrpndRsltNm"))
    if appeal_result:
        appeals.append(appeal_result)

    return {
        "판결 주문": judgment or "확인되지 않음",
        "선고일자": date or "확인되지 않음",
        "상소제기 여부": " / ".join(appeals),
        "상소사건번호": " ".join(x for x in (upper_court, upper_number) if x) if upper_number else "확인되지 않음",
    }
