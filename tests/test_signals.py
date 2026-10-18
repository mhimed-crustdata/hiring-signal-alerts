import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import signals  # noqa: E402
from crustdata_api import Crustdata  # noqa: E402


def job(cid, name, posted, title="GTM Engineer", domain=None, industries=None, rng="51-200", url=None):
    return {"crustdata_job_id": hash((cid, posted, title)),
            "job_details": {"title": title, "url": url or f"https://jobs.example/{cid}/{posted}"},
            "company": {"basic_info": {"crustdata_company_id": cid, "name": name, "primary_domain": domain,
                                       "industries": industries or ["Software Development"]},
                        "headcount": {"total": 120, "range": rng}},
            "location": {"raw": "New York, NY"},
            "metadata": {"date_added": posted + "T10:00:00"}}


def owner(pid, cid, title):
    return {"crustdata_person_id": pid, "basic_profile": {"name": f"Owner {pid}"},
            "social_handles": {"professional_network_identifier": {"profile_url": f"https://example.com/in/{pid}"}},
            "experience": {"employment_details": {"current": [{"crustdata_company_id": cid, "title": title}]}}}


JOBS = [
    job(1, "Acme", "2026-10-10", domain="acme.io"),
    job(1, "Acme", "2026-10-12", title="GTM Systems Engineer", domain=None),
    job(2, "Beta", "2026-10-01", domain="beta.com", rng="11-50"),
    job(3, "Jobgether", "2026-10-11", industries=["Technology, Information and Internet"]),
    job(4, "TalentCo", "2026-10-11", industries=["Staffing and Recruiting"]),
]


class GroupTests(unittest.TestCase):
    def test_groups_dedupes_and_drops_resellers(self):
        out = signals.group_accounts(JOBS)
        names = [a["company"] for a in out["accounts"]]
        self.assertEqual(names, ["Acme", "Beta"])
        self.assertEqual(sorted(out["dropped"]), ["Jobgether", "TalentCo"])
        acme = out["accounts"][0]
        self.assertEqual(acme["posts"], 2)
        self.assertEqual(acme["latest_post"], "2026-10-12")
        self.assertEqual(acme["domain"], "acme.io")
        self.assertEqual(acme["titles"], ["GTM Engineer", "GTM Systems Engineer"])

    def test_job_filters(self):
        f = signals.job_filters(["GTM Engineer"], "2026-09-18", "United States", ["11-50"])
        fields = [c["field"] for c in f["conditions"]]
        self.assertEqual(fields, ["job_details.title", "metadata.date_added", "location.country", "company.headcount.range"])
        self.assertEqual(len(signals.job_filters(["x"], "2026-01-01", None, [])["conditions"]), 2)


class OwnerTests(unittest.TestCase):
    def test_attach_owners_filters_titles_and_caps(self):
        accounts = signals.group_accounts(JOBS)["accounts"]
        people = [owner(10, 1, "VP, Revenue Operations"), owner(11, 1, "Account Executive - RevOps Services"),
                  owner(12, 1, "Software Engineer"), owner(13, 2, "Head of Growth"), owner(10, 1, "VP, Revenue Operations")]
        calls = []

        def transport(url, body, headers, timeout):
            calls.append((url, body))
            return {"profiles": people, "next_cursor": None}

        signals.attach_owners(Crustdata(api_key="test", transport=transport), accounts,
                              ["Revenue Operations", "Head of Growth"])
        by_name = {a["company"]: a for a in accounts}
        self.assertEqual([o["person_id"] for o in by_name["Acme"]["owners"]], [10])
        self.assertEqual([o["title"] for o in by_name["Beta"]["owners"]], ["Head of Growth"])
        self.assertTrue(calls[0][0].endswith("/person/search"))
        self.assertIn("experience.employment_details.current.company_id", json.dumps(calls[0][1]["filters"]))


class OutputTests(unittest.TestCase):
    def test_summary_slack_state(self):
        result = {"posts": len(JOBS), **signals.group_accounts(JOBS)}
        s = signals.summary(result)
        self.assertEqual(s["accounts"], 2)
        self.assertEqual(s["dropped_resellers"], 2)
        self.assertIn("2 companies posted", signals.slack_text(result, ["GTM Engineer"], "2026-09-18"))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "seen.json")
            signals.save_state(result["accounts"][:1], path)
            self.assertEqual([a["company"] for a in signals.only_new(result["accounts"], path)], ["Beta"])
            signals.write_outputs(result, d)
            self.assertTrue(os.path.exists(os.path.join(d, "accounts.csv")))


if __name__ == "__main__":
    unittest.main()
