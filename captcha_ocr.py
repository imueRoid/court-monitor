"""PC 안에서 수행하는 보안문자 OCR. 외부 인식 서비스는 사용하지 않는다."""
from __future__ import annotations

import io
import re
import threading

from PIL import Image

from court_client import CourtError


def prepare_image(image_bytes: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            rgba = source.convert("RGBA")
            # 법원 PNG는 투명 배경이다. 검은 배경으로 변환하면 숫자가 소실된다.
            white = Image.new("RGBA", rgba.size, "white")
            white.alpha_composite(rgba)
            output = io.BytesIO()
            white.convert("RGB").save(output, "PNG")
            return output.getvalue()
    except (OSError, ValueError) as exc:
        raise CourtError("보안문자 이미지를 읽지 못했습니다. 새로고침하세요.") from exc


class CaptchaRecognizer:
    def __init__(self):
        # 라이브러리가 스레드별 인스턴스를 권장하므로 인스턴스를 공유하지 않는다.
        self._local = threading.local()

    def recognize(self, image_bytes: bytes) -> str:
        prepared = prepare_image(image_bytes)
        try:
            if not hasattr(self._local, "engine"):
                import ddddocr
                engine = ddddocr.DdddOcr(show_ad=False)
                # 1.6.1에서는 정수 0이 빈 문자만 허용하므로 숫자 문자열을 명시한다.
                engine.set_ranges("0123456789")
                self._local.engine = engine
            answer = self._local.engine.classification(prepared)
        except ImportError as exc:
            raise CourtError("OCR 구성요소가 없습니다. setup.bat를 실행하거나 보안문자를 직접 입력하세요.") from exc
        except Exception as exc:
            raise CourtError("보안문자를 자동 인식하지 못했습니다. 직접 입력하거나 새로고침하세요.") from exc
        answer = re.sub(r"\s+", "", str(answer))
        if not re.fullmatch(r"\d{6}", answer):
            raise CourtError("보안문자 6자리를 인식하지 못했습니다. 직접 입력하거나 새로고침하세요.")
        return answer
