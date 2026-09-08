import copy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("review", ROOT / "scripts/review.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
EXAMPLES = json.loads((ROOT / "examples.json").read_text())


class ReviewTests(unittest.TestCase):
    def data(self, kind):
        return copy.deepcopy(EXAMPLES[kind])

    def test_every_example_is_offline(self):
        for data in EXAMPLES.values():
            self.assertFalse(module.review(data)["executes_external_actions"])

    def test_contribution_and_return(self):
        row = module.review(self.data("paid"))["rows"][0]
        self.assertEqual((row["contribution"], row["contribution_roi"], row["status"]), (1000, 2, "REVIEW_SCALE"))

    def test_missing_costs_block_return(self):
        data = self.data("paid")
        data["rows"][0]["costs_complete"] = False
        row = module.review(data)["rows"][0]
        self.assertEqual(row["status"], "RECONCILE")
        self.assertIsNone(row["contribution_roi"])

    def test_mismatched_currency_or_window(self):
        for key, value in (("account", "OTHER"), ("market", "MX"), ("currency", "CNY"), ("attribution_window", "1d_view"), ("end", "2026-08-08")):
            data = self.data("paid")
            data["rows"][0][key] = value
            self.assertEqual(module.review(data)["rows"][0]["status"], "RECONCILE")

    def test_missing_population_no_roi(self):
        data = self.data("paid")
        data["rows"][0]["population_matched"] = False
        self.assertIsNone(module.review(data)["rows"][0]["contribution"])

    def test_maturing_and_low_sample_do_not_scale(self):
        for key, value in (("window_mature", False), ("orders", 1), ("spend", 0)):
            data = self.data("paid")
            data["rows"][0][key] = value
            self.assertEqual(module.review(data)["rows"][0]["status"], "HOLD")

    def test_budget_boundary(self):
        data = self.data("paid")
        data["policy"]["max_spend"] = 400
        self.assertEqual(module.review(data)["rows"][0]["status"], "REVIEW_STOP")

    def test_missing_costs_do_not_hide_risk_alerts(self):
        data = self.data("paid")
        data["rows"][0].update(costs_complete=False, sellable=False, spend=1500)
        row = module.review(data)["rows"][0]
        self.assertEqual(row["status"], "RECONCILE")
        self.assertEqual(len(row["risk_alerts"]), 2)
        self.assertIsNone(row["contribution_roi"])

    def test_nonfinite_and_negative_inputs(self):
        for value in (float("nan"), float("inf"), -1, True, "500"):
            data = self.data("paid")
            data["rows"][0]["spend"] = value
            with self.assertRaises(ValueError): module.review(data)

    def test_bool_is_not_string(self):
        data = self.data("paid")
        data["rows"][0]["costs_complete"] = "false"
        with self.assertRaises(ValueError): module.review(data)

    def test_duplicates(self):
        data = self.data("catalog")
        data["items"] *= 2
        with self.assertRaises(ValueError): module.review(data)

    def test_catalog_patch_only_changed_fields(self):
        row = module.review(self.data("catalog"))["items"][0]
        self.assertEqual(list(row["patch_proposal"]), ["title"])
        self.assertEqual(row["status"], "APPROVAL_REQUIRED")

    def test_unpublish_open_orders(self):
        data = self.data("catalog")
        data["intent"] = "unpublish"
        data["items"][0]["open_orders_checked"] = False
        self.assertEqual(module.review(data)["items"][0]["status"], "BLOCKED")

    def test_delete_not_supported(self):
        data = self.data("catalog")
        data["intent"] = "delete"
        with self.assertRaises(ValueError): module.review(data)

    def test_cohort_maturity(self):
        rows = module.review(self.data("cohorts"))["cohorts"]
        self.assertEqual(rows[0]["retention_rate"], 0.2)
        self.assertEqual(rows[1]["status"], "WAIT_FOR_WINDOW")
        self.assertIsNone(rows[1]["retention_rate"])

    def test_bad_denominator(self):
        data = self.data("cohorts")
        data["cohorts"][0]["retained"] = 101
        with self.assertRaises(ValueError): module.review(data)

    def test_submitted_not_published(self):
        self.assertEqual(module.review(self.data("distribution"))["posts"][0]["status"], "VERIFY")

    def test_verified_post_requires_url_and_time(self):
        data = self.data("distribution")
        data["posts"][0]["public_readback_matches"] = True
        self.assertEqual(module.review(data)["posts"][0]["status"], "VERIFY")

    def test_invalid_public_evidence(self):
        for updates in ({"post_url": "not a url"}, {"verified_at": "not a date"}, {"verified_at": "2026-09-08T12:00:00"}):
            data = self.data("distribution")
            data["posts"][0].update(updates)
            with self.assertRaises(ValueError): module.review(data)

    def test_valid_ledger_does_not_call_network(self):
        data = self.data("distribution")
        data["posts"][0].update(public_readback_matches=True, post_url="https://example.com/post/1", verified_at="2026-09-08T12:00:00+08:00")
        self.assertEqual(module.review(data)["posts"][0]["status"], "PUBLISHED_VERIFIED")

    def test_shot_review_returns_local_fault(self):
        row = module.review(self.data("shots"))["shots"][0]
        self.assertEqual(row["failed_checks"], ["motion_ok"])
        self.assertEqual(row["status"], "REPAIR")


if __name__ == "__main__":
    unittest.main()
