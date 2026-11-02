import io
import json
import os
import sys
import unittest
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import job_pay_ranges as jpr  # noqa: E402


def load(name):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as f:
        return json.load(f)


def by_id(rows):
    return {r["job_id"]: r for r in rows}


class GreenhouseTests(unittest.TestCase):
    def setUp(self):
        self.rows = by_id(jpr.parse_greenhouse("example", load("greenhouse.json")))

    def test_structured_range_in_cents_with_period_from_title(self):
        r = self.rows["101"]
        self.assertEqual((r["pay_source"], r["pay_min"], r["pay_max"], r["currency"], r["interval"]),
                         ("ats", 58000, 68000, "CAD", "year"))
        self.assertEqual((r["annual_min"], r["annual_max"]), (58000, 68000))

    def test_period_from_double_escaped_blurb_and_range_count(self):
        r = self.rows["102"]
        self.assertEqual((r["interval"], r["range_count"], r["pay_min"]), ("year", 2, 100000))

    def test_description_fallback_reads_every_written_range(self):
        r = self.rows["103"]
        self.assertEqual(r["pay_source"], "description")
        self.assertEqual((r["pay_min"], r["pay_max"], r["currency"], r["interval"]), (120000, 150000, "USD", "year"))
        self.assertEqual(r["range_count"], 2)
        self.assertIn("€60.000 - €75.000", r["pay_text"])

    def test_no_range_anywhere(self):
        r = self.rows["104"]
        self.assertEqual((r["pay_source"], r["pay_min"], r["range_count"]), ("none", None, 0))


class LeverTests(unittest.TestCase):
    def setUp(self):
        self.rows = by_id(jpr.parse_lever("example", load("lever.json")))

    def test_hourly_structured_range_is_annualized_at_2080_hours(self):
        r = self.rows["aaa-1"]
        self.assertEqual((r["pay_source"], r["interval"], r["annual_min"], r["annual_max"]), ("ats", "hour", 83200, 131040))

    def test_yearly_structured_range(self):
        r = self.rows["aaa-2"]
        self.assertEqual((r["interval"], r["annual_min"], r["annual_max"]), ("year", 123000, 168000))

    def test_description_fallback_hourly(self):
        r = self.rows["aaa-3"]
        self.assertEqual((r["pay_source"], r["pay_min"], r["pay_max"], r["interval"]), ("description", 25, 32, "hour"))

    def test_none(self):
        self.assertEqual(self.rows["aaa-4"]["pay_source"], "none")


class AshbyTests(unittest.TestCase):
    def setUp(self):
        self.rows = by_id(jpr.parse_ashby("example", load("ashby.json")))

    def test_salary_component_only_not_equity(self):
        r = self.rows["b-1"]
        self.assertEqual((r["pay_source"], r["pay_min"], r["pay_max"], r["currency"], r["range_count"]),
                         ("ats", 211400, 290600, "USD", 1))

    def test_commission_ignored_and_tiers_counted(self):
        r = self.rows["b-2"]
        self.assertEqual((r["pay_min"], r["range_count"]), (90000, 2))
        self.assertNotIn("Commission", r["pay_text"] or "")

    def test_hidden_compensation_falls_back_to_description_with_k(self):
        r = self.rows["b-3"]
        self.assertEqual((r["pay_source"], r["pay_min"], r["pay_max"], r["interval"]), ("description", 150000, 180000, "year"))

    def test_empty_tiers_is_none(self):
        self.assertEqual(self.rows["b-4"]["pay_source"], "none")

    def test_unlisted_jobs_are_skipped(self):
        data = {"jobs": [{"id": "u-1", "title": "Listed", "isListed": True},
                         {"id": "u-2", "title": "Unlisted", "isListed": False},
                         {"id": "u-3", "title": "No flag"}]}
        self.assertEqual(sorted(by_id(jpr.parse_ashby("example", data))), ["u-1", "u-3"])


class TextTests(unittest.TestCase):
    def test_k_on_upper_bound_applies_to_both(self):
        r = jpr.ranges_from_text("Pay: $120-150K per year")[0]
        self.assertEqual((r["min"], r["max"], r["interval"]), (120000, 150000, "year"))

    def test_currency_code_after_range(self):
        r = jpr.ranges_from_text("£45,000 - £55,000 GBP annually")[0]
        self.assertEqual((r["min"], r["max"], r["currency"], r["interval"]), (45000, 55000, "GBP", "year"))

    def test_code_after_range_without_symbol(self):
        r = jpr.ranges_from_text("For Denver based hires: Estimated annual salary of 140,000 - 190,000 USD.")
        self.assertEqual([(x["min"], x["max"], x["currency"], x["interval"]) for x in r], [(140000, 190000, "USD", "year")])

    def test_code_before_both_numbers(self):
        r = jpr.ranges_from_text("Estimated annual salary is between SEK 760,000 - SEK 1,240,000.")[0]
        self.assertEqual((r["min"], r["max"], r["currency"], r["interval"]), (760000, 1240000, "SEK", "year"))

    def test_symbol_range_is_not_counted_twice(self):
        self.assertEqual(len(jpr.ranges_from_text("$140,000 - $190,000 USD per year")), 1)

    def test_reversed_range_ignored_and_plain_number_ignored(self):
        self.assertEqual(jpr.ranges_from_text("$150,000 - $120,000. We have 40 - 50 people."), [])

    def test_big_number_without_period_or_salary_word_stays_unknown(self):
        self.assertIsNone(jpr.ranges_from_text("Budget: $100,000 - $200,000.")[0]["interval"])

    def test_annualize_multipliers(self):
        self.assertEqual([jpr.annualize(10, p) for p in ("hour", "day", "week", "month", "year")],
                         [20800, 2600, 520, 120, 10])
        self.assertIsNone(jpr.annualize(10, None))


class SummaryTests(unittest.TestCase):
    def test_coverage_counts_and_median(self):
        rows = jpr.parse_lever("example", load("lever.json"))
        s = jpr.summarize(rows)["lever:example"]
        self.assertEqual((s["jobs"], s["ats"], s["description"], s["none"]), (4, 2, 1, 1))
        # midpoints: 107120 (hourly 40-63), 145500, 59280 (hourly 25-32) -> median 107120
        self.assertEqual(s["median_annual_midpoint"], {"USD": 107120})


class HttpTests(unittest.TestCase):
    def test_urls(self):
        self.assertEqual(jpr.board_url("greenhouse", "example"),
                         "https://boards-api.greenhouse.io/v1/boards/example/jobs?content=true&pay_transparency=true")
        self.assertEqual(jpr.board_url("lever", "example"), "https://api.lever.co/v0/postings/example?mode=json")
        self.assertEqual(jpr.board_url("ashby", "example"),
                         "https://api.ashbyhq.com/posting-api/job-board/example?includeCompensation=true")
        with self.assertRaises(ValueError):
            jpr.board_url("workday", "example")

    def test_retries_429_then_succeeds_and_sends_user_agent(self):
        calls, sleeps = [], []

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout):
            calls.append(req.get_header("User-agent"))
            if len(calls) == 1:
                raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {"Retry-After": "5"}, None)
            return Resp(b'{"jobs": []}')

        self.assertEqual(jpr.fetch_json("https://example.com", opener=opener, sleep=sleeps.append), {"jobs": []})
        self.assertEqual(sleeps, [5])
        self.assertTrue(calls[0].startswith("job-pay-ranges/"))

    def test_404_is_not_retried(self):
        def opener(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

        with self.assertRaises(urllib.error.HTTPError):
            jpr.fetch_json("https://example.com", opener=opener, sleep=lambda s: self.fail("slept"))


if __name__ == "__main__":
    unittest.main()
