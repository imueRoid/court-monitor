import json
import unittest
from unittest.mock import Mock
import xml.etree.ElementTree as ET

from court_client import CaseNumber, CourtClient, CourtError, outcome_fields


class CaseTests(unittest.TestCase):
    def test_case_input_and_invalid_numbers(self):
        self.assertEqual(str(CaseNumber.parse("2024 고합 000123")), "2024고합123")
        for value in ("고합123", "2025고합0", "2025고합1<script>", "2025고합12345678"):
            with self.assertRaises(CourtError):
                CaseNumber.parse(value)

    def test_outcomes_keep_party_path_and_do_not_invent_order(self):
        fields = outcome_fields({"dlt_acs": [
            {"jdgmnOrdCtt": "징역 1년<br>집행유예 2년", "crmcsUltmtDvsNm": "판결선고"},
            {"jdgmnOrdCtt": "무죄", "btprtCrmcsUltmtDvsCd": "123"}],
            "unknownNewField": "preserved in raw response"})
        self.assertEqual(fields[0]["category"], "판결 주문 필드")
        self.assertIn("[0]", fields[0]["path"])
        self.assertEqual(fields[0]["value"], "징역 1년\n집행유예 2년")
        self.assertEqual(fields[1]["category"], "판결·결과 관련 필드")
        self.assertEqual(fields[-1]["category"], "결과 코드 (원문)")
        self.assertFalse(any(f["category"] == "판결 주문 필드" for f in outcome_fields({"crmcsUltmtDvsNm": "판결선고"})))

    def test_server_error_in_http_200_is_not_a_success(self):
        c = CourtClient()
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {"status": 400, "errors": {"errorMessage": "보안문자를 확인하세요."}}
        c._request = Mock(return_value=response)
        with self.assertRaisesRegex(CourtError, "보안문자"):
            c._post("/ssgo/test.on", {})

    def test_html_block_page_does_not_crash_json_parser(self):
        c = CourtClient()
        response = Mock(ok=False, status_code=403)
        response.json.side_effect = ValueError("not json")
        c._request = Mock(return_value=response)
        with self.assertRaisesRegex(CourtError, "JSON"):
            c._post("/ssgo/test.on", {})

    def test_requests_use_official_websquare_headers(self):
        c = CourtClient()
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {"status": 200, "data": {}}
        c._request = Mock(return_value=response)
        c._post("/ssgo/ssgo10g/selectHmpgCrmcsPbtrlCsGnrlCtt.on", {})
        headers = c._request.call_args.kwargs["headers"]
        self.assertEqual(headers["Accept"], "application/json")
        self.assertEqual(headers["Content-Type"], "application/json;charset=UTF-8")
        self.assertTrue(headers["submissionid"].endswith("ssgoTab1_body_sbm_search"))

    def test_only_official_same_origin_paths(self):
        c = CourtClient()
        for path in ("https://example.org", "//evil/ssgo/test", "/other"):
            with self.assertRaises(CourtError):
                c._request("GET", path)

    def test_schema_submission_preserves_required_envelope(self):
        c = CourtClient()
        root = ET.fromstring('''<html xmlns:w2="http://www.inswave.com/websquare" xmlns:xf="http://www.w3.org/2002/xforms">
          <w2:dataMap id="dma_search"><w2:key id="captchaAnswer"/><w2:key id="csYear"/></w2:dataMap>
          <xf:submission id="sbm_search" ref="data:json,dma_search" action="/ssgo/search.on"/>
        </html>''')
        c.schema = Mock(return_value=root)
        c._post = Mock(return_value={"data": {}})
        c._query_screen("/ssgo/ui/test.xml", {"captchaAnswer": "123456encrypted", "csYear": "2025"})
        c._post.assert_called_once_with("/ssgo/search.on", {"dma_search": {"captchaAnswer": "123456encrypted", "csYear": "2025"}})

    def test_progress_failure_keeps_general_response_and_hidden_fields(self):
        c = CourtClient()
        c.courts = {"테스트법원": "000000"}
        c.kinds = {"고합": "075"}
        c.tasks = {"고합": "HG"}
        c.routes = {"HG": {"general": "/ssgo/general", "progress": "/ssgo/progress"}}
        c._captcha_token = "opaque-token"
        general = {"data": {"dma_csBasCtt": {"csNo": "20250750000001", "encCsNo": "encrypted", "btprtSeq": "1"},
                            "hidden": {"jdgmnOrdCtt": "테스트용 주문", "unrecognized": "원문 보존"}}}
        c._query_screen = Mock(side_effect=[general, CourtError("진행 조회 실패")])
        c.schema = Mock(return_value=ET.fromstring("<html/>"))
        result = c.search("테스트법원", "2025고합1", "가상인", "123456")
        self.assertEqual(result["general"], general)
        self.assertEqual(result["outcomes"][0]["raw"], "테스트용 주문")
        self.assertTrue(result["warnings"])
        self.assertEqual(c._captcha_token, "")
        params = c._query_screen.call_args_list[1].args[1]
        self.assertEqual(params["encCsNo"], "encrypted")
        self.assertEqual(params["captchaAnswer"], "")
        self.assertEqual(result["general"]["data"]["hidden"]["unrecognized"], "원문 보존")


if __name__ == "__main__":
    unittest.main()
