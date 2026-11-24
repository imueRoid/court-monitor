"""조회 성공 사건의 요약을 로컬 앱 데이터 폴더에 저장한다."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from case_summary import summarize_case


class HistoryError(Exception):
    pass


def default_history_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "ScourtCaseSearch" / "history.json"


def record_id(query) -> str:
    return hashlib.sha256("\0".join(query[k] for k in ("court", "case_number", "party")).encode("utf-8")).hexdigest()


class CaseHistory:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else default_history_path()
        self.records: dict[str, dict] = {}

    def _read(self):
        if not self.path.exists():
            return {}
        try:
            contents = json.loads(self.path.read_text("utf-8"))
            if contents.get("version") != 1 or not isinstance(contents.get("cases"), list):
                raise ValueError("unsupported history format")
            records = {}
            for record in contents["cases"]:
                query = record["query"]
                if not all(isinstance(query.get(k), str) and query[k] for k in ("court", "case_number", "party")):
                    raise ValueError("invalid case record")
                if not isinstance(record.get("summary"), dict):
                    raise ValueError("invalid summary")
                records[record_id(query)] = record
            return records
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise HistoryError("조회 이력 파일을 읽지 못했습니다. 기존 파일은 덮어쓰지 않았습니다.") from exc

    def load(self):
        self.records = self._read()
        return self.items()

    def items(self):
        return sorted(self.records.items(), key=lambda item: item[1].get("queried_at") or item[1].get("saved_at", ""), reverse=True)

    def save_result(self, result):
        query = result.get("query", {})
        if not all(isinstance(query.get(k), str) and query[k].strip() for k in ("court", "case_number", "party")):
            raise HistoryError("가져온 파일에 법원·사건번호·당사자명이 없습니다.")
        query = {k: query[k].strip() for k in ("court", "case_number", "party")}
        record = {"query": query, "summary": summarize_case(result), "queried_at": result.get("queried_at") or "",
                  "saved_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        records = self._read()  # 다른 실행 창에서 추가한 사건도 보존한다.
        key = record_id(query)
        records[key] = record
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".history-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump({"version": 1, "cases": list(records.values())}, handle, ensure_ascii=False, indent=2)
            os.replace(temporary, self.path)
        except OSError as exc:
            raise HistoryError("조회는 완료됐지만 이력을 저장하지 못했습니다. 저장 폴더의 쓰기 권한을 확인하세요.") from exc
        finally:
            if temporary is not None and temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass
        self.records = records
        return key
