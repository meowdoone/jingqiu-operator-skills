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
        if not isinstance(row[key], str) or row[key] in seen:
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


CHECKS = {"paid": paid, "catalog": catalog, "cohorts": cohorts, "distribution": distribution, "shots": shots}


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
