#!/usr/bin/env python3
"""Offline decision checks. Reads JSON, prints proposals, never calls an account API."""
import argparse
import json
import math
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit


def require(obj, *keys):
    for key in keys:
        if key not in obj or obj[key] is None or obj[key] == "":
            raise ValueError(f"missing {key}")


def number(obj, key, minimum=0):
    require(obj, key)
    value = obj[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
        raise ValueError(f"invalid {key}: expected finite number >= {minimum}")
    return value


def count(obj, key):
    value = number(obj, key)
    if int(value) != value:
        raise ValueError(f"invalid {key}: expected integer")
    return int(value)


def flag(obj, key):
    require(obj, key)
    if type(obj[key]) is not bool:
        raise ValueError(f"invalid {key}: expected boolean")
    return obj[key]


def records(obj, key):
    require(obj, key)
    if not isinstance(obj[key], list) or not obj[key] or not all(isinstance(x, dict) for x in obj[key]):
        raise ValueError(f"{key} must be a non-empty list of objects")
    return obj[key]


def unique(rows, key):
    seen = set()
    for row in rows:
        require(row, key)
        if not isinstance(row[key], str) or not row[key].strip() or row[key] in seen:
            raise ValueError(f"invalid or duplicate {key}")
        seen.add(row[key])


def date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def web_url(value):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise ValueError("invalid public URL")
    parsed = urlsplit(value)
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("invalid public URL")


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("invalid verification timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("verification timestamp must include timezone")


def ratio(top, bottom):
    return None if bottom == 0 else round(top / bottom, 6)


def paid(data):
    """Matched-population contribution, not causal attribution or predicted LTV."""
    require(data, "scope", "policy")
    scope, policy = data["scope"], data["policy"]
    require(scope, "account", "market", "currency", "start", "end", "revenue_basis", "attribution_window")
    if date(scope["start"]) > date(scope["end"]):
        raise ValueError("start must not follow end")
    require(policy, "min_orders", "max_spend", "loss_limit", "min_contribution_roi")
    min_orders = count(policy, "min_orders")
    if min_orders == 0:
        raise ValueError("min_orders must be positive; chosen by operator, not a statistical test")
    for key in ("max_spend", "loss_limit", "min_contribution_roi"):
        number(policy, key)
    rows = records(data, "rows")
    unique(rows, "entity_id")
    result = []
    for row in rows:
        require(row, "account", "market", "currency", "start", "end", "revenue_basis", "attribution_window")
        matched = all(row[k] == scope[k] for k in ("account", "market", "currency", "start", "end", "revenue_basis", "attribution_window"))
        checks = {k: flag(row, k) for k in ("population_matched", "costs_complete", "window_mature", "sellable")}
        v = {k: number(row, k) for k in ("gross_revenue", "refunds", "cogs", "fees", "fulfillment", "spend")}
        orders = count(row, "orders")
        contribution = v["gross_revenue"] - v["refunds"] - v["cogs"] - v["fees"] - v["fulfillment"] - v["spend"]
        net_revenue = v["gross_revenue"] - v["refunds"]
        roi = ratio(contribution, v["spend"])
        alerts = []
        if not checks["sellable"]:
            alerts.append("UNSELLABLE: independently review destination / inventory before further spend")
        if matched and v["spend"] > policy["max_spend"]:
            alerts.append("SPEND_LIMIT: known spending boundary exceeded even if costs need reconciliation")
        status, reason = "HOLD", "No paid spend or insufficient matured orders."
        if not matched or not checks["population_matched"] or not checks["costs_complete"]:
            status, reason = "RECONCILE", "Do not mix accounts, markets, periods, currencies, attribution windows, populations or missing costs. See independent risk alerts."
        elif not checks["sellable"]:
            status, reason = "REVIEW_STOP", "Product, destination or fulfillment is unavailable."
        elif v["spend"] > policy["max_spend"] or contribution < -policy["loss_limit"]:
            status, reason = "REVIEW_STOP", "Operator spending or observed-loss boundary breached; check revenue lag before attributing cause."
        elif checks["window_mature"] and orders >= min_orders and roi is not None:
            if roi >= policy["min_contribution_roi"]:
                status, reason = "REVIEW_SCALE", "Eligible for a bounded budget review, not evidence of causal lift or statistical significance."
            else:
                status, reason = "REVIEW_CHANGE", "Below operator return floor; diagnose traffic, promise, destination and repeat use before changing budget."
        result.append({"entity_id": row["entity_id"], "status": status, "reason": reason, "risk_alerts": alerts,
                       "contribution": round(contribution, 2) if matched and checks["costs_complete"] and checks["population_matched"] else None,
                       "contribution_roi": roi if matched and checks["costs_complete"] and checks["population_matched"] else None,
                       "net_revenue_roas": ratio(net_revenue, v["spend"]) if matched and checks["population_matched"] else None})
    return {"scope": scope, "rows": result, "warning": "ROI is contribution after ads / ad spend. Never substitute marketplace GMV or external estimates. No predicted lifetime value."}


def promotion_mix(data):
    """Scenario arithmetic by variant. No demand prediction or platform settlement parsing."""
    require(data, "market", "currency", "basis")
    if data["basis"] != "scenario" or count(data, "period_days") == 0:
        raise ValueError("use scenario basis and an equal, positive period_days for both plans")
    extra = {p: number(data, p + "_extra_fee") for p in ("before", "after")}
    rows = records(data, "rows")
    unique(rows, "sku")
    checked = []
    for row in rows:
        values = {p + "_orders": count(row, p + "_orders") for p in ("before", "after")}
        for p in ("before", "after"):
            values[p + "_unit_contribution"] = number(row, p + "_unit_contribution", -math.inf)
            values[p + "_contribution"] = values[p + "_orders"] * values[p + "_unit_contribution"]
        checked.append({"sku": row["sku"], **values})
    totals = {}
    for p in ("before", "after"):
        orders = sum(r[p + "_orders"] for r in checked)
        subtotal = sum(r[p + "_contribution"] for r in checked)
        totals[p] = {"orders": orders, "contribution": round(subtotal - extra[p], 2),
                     "unit_contribution_before_extra_fee": ratio(subtotal, orders)}
    before, after = totals["before"], totals["after"]
    average = sum(r["after_contribution"] for r in checked) / after["orders"] if after["orders"] else None
    needed = (before["contribution"] + extra["after"]) / average if average and average > 0 and before["contribution"] > 0 else None
    delta = round(after["contribution"] - before["contribution"], 2)
    loss_skus = [r["sku"] for r in checked if r["after_orders"] > 0 and r["after_unit_contribution"] < 0]
    return {"market": data["market"], "currency": data["currency"], "basis": "scenario", "period_days": data["period_days"],
            "before_extra_fee": extra["before"], "after_extra_fee": extra["after"], "rows": checked,
            **totals, "contribution_change": delta, "loss_variants": loss_skus,
            "orders_to_match_before_at_after_mix": round(needed, 2) if needed is not None else None,
            "status": "REVIEW_MIX_AND_OFFER" if loss_skus or delta < 0 else "REVIEW_FEASIBILITY",
            "warning": "Unit contribution uses reconciled seller receivable minus costs not already deducted. Extra fees are separate fixed scenario costs. Equal periods, same market/currency; not observed lift or a volume forecast. Break-even assumes unchanged mix, unit costs and extra fees; integer variant orders, inventory and capacity need a new check."}


def mercado_ads(data):
    """Offline campaign diagnosis from supplied mappings and verified business checks."""
    require(data, "scope", "policy", "metrics", "checks")
    scope, policy, metrics = data["scope"], data["policy"], data["metrics"]
    keys = ("site_id", "advertiser_id", "campaign_id")
    require(scope, *keys, "currency", "start", "end")
    if any(not isinstance(scope[k], str) or not scope[k].strip() for k in (*keys, "currency")):
        raise ValueError("scope identifiers and currency must be non-empty strings")
    if date(scope["start"]) > date(scope["end"]):
        raise ValueError("start must not follow end")
    threshold, cap = number(policy, "material_loss_pct"), number(policy, "max_extra_spend")
    if not 0 < threshold <= 100:
        raise ValueError("material_loss_pct must be > 0 and <= 100; it is an operator policy")
    require(metrics, *keys, "level", "unit")
    if metrics["level"] != "campaign" or metrics["unit"] != "percent":
        raise ValueError("loss metrics must use campaign level and explicit percent units")
    issues = ["metric scope mismatch"] if any(metrics[k] != scope[k] for k in keys) else []
    checks = {k: flag(data["checks"], k) for k in ("mapping_verified", "ownership_verified", "campaign_members_complete", "window_mature", "costs_complete", "contribution_ok", "sellable", "stock_ok", "budget_constrained")}
    groups = records(data, "groups")
    unique(groups, "ad_group_id")
    impacts, all_members, listed, unknown = [], [], [], []
    for group in groups:
        require(group, *keys, "ad_group_type", "ad_group_external_id")
        kind = group["ad_group_type"]
        if kind not in ("CATALOG", "FAMILY", "ITEM"):
            raise ValueError("unknown ad_group_type")
        if not isinstance(group["ad_group_external_id"], str) or not group["ad_group_external_id"].strip():
            raise ValueError("invalid ad_group_external_id")
        members = records(group, "members")
        unique(members, "item_id")
        link_key = {"CATALOG": "parent_id", "FAMILY": "family_id", "ITEM": "item_id"}[kind]
        if any(group[k] != scope[k] for k in keys):
            issues.append(group["ad_group_id"] + ": scope mismatch")
        if any(m.get(link_key) != group["ad_group_external_id"] for m in members) or (kind == "ITEM" and len(members) != 1):
            issues.append(group["ad_group_id"] + ": member grouping mismatch")
        for member in members:
            state = member.get("catalog_status")
            if state not in ("winning", "sharing_first_place", "competing", "listed", "non_catalog"):
                unknown.append(member["item_id"] + ": catalog status missing or unknown")
            if kind == "CATALOG" and state == "non_catalog":
                issues.append(member["item_id"] + ": catalog group contains non-catalog member")
            if state == "listed":
                listed.append({"item_id": member["item_id"], "reason": member.get("catalog_reason")})
                if not isinstance(member.get("catalog_reason"), str) or not member["catalog_reason"].strip():
                    unknown.append(member["item_id"] + ": missing listed reason")
            all_members.append(member)
        impacts.append({"ad_group_id": group["ad_group_id"], "ad_group_type": kind, "ad_group_external_id": group["ad_group_external_id"], "member_item_ids": [m["item_id"] for m in members]})
    unique(all_members, "item_id")
    losses = {k: None if metrics.get(k) is None else number(metrics, k) for k in ("budget_lost_pct", "rank_lost_pct")}
    unknown.extend(k + ": missing campaign metric" for k, v in losses.items() if v is None)
    if any(v is not None and v > 100 for v in losses.values()):
        raise ValueError("loss percentages must not exceed 100")
    if all(v is not None for v in losses.values()) and sum(losses.values()) > 100 + 1e-9:
        raise ValueError("campaign loss shares exceed 100%; reconcile the report denominator or rounding")
    status, reason = "LIMITED_BUDGET_REVIEW", "Review a bounded increase within the operator cap, never execute it."
    if issues:
        status, reason = "RECONCILE_SCOPE", "Resolve campaign ownership or group membership conflicts before any change."
    elif unknown:
        status, reason = "NEEDS_DATA", "Resolve unknown catalog states or missing campaign metrics; never replace them with zero."
    elif not all(checks[k] for k in ("mapping_verified", "ownership_verified", "campaign_members_complete")):
        status, reason = "NEEDS_DATA", "Verify ownership, current mapping and the complete campaign impact before proposing changes."
    elif listed:
        status, reason = "FIX_CATALOG_REASON", "Review the listed reasons before expanding spend; listed does not mean unavailable for purchase."
    elif not checks["sellable"] or not checks["stock_ok"]:
        status, reason = "REVIEW_OFFER_STOCK", "Resolve the verified saleability or stock constraint before additional paid demand."
    elif not checks["costs_complete"]:
        status, reason = "NEEDS_DATA", "Reconcile costs before judging available contribution headroom."
    elif not checks["contribution_ok"]:
        status, reason = "REVIEW_ECONOMICS", "The operator contribution condition fails; more exposure is not the immediate decision."
    elif not checks["window_mature"]:
        status, reason = "WAIT_FOR_WINDOW", "The supplied observation window is not mature; do not infer a budget opportunity."
    elif losses["rank_lost_pct"] >= threshold:
        status = "REVIEW_MIXED_LIMITS" if losses["budget_lost_pct"] >= threshold else "REVIEW_RANK"
        reason = "Review ranking, offer eligibility and the strategy/target controls actually available to this account before any budget proposal."
    elif losses["budget_lost_pct"] < threshold or cap == 0:
        status, reason = "HOLD", "Budget loss is below the operator materiality policy or the extra-spend cap is zero."
    elif not checks["budget_constrained"]:
        status, reason = "NEEDS_DATA", "Confirm actual budget/consumption constraints before a budget increase review."
    return {"scope": scope, "status": status, "reason": reason, "scope_issues": issues, "data_gaps": unknown,
            "failed_checks": [k for k, passed in checks.items() if not passed],
            "group_impacts": impacts, "campaign_impact_item_ids": [m["item_id"] for m in all_members],
            "campaign_members_complete": checks["campaign_members_complete"], "catalog_listed": listed,
            "losses": {"level": "campaign", "unit": "percent", **losses}, "operator_policy": policy,
            "review_extra_spend_cap": cap if status == "LIMITED_BUDGET_REVIEW" else None,
            "warning": "Campaign losses are not allocated to items. Supplied checks are attestations, not independently verified accounts or economics. Listed does not mean unpurchasable. No account calls, bids, fees or budget writes."}


def catalog(data):
    """Exact-target change proposals, not a publishing connector."""
    require(data, "platform", "market", "account", "intent")
    if data["intent"] not in ("update", "publish", "unpublish"):
        raise ValueError("intent must be update, publish or unpublish; deletion is deliberately unsupported")
    rows = records(data, "items")
    unique(rows, "sku")
    proposals = []
    for row in rows:
        require(row, "remote_id", "before", "desired", "source_refs")
        if not isinstance(row["before"], dict) or not isinstance(row["desired"], dict) or not isinstance(row["source_refs"], list):
            raise ValueError("before/desired must be objects; source_refs must be a list")
        blocked = []
        if not row["source_refs"]:
            blocked.append("missing approved product evidence")
        for key in ("variant_mapping_checked", "fulfillment_checked", "account_permission_checked"):
            if not flag(row, key):
                blocked.append(key)
        if data["intent"] == "unpublish" and not flag(row, "open_orders_checked"):
            blocked.append("review outstanding orders before unpublishing")
        diff = {k: {"before": row["before"].get(k), "after": v} for k, v in row["desired"].items() if row["before"].get(k) != v}
        proposals.append({"sku": row["sku"], "remote_id": row["remote_id"], "intent": data["intent"],
                          "status": "BLOCKED" if blocked else "APPROVAL_REQUIRED" if diff or data["intent"] != "update" else "NO_CHANGE",
                          "blockers": blocked, "patch_proposal": diff,
                          "readback": ["same account / market / remote ID", "submission and review status", "buyer-visible price, variants, availability and shipping"]})
    return {"platform": data["platform"], "market": data["market"], "account": data["account"], "items": proposals}


def cohorts(data):
    """Fixed follow-up windows; recent cohorts excluded instead of called churned."""
    require(data, "as_of", "activation_definition", "retention_definition")
    as_of = date(data["as_of"])
    horizon = count(data, "horizon_days")
    if horizon == 0:
        raise ValueError("horizon_days must be positive")
    rows = records(data, "cohorts")
    unique(rows, "cohort_id")
    output = []
    for row in rows:
        require(row, "cohort_end", "source", "unit")
        age = (as_of - date(row["cohort_end"])).days
        n, activated, retained, paid_count = [count(row, k) for k in ("eligible", "activated", "retained", "paid")]
        if max(activated, retained, paid_count) > n or age < 0:
            raise ValueError("cohort counts exceed eligible denominator or cohort is in the future")
        mature = age >= horizon
        output.append({"cohort_id": row["cohort_id"], "source": row["source"], "unit": row["unit"], "eligible": n,
                       "status": "MATURE" if mature else "WAIT_FOR_WINDOW",
                       "activation_rate": ratio(activated, n) if mature else None,
                       "retention_rate": ratio(retained, n) if mature else None,
                       "paid_rate": ratio(paid_count, n) if mature else None})
    return {"definitions": {k: data[k] for k in ("activation_definition", "retention_definition", "horizon_days")},
            "cohorts": output, "warning": "All three numerators use eligible as denominator; rows may overlap across outcomes. Do not sum users across sources or infer causation."}


def distribution(data):
    """Publication ledger completeness, separate from ranking/citation performance."""
    rows = records(data, "posts")
    unique(rows, "task_id")
    output = []
    for row in rows:
        require(row, "account_role", "content_version", "environment_ref", "canonical_url")
        web_url(row["canonical_url"])
        if row.get("post_url"):
            web_url(row["post_url"])
        if row.get("verified_at"):
            timestamp(row["verified_at"])
        authorized = flag(row, "authorized")
        submitted = flag(row, "submitted")
        readback = flag(row, "public_readback_matches")
        status = "BLOCKED" if not authorized else "DRAFT"
        if authorized and submitted:
            status = "VERIFY" if not readback or not row.get("post_url") or not row.get("verified_at") else "PUBLISHED_VERIFIED"
        output.append({"task_id": row["task_id"], "status": status, "post_url": row.get("post_url"),
                       "next": "read public body, media, identity and URL" if status == "VERIFY" else "track indexing, citation, referral and qualified action separately"})
    return {"posts": output, "warning": "Ledger validation only: supplied readback is not independently checked. No fingerprint evasion, fabricated endorsements or posting is performed. A verified post is not an AI citation."}


def geo_sample(data):
    """Count supplied answer labels within one planned question/condition group."""
    scope_keys = {"surface", "market", "language", "question_id", "question_version",
                  "conditions_id", "target_product_id", "target_source_id"}
    outcome_keys = ("mentioned", "recommended", "cited_target", "search_observed")
    statuses = ("valid", "technical_failure", "refused", "not_triggered")

    def text_field(obj, key):
        value = obj.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty string")
        return value

    scope = data.get("scope")
    if not isinstance(scope, dict) or set(scope) != scope_keys:
        raise ValueError("geo_sample scope must contain exactly the eight defined scope fields")
    for key in scope_keys:
        text_field(scope, key)
    planned = data.get("planned_run_ids")
    if not isinstance(planned, list) or not planned or any(not isinstance(x, str) or not x.strip() for x in planned):
        raise ValueError("planned_run_ids must be a non-empty list of non-empty strings")
    planned_set = set(planned)
    if len(planned_set) != len(planned):
        raise ValueError("duplicate planned run_id")
    rows = data.get("runs")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("runs must be a list of objects; use an empty list when none were observed")
    seen, signatures, duplicates = {}, {}, 0
    for row in rows:
        run_id = text_field(row, "run_id")
        if run_id not in planned_set:
            raise ValueError(f"unplanned run_id: {run_id}")
        if not isinstance(row.get("scope"), dict) or row["scope"] != scope:
            raise ValueError(f"scope mismatch for {run_id}")
        text_field(row, "raw_answer_ref")
        try:
            timestamp(row.get("captured_at"))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"invalid captured_at for {run_id}: expected timestamp with timezone") from exc
        status = text_field(row, "status")
        if status not in statuses:
            raise ValueError(f"unknown status for {run_id}")
        if status == "valid":
            for key in outcome_keys:
                flag(row, key)
            text_field(row, "evidence_ref")
            if row["recommended"] and not row["mentioned"]:
                raise ValueError(f"recommended requires mentioned for {run_id}")
        elif any(key in row for key in outcome_keys):
            raise ValueError(f"outcome flags are only allowed on valid runs: {run_id}")
        elif "evidence_ref" in row:
            text_field(row, "evidence_ref")
        signature = json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if run_id in seen:
            if signatures[run_id] != signature:
                raise ValueError(f"conflicting records for run_id: {run_id}")
            duplicates += 1
        else:
            seen[run_id], signatures[run_id] = row, signature
    ordered = [seen[run_id] for run_id in planned if run_id in seen]
    valid = [row for row in ordered if row["status"] == "valid"]
    searched = [row for row in valid if row["search_observed"]]
    status_counts = {status: sum(row["status"] == status for row in ordered) for status in statuses}
    status_counts["unobserved"] = len(planned) - len(ordered)
    metrics = {}
    for label, key in (("mentions", "mentioned"), ("recommendations", "recommended"), ("citations", "cited_target")):
        numerator = sum(row[key] for row in valid)
        metrics[label] = {"count": numerator, "denominator": len(valid), "rate": ratio(numerator, len(valid))}
    search_citations = sum(row["cited_target"] for row in searched)
    return {"scope": scope, "planned_run_ids": planned, "planned_count": len(planned),
            "observed_count": len(ordered), "valid_count": len(valid), "status_counts": status_counts,
            "observed_rate": ratio(len(ordered), len(planned)),
            "valid_completion_rate": ratio(len(valid), len(planned)), **metrics,
            "confirmed_search_valid": {"valid_count": len(searched), "citations": search_citations,
                                       "citation_rate": ratio(search_citations, len(searched))},
            "unobserved_run_ids": [run_id for run_id in planned if run_id not in seen],
            "duplicates_merged": duplicates, "runs": ordered,
            "warning": "Counts use supplied labels, not independently verified evidence. cited_target means an actual citation of the predefined target_source_id, not a related link. search_observed=false means search was not confirmed, not proof that no search occurred. Valid uncited answers remain in the denominator. Failures, refusals, not-triggered runs and unobserved plans are separate. No model querying, posting, pixel reading, URL/entity normalization or automatic attribution; no causal, ranking, weighting or population inference."}


def shots(data):
    """Editorial checklist assembly from human/multimodal review; not a vision model."""
    rows = records(data, "shots")
    unique(rows, "shot_id")
    output = []
    for row in rows:
        require(row, "product_ref", "character_ref", "take", "reviewer", "timecode")
        failed = [key for key in ("product_matches", "identity_matches", "motion_ok", "audio_ok", "rights_ok") if not flag(row, key)]
        output.append({"shot_id": row["shot_id"], "take": row["take"], "status": "REPAIR" if failed else "READY_FOR_EDIT",
                       "failed_checks": failed, "timecode": row["timecode"], "reviewer": row["reviewer"]})
    return {"shots": output, "warning": "Flags are supplied evidence, not computed image similarity. READY_FOR_EDIT is not final film approval."}


CHECKS = {"paid": paid, "promotion_mix": promotion_mix, "mercado_ads": mercado_ads, "catalog": catalog, "cohorts": cohorts, "distribution": distribution, "geo_sample": geo_sample, "shots": shots}


def review(data):
    require(data, "kind")
    if data["kind"] not in CHECKS:
        raise ValueError("unknown kind; choose " + ", ".join(CHECKS))
    return {"kind": data["kind"], "mode": "offline_review", "executes_external_actions": False, **CHECKS[data["kind"]](data)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--example", help="Select a named object from examples.json")
    args = parser.parse_args()
    try:
        data = json.loads(args.input.read_text(encoding="utf-8"))
        if args.example:
            data = data[args.example]
        print(json.dumps(review(data), ensure_ascii=False, indent=2, allow_nan=False))
    except (ValueError, TypeError, KeyError, OSError) as exc:
        parser.exit(2, f"Invalid input: {exc}\n")


if __name__ == "__main__":
    main()
