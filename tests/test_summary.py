import unittest

from case_summary import summarize_case


def result(basic, **data):
    return {"query": {"case_number": "2025고합1"},
            "general": {"data": {"dma_csBasCtt": basic, **data}}, "progress": []}


class SummaryTests(unittest.TestCase):
    def test_civil_terminal_result_is_used_when_order_is_missing(self):
        for terminal in ("원고승", "원고패", "원고일부승"):
            response = result({"csUltmtDvsNm": terminal, "csUltmtYmd": "20261002"})
            response["query"]["case_number"] = "2024가단1"
            summary = summarize_case(response)
            self.assertEqual(summary["판결 주문"], terminal)
            self.assertEqual(summary["선고일자"], "2026.10.02")

    def test_civil_explicit_date_and_progress_result(self):
        response = result({"csUltmtDvsNm": "원고승", "jdgmnYmd": "20261002", "btprtUltmtThrstCtt": "<br>"})
        response["query"]["case_number"] = "2024가합1"
        self.assertEqual(summarize_case(response)["판결 주문"], "원고승")
        response["general"]["data"]["dma_csBasCtt"]["btprtUltmtThrstCtt"] = "명시된 주문"
        self.assertEqual(summarize_case(response)["판결 주문"], "명시된 주문")

    def test_civil_result_without_conclusion_date_stays_unknown(self):
        response = result({"csUltmtDvsNm": "원고승"})
        response["query"]["case_number"] = "2024가단1"
        self.assertEqual(summarize_case(response)["판결 주문"], "확인되지 않음")

    def test_known_order_field_and_official_appeal_mapping(self):
        response = result({"btprtUltmtThrstCtt": "징역  1년 집행유예  2년<br><br>추징  1,234,000원 가납명령<br>보호관찰",
                           "btprtUltmtYmd": "20261002", "apelCrmcsLwstRltnrDvsCd": "02",
                           "acsApelPrpndYmd": "20261006", "hcrtCsNo": "20260790000123"},
                          dlt_inscrtDtsLst=[{"csNo": "20260790000123", "userCsNo": "2026노123", "cortNm": "서울고등법원"}])
        summary = summarize_case(response, {"노": "079"})
        self.assertIn("추징  1,234,000원", summary["판결 주문"])
        self.assertNotIn("<br>", summary["판결 주문"])
        self.assertEqual(summary["선고일자"], "2026.10.02")
        self.assertEqual(summary["상소제기 여부"], "항소 제기됨 (피고인 · 2026.10.06)")
        self.assertEqual(summary["상소사건번호"], "서울고등법원 2026노123")

    def test_future_hearing_and_lower_court_are_not_judgment_or_appeal(self):
        response = result({"btprtUltmtYmd": "20261002"},
                          dlt_inscrtDtsLst=[{"userCsNo": "2024고단10", "cortNm": "다른법원"}])
        response["progress"] = [{"data": {"dlt_csProgCtt": [{"progYmd": "20991231", "progCtt": "선고기일 예정"}]}}]
        summary = summarize_case(response)
        self.assertEqual(summary["선고일자"], "확인되지 않음")
        self.assertEqual(summary["상소사건번호"], "확인되지 않음")
        self.assertEqual(summary["상소제기 여부"], "제기 내역 확인되지 않음")

    def test_current_defendant_order_does_not_mix_other_defendants(self):
        response = result({"btprtUltmtThrstCtt": "무죄"},
                          dlt_acsCttLst=[{"btprtUltmtThrstCtt": "징역 5년"}])
        self.assertEqual(summarize_case(response)["판결 주문"], "무죄")

    def test_appeal_withdrawal_keeps_filing_and_disposition(self):
        summary = summarize_case(result({"apelCrmcsLwstRltnrDvsCd": "01", "aplPrpndRsltNm": "항소취하"}))
        self.assertEqual(summary["상소제기 여부"], "항소 제기됨 (검사) / 항소취하")

    def test_immediate_higher_stage_only(self):
        response = result({}, dlt_inscrtDtsLst=[
            {"userCsNo": "2026도3", "cortNm": "대법원"},
            {"userCsNo": "2026노2", "cortNm": "서울고등법원"}])
        self.assertEqual(summarize_case(response)["상소사건번호"], "서울고등법원 2026노2")


if __name__ == "__main__":
    unittest.main()
