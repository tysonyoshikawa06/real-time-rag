"""Mechanical (deterministic) grading checks for Step 19B.

Pure functions, no network/DB access here — grade.py owns the one live DB
lookup (citation existence) and passes its result in. Every regex/threshold
below is taken verbatim from `.claude/specs/19b-eval-grader.spec.md`'s
Context section (the per-question checker table) and Behavior #1-9; nothing
here invents alternative check logic.
"""

import re

# --- Shared constants (also echoed into graded_<ts>.json's "config" block) ---

COUNT_TOLERANCE_PCT = 0.02  # 2% relative — counts scale with volume/time drift
RATE_TOLERANCE_ABS = 0.02  # 2 percentage points absolute — rates are already
# normalized, so an absolute tolerance is the natural unit (a relative
# tolerance would be absurdly tight near 0% and absurdly loose near 100%).
FRESHNESS_STALE_THRESHOLD_SECONDS = 300

METHODS = ("card", "ach", "wallet")
GATEWAYS = ("stripe-proxy", "adyen-gw", "braintree-edge", "checkout-io")

UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def extract_uuids(text: str | None) -> list[str]:
    """Every UUID-shaped token in `text`, in order of first appearance, de-duped."""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in UUID_RE.findall(text):
        low = match.lower()
        if low not in seen:
            seen.add(low)
            out.append(match)
    return out


def _tool_results_blob(tool_calls: list[dict]) -> str:
    return "\n".join(str(tc.get("result_text") or "") for tc in tool_calls)


def check_citations(
    answer_text: str | None,
    tool_calls: list[dict],
    db_existing_ids: set[str] | None,
    db_checked: bool,
) -> dict:
    """Classify every UUID cited in `answer_text` per Behavior #1-5.

    `db_existing_ids` is a set of lowercased transaction_id strings that
    exist in the DB (already batched-queried by the caller for this run's
    cited ids); ignored when `db_checked` is False.
    """
    cited_ids = extract_uuids(answer_text)
    blob_lower = _tool_results_blob(tool_calls).lower()
    db_existing_ids = db_existing_ids or set()

    valid_ids: list[str] = []
    fabricated_ids: list[str] = []
    ungrounded_but_real_ids: list[str] = []

    for cid in cited_ids:
        low = cid.lower()
        if low in blob_lower:
            valid_ids.append(cid)
        elif db_checked and low in db_existing_ids:
            ungrounded_but_real_ids.append(cid)
        else:
            fabricated_ids.append(cid)

    passed = not fabricated_ids and not ungrounded_but_real_ids
    return {
        "cited_ids": cited_ids,
        "valid_ids": valid_ids,
        "fabricated_ids": fabricated_ids,
        "ungrounded_but_real_ids": ungrounded_but_real_ids,
        "db_checked": db_checked,
        "passed": passed,
    }


def _extract_number_near(text: str, keyword: str, window: int = 40) -> int | None:
    pattern = re.compile(re.escape(keyword) + rf"[^\d]{{0,{window}}}?([\d,]+)", re.IGNORECASE)
    match = pattern.search(text)
    if not match:
        return None
    return int(match.group(1).replace(",", ""))


def _extract_percent_near(text: str, keyword: str, window: int = 60) -> float | None:
    pattern = re.compile(
        re.escape(keyword) + rf"[^\d%]{{0,{window}}}?([\d.]+)\s*%", re.IGNORECASE
    )
    match = pattern.search(text)
    if not match:
        return None
    return float(match.group(1)) / 100.0


def _gt_group_count(ground_truth_sql: dict, group_name: str) -> int | None:
    for key in ("by_method", "by_gateway"):
        for row in ground_truth_sql.get(key, {}).get("rows", []):
            if row.get("group") == group_name:
                return row.get("count")
    return None


def _gt_gateway_failure_rate(ground_truth_sql: dict, group_name: str) -> float | None:
    for row in ground_truth_sql.get("rows", []):
        if row.get("group") == group_name:
            return row.get("failure_rate")
    return None


def _within_count_tolerance(extracted: int, ground_truth: int) -> bool:
    if ground_truth == 0:
        return extracted == 0
    return abs(extracted - ground_truth) <= ground_truth * COUNT_TOLERANCE_PCT


def numeric_accuracy_aggregation(answer_text: str, ground_truth_sql: dict) -> dict:
    """Behavior #6 — per-category count extraction + tolerance for `aggregation`."""
    categories = []
    for category in (*METHODS, *GATEWAYS):
        extracted = _extract_number_near(answer_text, category)
        gt_value = _gt_group_count(ground_truth_sql, category)
        within = None
        if extracted is not None and gt_value is not None:
            within = _within_count_tolerance(extracted, gt_value)
        categories.append(
            {
                "category": category,
                "extracted": extracted,
                "ground_truth": gt_value,
                "within_tolerance": within,
            }
        )
    # Extraction failures are "couldn't check," not "checked and wrong."
    passed = all(c["within_tolerance"] is not False for c in categories)
    return {"attempted": True, "categories": categories, "passed": passed}


def numeric_accuracy_gateway_rate(answer_text: str, ground_truth_sql: dict) -> dict:
    """Behavior #7 — stripe-proxy failure-rate extraction + tolerance for `gateway_rate`."""
    extracted = _extract_percent_near(answer_text, "stripe-proxy")
    gt_value = _gt_gateway_failure_rate(ground_truth_sql, "stripe-proxy")
    within = None
    if extracted is not None and gt_value is not None:
        within = abs(extracted - gt_value) <= RATE_TOLERANCE_ABS
    passed = within is not False
    return {
        "attempted": True,
        "extracted": extracted,
        "ground_truth": gt_value,
        "within_tolerance": within,
        "passed": passed,
    }


def numeric_accuracy_not_attempted() -> dict:
    """Behavior #8 — the four question ids with no numeric-accuracy check defined."""
    return {"attempted": False}


NUMERIC_ACCURACY_FUNCS = {
    "aggregation": numeric_accuracy_aggregation,
    "gateway_rate": numeric_accuracy_gateway_rate,
}


def check_routing(tool_calls: list[dict], tools_expected: list[str]) -> dict:
    """Behavior #9 — OR semantics: at least one expected tool was used."""
    used = sorted({tc.get("name") for tc in tool_calls if tc.get("name")})
    passed = any(name in used for name in tools_expected)
    return {"expected": list(tools_expected), "used": used, "passed": passed}


# --- Per-question assertion checkers (Context section, positionally aligned
# with golden_questions.json's `assertions` list for each id). Every checker
# takes a single `ctx` dict with keys: answer_text, question_id,
# incident_context, ground_truth (the run's full "ground_truth" dict),
# citation (this run's check_citations() result), numeric_accuracy (this
# run's numeric-accuracy result), assertion_text (this assertion's exact
# text from golden_questions.json). Mechanical checkers return
# {"tier": "mechanical", "passed": bool | "unattempted", "detail": str}. ---


def _cited_matches_ground_truth(ctx: dict) -> set[str]:
    valid = {v.lower() for v in ctx["citation"]["valid_ids"]}
    gt_ids = {i.lower() for i in ctx["ground_truth"]["sql"].get("matching_transaction_ids", [])}
    return valid & gt_ids


# aggregation


def _agg_assert_1(ctx: dict) -> dict:
    match = re.search(r"5\s*-?\s*minutes?", ctx["answer_text"], re.IGNORECASE)
    return {
        "tier": "mechanical",
        "passed": bool(match),
        "detail": f"5-minute window phrase {'found' if match else 'not found'} in answer_text",
    }


def _agg_assert_2(ctx: dict) -> dict:
    text_lower = ctx["answer_text"].lower()
    methods_found = [m for m in METHODS if m in text_lower]
    gateways_found = [g for g in GATEWAYS if g in text_lower]
    passed = len(methods_found) >= 2 and len(gateways_found) >= 2
    return {
        "tier": "mechanical",
        "passed": passed,
        "detail": f"methods found: {methods_found}, gateways found: {gateways_found}",
    }


def _agg_assert_3(ctx: dict) -> dict:
    categories = ctx["numeric_accuracy"].get("categories", [])
    method_ok = any(c["extracted"] is not None for c in categories if c["category"] in METHODS)
    gateway_ok = any(c["extracted"] is not None for c in categories if c["category"] in GATEWAYS)
    passed = method_ok and gateway_ok
    return {
        "tier": "mechanical",
        "passed": passed,
        "detail": f"method number extracted: {method_ok}, gateway number extracted: {gateway_ok}",
    }


# gateway_rate


def _gw_assert_1(ctx: dict) -> dict:
    passed = "stripe-proxy" in ctx["answer_text"].lower()
    return {
        "tier": "mechanical",
        "passed": passed,
        "detail": f"'stripe-proxy' {'found' if passed else 'not found'} in answer_text",
    }


def _gw_assert_2(ctx: dict) -> dict:
    extracted = ctx["numeric_accuracy"].get("extracted")
    passed = extracted is not None
    return {
        "tier": "mechanical",
        "passed": passed,
        "detail": f"extracted stripe-proxy failure-rate value: {extracted}",
    }


def _gw_assert_3(ctx: dict) -> dict:
    text_lower = ctx["answer_text"].lower()
    others = [g for g in GATEWAYS if g != "stripe-proxy" and g in text_lower]
    passed = len(others) >= 1
    return {
        "tier": "mechanical",
        "passed": passed,
        "detail": f"other gateways mentioned: {others}",
    }


# fraud_pattern

_UNDER_5_RE = re.compile(r"(under\s*\$5|\$?5(\.00)?\b)", re.IGNORECASE)


def _fraud_assert_1(ctx: dict) -> dict:
    card_bin = (ctx["incident_context"] or {}).get("card_bin", "")
    passed = bool(card_bin) and card_bin in ctx["answer_text"]
    return {
        "tier": "mechanical",
        "passed": passed,
        "detail": f"card_bin {card_bin!r} {'found' if passed else 'not found'} in answer_text",
    }


def _fraud_assert_2(ctx: dict) -> dict:
    match = _UNDER_5_RE.search(ctx["answer_text"])
    return {
        "tier": "mechanical",
        "passed": bool(match),
        "detail": f"$5 amount pattern {'matched' if match else 'not found'} in answer_text",
    }


def _fraud_assert_3(ctx: dict) -> dict:
    overlap = _cited_matches_ground_truth(ctx)
    return {
        "tier": "mechanical",
        "passed": bool(overlap),
        "detail": f"cited valid ids overlapping ground-truth matches: {sorted(overlap)}",
    }


# novel_error


def _novel_assert_1(ctx: dict) -> dict:
    from eval.llm_judge import judge

    signature = ctx["ground_truth"]["sql"].get("signature", "")
    result = judge(
        answer_text=ctx["answer_text"],
        assertion_text=ctx["assertion_text"],
        context=signature,
    )
    return {"tier": "semantic", **result}


def _novel_assert_2(ctx: dict) -> dict:
    match = re.search(r"\d+\s*(times|occurrences|of\s*\d+)", ctx["answer_text"], re.IGNORECASE)
    return {
        "tier": "mechanical",
        "passed": bool(match),
        "detail": f"recurrence-count pattern {'matched' if match else 'not found'} in answer_text",
    }


def _novel_assert_3(ctx: dict) -> dict:
    overlap = _cited_matches_ground_truth(ctx)
    return {
        "tier": "mechanical",
        "passed": bool(overlap),
        "detail": f"cited valid ids overlapping ground-truth matches: {sorted(overlap)}",
    }


# freshness

# A freshness answer typically states the asked-about window ("over the last
# 5 minutes") *and* the actual lag figure ("median lag is 0.6s") in the same
# sentence - a bare "first number+unit in the text" match grabs the window
# phrase instead of the figure it's supposed to check. Anchor on the
# vocabulary system_freshness's own output uses (current/lag/p50/median)
# first, and only fall back to the naive any-number-any-unit match if none of
# those are present (an answer phrased unexpectedly still gets *a* check
# rather than none).
_FRESHNESS_UNIT_RE = (
    r"(\d+(?:\.\d+)?)\s*(ms|milliseconds?|s\b|sec|secs|seconds?|minutes?|mins?|min\b)"
)
_FRESHNESS_KEYWORD_RE = re.compile(
    r"(?:current\s+(?:as\s+of\s+)?~?|lag(?:\s+is)?\s*~?|p50[^\d]{0,10}|median[^\d]{0,15})"
    r"\s*" + _FRESHNESS_UNIT_RE,
    re.IGNORECASE,
)
_FRESHNESS_FALLBACK_RE = re.compile(_FRESHNESS_UNIT_RE, re.IGNORECASE)


def _extract_freshness_seconds(text: str) -> float | None:
    match = _FRESHNESS_KEYWORD_RE.search(text) or _FRESHNESS_FALLBACK_RE.search(text)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith("ms") or unit.startswith("milli"):
        return value / 1000.0
    if unit.startswith("min"):
        return value * 60.0
    return value


def _fresh_assert_1(ctx: dict) -> dict:
    seconds = _extract_freshness_seconds(ctx["answer_text"])
    passed = seconds is not None
    detail = (
        f"extracted recency figure: {seconds} seconds" if passed else "no recency figure found"
    )
    return {"tier": "mechanical", "passed": passed, "detail": detail}


def _fresh_assert_2(ctx: dict) -> dict:
    seconds = _extract_freshness_seconds(ctx["answer_text"])
    if seconds is None:
        return {
            "tier": "mechanical",
            "passed": "unattempted",
            "detail": "no recency figure extracted, cannot evaluate staleness",
        }
    passed = seconds < FRESHNESS_STALE_THRESHOLD_SECONDS
    return {
        "tier": "mechanical",
        "passed": passed,
        "detail": (
            f"extracted {seconds}s vs staleness threshold "
            f"{FRESHNESS_STALE_THRESHOLD_SECONDS}s"
        ),
    }


# hallucination_control

_CRYPTO_NUMBER_RE = re.compile(r"(\d+\s*crypto|crypto[^.]*\b[1-9]\d*\b)", re.IGNORECASE)
_HALLUC_PHRASES = (
    "not a valid",
    "not tracked",
    "not a payment method",
    "no such method",
    "isn't a",
    "is not a",
)


def _halluc_assert_1(ctx: dict) -> dict:
    match = _CRYPTO_NUMBER_RE.search(ctx["answer_text"])
    passed = not bool(match)
    detail = (
        "crypto tied to a nonzero number (fail)"
        if match
        else "no crypto+nonzero-number pattern found (pass)"
    )
    return {"tier": "mechanical", "passed": passed, "detail": detail}


def _halluc_assert_2(ctx: dict) -> dict:
    text_lower = ctx["answer_text"].lower()
    has_crypto = "crypto" in text_lower
    matched_phrase = next((p for p in _HALLUC_PHRASES if p in text_lower), None)
    passed = has_crypto and matched_phrase is not None
    return {
        "tier": "mechanical",
        "passed": passed,
        "detail": f"crypto mentioned: {has_crypto}, explanatory phrase: {matched_phrase!r}",
    }


ASSERTION_CHECKS: dict[str, list] = {
    "aggregation": [_agg_assert_1, _agg_assert_2, _agg_assert_3],
    "gateway_rate": [_gw_assert_1, _gw_assert_2, _gw_assert_3],
    "fraud_pattern": [_fraud_assert_1, _fraud_assert_2, _fraud_assert_3],
    "novel_error": [_novel_assert_1, _novel_assert_2, _novel_assert_3],
    "freshness": [_fresh_assert_1, _fresh_assert_2],
    "hallucination_control": [_halluc_assert_1, _halluc_assert_2],
}
