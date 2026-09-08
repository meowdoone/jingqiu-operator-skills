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

    def test_mercado_budget_review_keeps_whole_campaign_impact(self):
        result = module.review(self.data("mercado_ads"))
        self.assertEqual(result["status"], "LIMITED_BUDGET_REVIEW")
        self.assertEqual(result["campaign_impact_item_ids"], ["DEMO-A", "DEMO-B1", "DEMO-B2", "DEMO-C"])
        self.assertEqual(result["group_impacts"][1]["member_item_ids"], ["DEMO-B1", "DEMO-B2"])
        self.assertEqual(result["review_extra_spend_cap"], 25)
        self.assertEqual(result["losses"]["level"], "campaign")
        self.assertNotIn("losses", result["group_impacts"][0])

    def test_mercado_scope_or_grouping_conflict_blocks_budget(self):
        for target, key, value in (("metrics", "site_id", "MLB"), ("group", "advertiser_id", "OTHER"), ("group", "campaign_id", "OTHER"), ("member", "parent_id", "WRONG")):
            data = self.data("mercado_ads")
            obj = data["metrics"] if target == "metrics" else data["groups"][0] if target == "group" else data["groups"][0]["members"][0]
            obj[key] = value
            result = module.review(data)
            self.assertEqual(result["status"], "RECONCILE_SCOPE")
            self.assertIsNone(result["review_extra_spend_cap"])

    def test_mercado_missing_loss_is_unknown_not_zero(self):
        data = self.data("mercado_ads")
        data["metrics"]["budget_lost_pct"] = None
        result = module.review(data)
        self.assertEqual(result["status"], "NEEDS_DATA")
        self.assertIsNone(result["losses"]["budget_lost_pct"])
        self.assertIsNone(result["review_extra_spend_cap"])

    def test_mercado_listed_is_catalog_reason_not_unpurchasable(self):
        data = self.data("mercado_ads")
        data["groups"][0]["members"][0].update(catalog_status="listed", catalog_reason="winner_has_better_reputation")
        result = module.review(data)
        self.assertEqual(result["status"], "FIX_CATALOG_REASON")
        self.assertEqual(result["catalog_listed"], [{"item_id": "DEMO-A", "reason": "winner_has_better_reputation"}])
        self.assertIsNone(result["review_extra_spend_cap"])

    def test_mercado_business_and_scope_checks_gate_budget_review(self):
        cases = {"mapping_verified": "NEEDS_DATA", "ownership_verified": "NEEDS_DATA", "campaign_members_complete": "NEEDS_DATA", "costs_complete": "NEEDS_DATA", "window_mature": "WAIT_FOR_WINDOW", "sellable": "REVIEW_OFFER_STOCK", "stock_ok": "REVIEW_OFFER_STOCK", "contribution_ok": "REVIEW_ECONOMICS", "budget_constrained": "NEEDS_DATA"}
        for key, expected in cases.items():
            with self.subTest(key=key):
                data = self.data("mercado_ads")
                data["checks"][key] = False
                result = module.review(data)
                self.assertEqual(result["status"], expected)
                self.assertIsNone(result["review_extra_spend_cap"])

    def test_mercado_material_losses_are_not_just_compared_with_each_other(self):
        cases = [(2, 1, True, 25, "HOLD"), (5, 40, False, 25, "REVIEW_RANK"),
                 (35, 40, True, 25, "REVIEW_MIXED_LIMITS"), (35, 10, True, 0, "HOLD"),
                 (20, 0, True, 25, "LIMITED_BUDGET_REVIEW")]
        for budget, rank, constrained, cap, expected in cases:
            with self.subTest(budget=budget, rank=rank, cap=cap):
                data = self.data("mercado_ads")
                data["metrics"].update(budget_lost_pct=budget, rank_lost_pct=rank)
                data["checks"]["budget_constrained"] = constrained
                data["policy"]["max_extra_spend"] = cap
                result = module.review(data)
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["review_extra_spend_cap"], cap if expected == "LIMITED_BUDGET_REVIEW" else None)

    def test_mercado_operator_threshold_is_explicit_and_configurable(self):
        data = self.data("mercado_ads")
        data["policy"]["material_loss_pct"] = 40
        self.assertEqual(module.review(data)["status"], "HOLD")

    def test_mercado_inconsistent_shares_are_not_a_budget_signal(self):
        data = self.data("mercado_ads")
        data["metrics"].update(budget_lost_pct=70, rank_lost_pct=60)
        with self.assertRaises(ValueError): module.review(data)

    def test_mercado_invalid_metrics_policy_and_flags(self):
        cases = [("metrics", "budget_lost_pct", v) for v in (-1, 101, True, float("nan"), float("inf"))]
        cases += [("policy", "material_loss_pct", 0), ("policy", "material_loss_pct", 101),
                  ("policy", "max_extra_spend", -1), ("metrics", "unit", "fraction"),
                  ("metrics", "level", "item"), ("checks", "stock_ok", "true")]
        for section, key, value in cases:
            with self.subTest(section=section, key=key, value=value):
                data = self.data("mercado_ads")
                data[section][key] = value
                with self.assertRaises(ValueError): module.review(data)

    def test_mercado_missing_state_reason_or_metric_blocks_budget(self):
        for case in ("status", "reason", "rank"):
            data = self.data("mercado_ads")
            member = data["groups"][0]["members"][0]
            if case == "status": member.pop("catalog_status")
            elif case == "reason": member["catalog_status"] = "listed"
            else: data["metrics"].pop("rank_lost_pct")
            result = module.review(data)
            self.assertEqual(result["status"], "NEEDS_DATA")
            self.assertIsNone(result["review_extra_spend_cap"])

    def test_mercado_duplicate_members_or_groups_are_rejected(self):
        for case in ("group", "member"):
            data = self.data("mercado_ads")
            if case == "group": data["groups"].append(copy.deepcopy(data["groups"][0]))
            else: data["groups"][1]["members"][0]["item_id"] = "DEMO-A"
            with self.assertRaises(ValueError): module.review(data)

    def test_mercado_family_item_and_non_catalog_mapping_conflicts(self):
        for case in ("family", "item_count", "non_catalog"):
            data = self.data("mercado_ads")
            if case == "family": data["groups"][1]["members"][0]["family_id"] = "WRONG"
            elif case == "item_count": data["groups"][2]["members"].append({"item_id": "DEMO-D", "catalog_status": "non_catalog"})
            else: data["groups"][0]["members"][0]["catalog_status"] = "non_catalog"
            result = module.review(data)
            self.assertEqual(result["status"], "RECONCILE_SCOPE")
            self.assertIsNone(result["review_extra_spend_cap"])

    def test_mercado_whitespace_ids_cannot_define_impact(self):
        for case in ("group", "item"):
            data = self.data("mercado_ads")
            if case == "group": data["groups"][0]["ad_group_id"] = "  "
            else: data["groups"][1]["members"][1]["item_id"] = "  "
            with self.assertRaises(ValueError): module.review(data)

    def test_mercado_missing_metric_does_not_hide_known_stock_constraint(self):
        data = self.data("mercado_ads")
        data["metrics"]["budget_lost_pct"] = None
        data["checks"]["stock_ok"] = False
        result = module.review(data)
        self.assertEqual(result["status"], "NEEDS_DATA")
        self.assertEqual(result["failed_checks"], ["stock_ok"])
        self.assertIsNone(result["review_extra_spend_cap"])

    def test_missing_costs_block_return(self):
        data = self.data("paid")
        data["rows"][0]["costs_complete"] = False
        row = module.review(data)["rows"][0]
        self.assertEqual(row["status"], "RECONCILE")
        self.assertIsNone(row["contribution_roi"])

    def test_promotion_more_orders_less_contribution(self):
        result = module.review(self.data("promotion_mix"))
        self.assertEqual(result["before"], {"orders": 100, "contribution": 480, "unit_contribution_before_extra_fee": 4.8})
        self.assertEqual(result["after"], {"orders": 150, "contribution": 200, "unit_contribution_before_extra_fee": 1.6})
        self.assertEqual(result["contribution_change"], -280)
        self.assertEqual(result["loss_variants"], ["DEMO-B"])
        self.assertEqual(result["orders_to_match_before_at_after_mix"], 325)
        self.assertEqual(result["status"], "REVIEW_MIX_AND_OFFER")

    def test_promotion_zero_orders_or_negative_average_no_volume_target(self):
        for field, value in (("after_orders", 0), ("after_unit_contribution", -1), ("after_unit_contribution", 0)):
            data = self.data("promotion_mix")
            for row in data["rows"]: row[field] = value
            result = module.review(data)
            self.assertIsNone(result["orders_to_match_before_at_after_mix"])

    def test_promotion_output_keeps_period_and_cost_assumptions(self):
        data = self.data("promotion_mix")
        data.update(period_days=30, before_extra_fee=10, after_extra_fee=50)
        result = module.review(data)
        for key in ("period_days", "before_extra_fee", "after_extra_fee"):
            self.assertEqual(result[key], data[key])

    def test_promotion_invalid_counts_or_money(self):
        for field, value in (("after_orders", -1), ("after_orders", 0.5), ("after_orders", True), ("after_unit_contribution", float("nan")), ("after_unit_contribution", float("inf"))):
            data = self.data("promotion_mix")
            data["rows"][0][field] = value
            with self.assertRaises(ValueError): module.review(data)

    def test_promotion_duplicate_variants(self):
        data = self.data("promotion_mix")
        data["rows"][1]["sku"] = data["rows"][0]["sku"]
        with self.assertRaises(ValueError): module.review(data)

    def test_promotion_requires_scope_and_scenario(self):
        for field, value in (("basis", "observed_lift"), ("period_days", 0), ("market", ""), ("currency", ""), ("after_extra_fee", -1)):
            data = self.data("promotion_mix")
            data[field] = value
            with self.assertRaises(ValueError): module.review(data)

    def test_promotion_fixed_fee_and_nonloss_case(self):
        data = self.data("promotion_mix")
        data["rows"][1]["after_unit_contribution"] = 4
        result = module.review(data)
        self.assertEqual(result["after"]["contribution"], 560)
        self.assertEqual(result["orders_to_match_before_at_after_mix"], 130)
        self.assertEqual(result["status"], "REVIEW_FEASIBILITY")

    def test_promotion_no_positive_baseline_no_break_even_claim(self):
        data = self.data("promotion_mix")
        data["before_extra_fee"] = 500
        self.assertIsNone(module.review(data)["orders_to_match_before_at_after_mix"])

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

    def test_geo_sample_demo_uses_valid_not_planned_denominators(self):
        result = module.review(self.data("geo_sample"))
        self.assertEqual((result["planned_count"], result["observed_count"], result["valid_count"]), (6, 5, 4))
        self.assertEqual(result["status_counts"], {"valid": 4, "technical_failure": 1, "refused": 0, "not_triggered": 0, "unobserved": 1})
        self.assertEqual((result["observed_rate"], result["valid_completion_rate"]), (0.833333, 0.666667))
        self.assertEqual(result["mentions"], {"count": 3, "denominator": 4, "rate": 0.75})
        self.assertEqual(result["recommendations"], {"count": 2, "denominator": 4, "rate": 0.5})
        self.assertEqual(result["citations"], {"count": 1, "denominator": 4, "rate": 0.25})
        self.assertEqual(result["confirmed_search_valid"], {"valid_count": 2, "citations": 1, "citation_rate": 0.5})
        self.assertEqual(result["unobserved_run_ids"], ["DEMO-06"])

    def test_geo_identical_imports_merge_and_keep_original_refs(self):
        data = self.data("geo_sample")
        data["runs"][-1] = dict(reversed(list(data["runs"][-1].items())))
        result = module.review(data)
        self.assertEqual(result["duplicates_merged"], 1)
        self.assertEqual(result["runs"], data["runs"][:5])
        self.assertEqual(result["scope"], data["scope"])
        self.assertEqual(result["planned_run_ids"], data["planned_run_ids"])

    def test_geo_conflicting_same_run_id_rejected(self):
        for key, value in (("cited_target", False), ("raw_answer_ref", "OTHER"), ("captured_at", "2026-09-08T09:01:00Z")):
            with self.subTest(key=key):
                data = self.data("geo_sample")
                data["runs"][-1][key] = value
                with self.assertRaisesRegex(ValueError, "conflicting records"):
                    module.review(data)

    def test_geo_separate_runs_with_identical_answer_are_kept(self):
        data = self.data("geo_sample")
        row = copy.deepcopy(data["runs"][0])
        row["run_id"] = "DEMO-06"
        data["runs"].append(row)
        result = module.review(data)
        self.assertEqual((result["observed_count"], result["valid_count"], result["duplicates_merged"]), (6, 5, 1))
        self.assertEqual(result["citations"], {"count": 2, "denominator": 5, "rate": 0.4})

    def test_geo_invalid_plans_and_unplanned_runs_rejected(self):
        for value in ([], "DEMO-01", ["DEMO-01", "DEMO-01"], [" "], [1], [None]):
            with self.subTest(value=value):
                data = self.data("geo_sample")
                data["planned_run_ids"] = value
                with self.assertRaises(ValueError): module.review(data)
        data = self.data("geo_sample")
        data["runs"][0]["run_id"] = "NOT-PLANNED"
        with self.assertRaisesRegex(ValueError, "unplanned run_id"): module.review(data)

    def test_geo_scope_must_be_complete_and_exact(self):
        for key in self.data("geo_sample")["scope"]:
            for action in ("missing", "different"):
                with self.subTest(key=key, action=action):
                    data = self.data("geo_sample")
                    if action == "missing": data["runs"][0]["scope"].pop(key)
                    else: data["runs"][0]["scope"][key] = "OTHER"
                    with self.assertRaisesRegex(ValueError, "scope mismatch"): module.review(data)
        for value in (None, [], {}, {"surface": "demo"}):
            data = self.data("geo_sample")
            data["scope"] = value
            with self.assertRaises(ValueError): module.review(data)
        data = self.data("geo_sample")
        data["scope"]["market"] = " "
        with self.assertRaises(ValueError): module.review(data)

    def test_geo_empty_and_nonvalid_samples_have_null_outcome_rates(self):
        for statuses in ([], ["technical_failure", "refused", "not_triggered"]):
            data = self.data("geo_sample")
            base = data["runs"][4]
            data["runs"] = [{**copy.deepcopy(base), "run_id": data["planned_run_ids"][i], "status": status} for i, status in enumerate(statuses)]
            result = module.review(data)
            self.assertEqual((result["valid_count"], result["observed_count"]), (0, len(statuses)))
            self.assertEqual(result["status_counts"]["unobserved"], 6 - len(statuses))
            for status in statuses: self.assertEqual(result["status_counts"][status], 1)
            for key in ("mentions", "recommendations", "citations"):
                self.assertEqual(result[key], {"count": 0, "denominator": 0, "rate": None})
            self.assertIsNone(result["confirmed_search_valid"]["citation_rate"])

    def test_geo_zero_confirmed_search_does_not_remove_valid_answers(self):
        data = self.data("geo_sample")
        for row in data["runs"]:
            if row["status"] == "valid": row["search_observed"] = False
        result = module.review(data)
        self.assertEqual(result["citations"], {"count": 1, "denominator": 4, "rate": 0.25})
        self.assertEqual(result["confirmed_search_valid"], {"valid_count": 0, "citations": 0, "citation_rate": None})

    def test_geo_valid_flags_are_required_strict_booleans(self):
        for key in ("mentioned", "recommended", "cited_target", "search_observed"):
            for value in (1, 0, "true", "false", None, [], "MISSING"):
                with self.subTest(key=key, value=value):
                    data = self.data("geo_sample")
                    if value == "MISSING": data["runs"][0].pop(key)
                    else: data["runs"][0][key] = value
                    with self.assertRaises(ValueError): module.review(data)
        data = self.data("geo_sample")
        data["runs"][0].update(mentioned=False, recommended=True)
        with self.assertRaisesRegex(ValueError, "recommended requires mentioned"): module.review(data)

    def test_geo_evidence_and_timezone_required(self):
        cases = [(key, value) for key in ("run_id", "raw_answer_ref", "evidence_ref") for value in (None, " ", 123)]
        cases += [("captured_at", value) for value in (None, "DEMO-date", "2026-09-08", "2026-09-08T09:00:00")]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                data = self.data("geo_sample")
                data["runs"][0][key] = value
                with self.assertRaises(ValueError): module.review(data)

    def test_geo_invalid_statuses_or_rows_are_rejected(self):
        for value in ("success", "unobserved", "", 1):
            data = self.data("geo_sample")
            data["runs"][4]["status"] = value
            with self.assertRaises(ValueError): module.review(data)
        for value in (None, {}, [None], ["DEMO-01"]):
            data = self.data("geo_sample")
            data["runs"] = value
            with self.assertRaises(ValueError): module.review(data)
        data = self.data("geo_sample")
        data["runs"][4]["mentioned"] = False
        with self.assertRaisesRegex(ValueError, "only allowed on valid"): module.review(data)

    def test_shot_review_returns_local_fault(self):
        row = module.review(self.data("shots"))["shots"][0]
        self.assertEqual(row["failed_checks"], ["motion_ok"])
        self.assertEqual(row["status"], "REPAIR")


if __name__ == "__main__":
    unittest.main()
