from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from server.config import Settings
from server.domain import snapshot
from server.store import Store


def fixture(number, party, task, **data):
    return {"case_type": task, "query": {"case_number": number, "party": party},
            "general": {"data": data}}


class CaseIdentityTests(unittest.TestCase):
    def test_official_civil_roles_take_precedence_over_representative_labels(self):
        data = {"dma_csBasCtt": {"rprsClmntNm": "가상 원고"},
                "dlt_btprtCttLst": [{"btprtNm": "가상원고", "btprtDvsNm": "원고(반소피고)"},
                                   {"btprtNm": "가상피고", "btprtDvsNm": "피고"}]}
        for person, role in (("가상 원고", "원고(반소피고)"), ("가상피고", "피고")):
            observed = snapshot(fixture("2024가단1", person, "CV", **data))
            self.assertEqual((observed["case_category"], observed["party_role"]), ("민사", role))

    def test_ambiguous_masked_admin_names_leave_role_unknown(self):
        data = {"dlt_btprtCttLst": [{"btprtNm": "김○○", "btprtDvsNm": "원고"},
                                  {"btprtNm": "김○○", "btprtDvsNm": "피고"}]}
        observed = snapshot(fixture("2024구합1", "김가상", "HS", **data))
        self.assertEqual((observed["case_category"], observed["party_role"]), ("행정", ""))

    def test_criminal_role_is_tied_to_the_selected_person(self):
        data = {"dma_csBasCtt": {"btprtSeq": "2", "btprtNm": "가○○", "acsdDvsNm": "피의자"},
                "dlt_acsCttLst": [{"btprtSeq": "1", "btprtNmOrg": "김가상"},
                                  {"btprtSeq": "2", "btprtNmOrg": "가나름"}]}
        cases = (("가나름", "피의자"), ("김가상", "피고인"), ("없는이름", ""))
        for person, role in cases:
            observed = snapshot(fixture("2024고합1", person, "HG", **data))
            self.assertEqual((observed["case_category"], observed["party_role"]), ("형사", role))

    def test_legacy_rows_are_classified_and_updated_roles_are_searchable(self):
        with TemporaryDirectory() as folder:
            settings = Settings(database=Path(folder) / "cases.sqlite3")
            store = Store(settings.database)
            key = store.add_case("가상법원", "2024구단1", "김가상", now=1)
            old = store.case_page("행정")["cases"][0]
            self.assertEqual((old["case_category"], old["party_role"]), ("행정", ""))
            observed = snapshot(fixture("2024구단1", "김가상", "HS",
                dlt_btprtCttLst=[{"btprtNm": "김○○", "btprtDvsNm": "원고"}]))
            lease = store.claim_case(2)["lease_token"]
            self.assertTrue(store.complete(key, lease, observed, settings, 3))
            self.assertEqual(Store(settings.database).case_page("행정 원고")["cases"][0]["party_role"], "원고")
