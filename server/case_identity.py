"""Display the court's case category and the role of the searched party."""
import re
import unicodedata

from court_client import display_text


_TASK_CATEGORY = {
    "CV": "민사", "DC": "민사", "DJ": "민사",
    "HG": "형사", "HY": "형사", "EY": "형사",
    "HS": "행정",
    "KS": "가사", "PT": "특허", "SB": "소년", "HP": "회생·파산",
}
# Official case codes for common cases, also available before the first lookup.
_CASE_CODES = {
    code: category
    for category, codes in {
        "민사": "가합 가단 가소 나 다 머 재가합 재가단 재가소 재나 재다 재머 차 차전 카 카합 카단 카기 카명 카불",
        "형사": "고합 고단 고정 고약 고약전 노 도 재고합 재고단 재고정 재고약 재노 재도",
        "행정": "구 구합 구단 누 두 아 우 재구 재구합 재구단 재누 재두 재아 재우",
        "가사": "드합 드단 너 느합 느단 르 므",
        "특허": "허 후 재허 재후",
    }.items()
    for code in codes.split()
}
_MASK = frozenset("○●□*＊")


def case_category(case_number, task=""):
    if task in _TASK_CATEGORY:
        return _TASK_CATEGORY[task]
    match = re.fullmatch(r"\d{4}([가-힣]+)\d+", case_number)
    return _CASE_CODES.get(match[1], "") if match else ""


def _name(value):
    if not isinstance(value, str):
        return ""
    return "".join(unicodedata.normalize("NFC", display_text(value)).split()).casefold()


def _role(value):
    return display_text(value) if isinstance(value, str) and len(value) <= 80 else ""


def _matching_role(party, candidates):
    def matches_mask(name):
        return (len(name) == len(party) and any(ch in _MASK for ch in name)
                and any(ch not in _MASK for ch in name)
                and all(ch in _MASK or ch == expected for ch, expected in zip(name, party)))

    if not party:
        return None
    exact = {role for name, role in candidates if name == party and role}
    roles = exact or {role for name, role in candidates if matches_mask(name) and role}
    if not roles:
        return None
    return next(iter(roles)) if len(roles) == 1 else ""


def case_identity(result):
    query = result.get("query", {})
    task = result.get("case_type", "")
    category = case_category(query.get("case_number", ""), task)
    data = result.get("general", {}).get("data", {})
    basic = data.get("dma_csBasCtt") or {}
    party = _name(query.get("party"))
    candidates = [
        (_name(row.get("btprtNm")), _role(row.get("btprtDvsNm")))
        for row in data.get("dlt_btprtCttLst", []) or [] if isinstance(row, dict)
    ]
    official_role = _matching_role(party, candidates)
    if official_role is not None:
        return {"case_category": category, "party_role": official_role}
    candidates = []
    if category in ("민사", "행정"):
        candidates.extend(((_name(basic.get("rprsClmntNm")), "원고"),
                           (_name(basic.get("rprsAcsdNm")), "피고")))
    if category == "형사":
        selected_role = _role(basic.get("acsdDvsNm")) or "피고인"
        selected_seq = str(basic.get("btprtSeq") or "")
        candidates.extend(((_name(basic.get("btprtNm")), selected_role),
                           (_name(basic.get("rprsAcsNm")), "피고인"),
                           (_name(basic.get("prstrNm")), "검사")))
        for row in data.get("dlt_acsCttLst", []) or []:
            if isinstance(row, dict):
                role = selected_role if selected_seq and str(row.get("btprtSeq")) == selected_seq else "피고인"
                candidates.append((_name(row.get("btprtNmOrg") or row.get("btprtNm") or row.get("acsNm")), role))
    return {"case_category": category,
            "party_role": _matching_role(party, candidates) or ""}
