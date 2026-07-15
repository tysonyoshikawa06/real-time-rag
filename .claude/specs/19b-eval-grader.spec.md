# Step 19B — Eval grader

## Feature

`eval/grade.py` (`python -m eval.grade [results_file]`, `make eval-grade`)
reads a capture file written by 19A (`eval/results/run_<timestamp>.json`)
and scores every run **offline** — no agent calls, no re-derivation of the
numeric ground truth already captured at ask-time. Grading is split into
three explicitly separated tiers (mechanical / semantic / routing-as-part-
of-mechanical), written to `eval/results/graded_<timestamp>.json`, with a
console pass-rate summary per question. No aggregated metrics table here —
that is Step 20's job, which consumes this file's output.

## Context

- `eval/results/run_<timestamp>.json` (19A's output, already committed) —
  each entry in `runs[]` has `question_id`, `question_text`,
  `incident_context`, `answer_text`, `tool_calls` (flattened
  `[{name, arguments, result_text}]`), `transcript.messages` (full,
  serialized), and `ground_truth` (`incident_records` +
  `sql`, captured independently at ask-time — see `.claude/specs/19a-eval-
  runner.spec.md` for exactly what `ground_truth.sql` contains per
  question id).
- `demo/golden_questions.json` — the 6 questions' `id`, `assertions`
  (ordered lists, exact text below), `tools_expected`, `incident_context`.
  This step's assertion checkers are keyed by **question id + list index**
  into this file's `assertions` array (fixed, known text — read below),
  not by re-parsing the English at grade time. On startup, `grade.py` must
  assert that `len(ASSERTION_CHECKS[qid])` equals
  `len(golden_questions[qid]["assertions"])` for every id and raise a clear
  error if not — this catches silent drift if `golden_questions.json` is
  ever edited without updating the checkers.
- `consumer/db.py::connect()` — used for exactly one thing in this step:
  the citation hard-fabrication check (does a cited `transaction_id`
  exist at all in `transactions`). This is the one deliberate exception to
  "offline" — the capture step can't pre-enumerate every ID the agent
  might cite, so this check needs a live lookup. Every other check
  (numeric accuracy, assertions, routing, the softer "was this ID actually
  retrieved this run" grounding check) uses only what 19A already
  captured in the results file. If the DB is unreachable, this one
  sub-check degrades to `"db_checked": false` per run rather than
  crashing the whole grade pass.
- `anthropic` SDK (already a dependency, used in `agent/loop.py`) — reused
  for the one LLM-as-judge call this step makes, via a **new, separate**
  lightweight completion call (not `agent/loop.py`'s tool-use loop — no
  tools needed here, just one prompt in, one verdict out).

### The 6 questions' exact assertions (from `demo/golden_questions.json`), and how each is graded

For each, the checker to build. "Mechanical" checks are deterministic
Python; the one "semantic" checker is the LLM-judge call.

**aggregation** (no incident):
1. "states the time window examined (last 5 minutes)" — mechanical: regex
   for `5\s*-?\s*minutes?` (case-insensitive) in `answer_text`.
2. "names concrete payment methods and gateways rather than vague
   generalities" — mechanical: `answer_text` contains at least 2 of
   `{card, ach, wallet}` and at least 2 of `{stripe-proxy, adyen-gw,
   braintree-edge, checkout-io}` (case-insensitive substring).
3. "reports counts or proportions for methods and gateways, not just
   names" — mechanical: reuse the numeric-accuracy extraction (below) —
   passes if it successfully extracted a number for at least one method
   and at least one gateway.

**gateway_rate** (`gateway_degradation`, target `stripe-proxy`):
1. "names the gateway stripe-proxy as the outlier" — mechanical: substring
   `stripe-proxy` in `answer_text` (case-insensitive).
2. "quantifies stripe-proxy's failure rate as a percentage or ratio" —
   mechanical: the numeric-accuracy extraction (below) for this question
   found a value (not `None`).
3. "compares stripe-proxy's rate against the other gateways, not just its
   own history" — mechanical: `answer_text` mentions at least one of the
   other three gateway names (`adyen-gw`, `braintree-edge`,
   `checkout-io`).

**fraud_pattern** (`fraud_burst`, target `incident_context["card_bin"]`):
1. "names the card BIN 411111 as the shared pattern" — mechanical:
   substring `incident_context["card_bin"]` in `answer_text`.
2. "states that the matching transactions are all under $5" — mechanical:
   regex for a dollar amount pattern near "5" (e.g. `\$?5(\.00)?\b` or
   "under $5") in `answer_text`.
3. "cites at least one real transaction_id from the matching rows" —
   mechanical: intersection of the citation check's `valid_ids` (below)
   with `ground_truth.sql["matching_transaction_ids"]` is non-empty.

**novel_error** (`novel_error_pattern`, target `incident_context["merchant"]`):
1. "describes an error pattern distinct in kind from ordinary
   decline/timeout/fraud reasons" — **semantic (LLM-judge)**: this is the
   one assertion in the whole set mechanics can't reach (it requires
   judging semantic characterization, not string matching). Give the
   judge `answer_text`, this assertion's text, and
   `ground_truth.sql["signature"]` (the actual novel-error string) as
   context; ask for strict PASS/FAIL + one-sentence reason.
2. "states that the pattern recurs rather than being a single isolated
   failure" — mechanical: regex for a count pattern like `\d+\s*(times|
   occurrences|of\s*\d+)` in `answer_text`.
3. "cites at least one real transaction_id matching the novel error" —
   mechanical: intersection of citation check's `valid_ids` with
   `ground_truth.sql["matching_transaction_ids"]` is non-empty.

**freshness** (no incident):
1. "states a concrete recency figure (e.g. seconds/minutes since last
   event) rather than a vague claim" — mechanical: regex for a number
   followed by `s|sec|second|ms|minute` in `answer_text`.
2. "reflects data that is actually recent (not stale or missing)" —
   mechanical: the extracted figure from check 1, converted to seconds,
   is under a `FRESHNESS_STALE_THRESHOLD_SECONDS = 300` constant
   (generous — observed p99 lag in this project is under 30s; 300s only
   catches a genuinely broken/stale claim). If extraction fails, this
   assertion is `unattempted` (not a fail).

**hallucination_control** (no incident):
1. "reports no matching data rather than inventing a number" — mechanical:
   `answer_text` does NOT contain a pattern like `\d+\s*crypto` or
   `crypto[^.]*\b[1-9]\d*\b` (a nonzero number tied to "crypto") — a
   negative check.
2. "explains that crypto is not a valid payment method in this stream" —
   mechanical: `answer_text` contains "crypto" together with one of
   `{"not a valid", "not tracked", "not a payment method", "no such
   method", "isn't a", "is not a"}` (case-insensitive).

## Behavior

### Citation validity (mechanical, applies to every run regardless of question)

1. Extract every UUID-shaped token from `answer_text` via regex (standard
   UUID pattern, case-insensitive).
2. For each extracted id, two independent checks:
   - **Grounding** (fully offline): does this id appear as a
     `transaction_id` value anywhere in this run's own
     `tool_calls[*].result_text` (string containment is fine — the id is
     a UUID, false positives are not a practical concern)? This is the
     "was it actually retrieved this run" check — it's what "falls within
     the asked window" cashes out to for this project, since every tool
     call is itself window-scoped by the arguments the agent chose.
   - **Existence** (needs live DB, degrades gracefully — see Context):
     does a row with this `transaction_id` exist in `transactions` at
     all? A single batched query (`WHERE transaction_id = ANY(%(ids)s::uuid[])`,
     parameterized) covering all cited ids in the run, not one query per id.
3. Classify every cited id into exactly one bucket:
   - `fabricated_ids`: not grounded (never appeared in a tool result this
     run) AND does not exist in the DB (when DB-checked) — a hard,
     unambiguous failure of grounding.
   - `ungrounded_but_real_ids`: exists in the DB but never appeared in
     this run's own tool results — report separately, also treated as a
     failure (the agent cited something it didn't actually retrieve this
     turn, even if the id happens to be real).
   - `valid_ids`: grounded (appeared in a tool result this run) —
     regardless of whether the DB check ran (grounding alone is
     sufficient for validity; DB existence is a bonus corroboration when
     available, not required for `valid_ids`).
4. `citation_passed` = `fabricated_ids` is empty AND `ungrounded_but_real_ids`
   is empty (i.e., every cited id is grounded). A run that cites nothing
   has `citation_passed = True` trivially (no citations to fail).
5. Report `{cited_ids, valid_ids, fabricated_ids, ungrounded_but_real_ids,
   db_checked: bool, passed: bool}`.

### Numeric accuracy (mechanical; only where the spec above names it)

6. `aggregation`: for each of `{card, ach, wallet}` and each of
   `{stripe-proxy, adyen-gw, braintree-edge, checkout-io}`, regex for the
   category name followed within ~40 characters by a number (allow comma
   thousands separators, e.g. `card[^\d]{0,40}?([\d,]+)`), take the first
   match, parse to int. Compare against `ground_truth.sql["by_method"]`/
   `["by_gateway"]`'s matching row's `count`, within
   `COUNT_TOLERANCE_PCT = 0.02` (2% relative — see concept explanation
   below for why this exists at all). Report per-category
   `{category, extracted, ground_truth, within_tolerance}`; overall
   `numeric_passed` is `True` only if every successfully-extracted value
   is within tolerance (categories that couldn't be extracted are
   excluded from the pass determination, not counted as failures —
   extraction failure is a "couldn't check," not a "checked and wrong").
7. `gateway_rate`: regex for a percentage near "stripe-proxy" (e.g.
   `stripe-proxy[^\d%]{0,60}?([\d.]+)\s*%`), parse to a 0-1 fraction.
   Compare against `ground_truth.sql["rows"]`'s stripe-proxy row's
   `failure_rate`, within `RATE_TOLERANCE_ABS = 0.02` (2 **percentage
   points**, absolute — not relative; see concept explanation for why
   rates use an absolute tolerance while counts use relative).
8. `fraud_pattern`/`novel_error`/`freshness`/`hallucination_control`: no
   numeric-accuracy check defined (their correctness is covered by the
   assertion checks and citation check instead) — report
   `{"attempted": false}` for these ids, not a failure.

### Tool routing (mechanical)

9. `routing_passed` = at least one tool name in
   `golden_questions.json`'s `tools_expected` for this question id
   appears among the names in this run's `tool_calls` (**OR** semantics,
   not AND — e.g. `novel_error`'s question text itself says "via
   get_transactions and/or semantic_search," so using only one of the two
   is a legitimate pass, confirmed by 19A's own live verification run
   where the model answered correctly using only `get_transactions`).
   Report `{expected: [...], used: [...], passed: bool}`.

### Assertion checks (mechanical + the one semantic)

10. For each question id, run the ordered checkers from Context above,
    positionally aligned with `golden_questions.json`'s `assertions`
    list for that id. Mechanical checkers return `{"assertion": <exact
    text>, "tier": "mechanical", "passed": bool, "detail": str}`
    (`detail` explains what was found/not found — this is the "why" the
    Failure detail requirement needs). The one semantic checker
    (novel_error assertion 1) calls the LLM judge and returns
    `{"assertion": <exact text>, "tier": "semantic", "passed": bool,
    "reason": <judge's one-sentence reason>}`.
11. LLM judge (`eval/llm_judge.py`): one Anthropic call, no tools, a
    prompt containing the question's answer text, the assertion text
    being judged, and the relevant ground-truth context (the novel-error
    signature string). Ask for a strict verdict the code can reliably
    parse (e.g. instruct the model to respond with `PASS: <reason>` or
    `FAIL: <reason>` as the first line) — parse defensively; if the
    response doesn't match the expected shape, report
    `passed: None`/`"unparseable"` rather than crashing or silently
    defaulting to pass.

### Aggregation into overall pass/fail

12. `overall_mechanical_pass` for a run = `citation.passed` AND
    (`numeric_accuracy.passed` is `True` or the check wasn't attempted)
    AND every **mechanical-tier** assertion passed AND `routing.passed`.
    Semantic-tier assertion results are attached to the run's output but
    **excluded** from `overall_mechanical_pass` — reported alongside,
    never blended into it (see concept explanation).
13. `failure_reasons`: a flat list across all failed mechanical checks for
    this run, each `{check, expected, actual}` (or the equivalent fields
    each check already produces) — this is what makes a red result
    debuggable rather than just red.

### Output

14. Write `eval/results/graded_<UTC timestamp>.json`:
    ```
    {
      "source_run_file": "eval/results/run_<...>.json",
      "graded_at": "<iso>",
      "config": {"count_tolerance_pct": 0.02, "rate_tolerance_abs": 0.02,
                 "freshness_stale_threshold_seconds": 300},
      "graded_runs": [
        {
          "question_id": "...", "repeat_index": N,
          "citation": {...}, "numeric_accuracy": {...},
          "routing": {...},
          "assertions": [{"assertion": ..., "tier": ..., "passed": ..., "detail"/"reason": ...}, ...],
          "overall_mechanical_pass": bool,
          "failure_reasons": [...]
        }, ...
      ],
      "summary": {
        "per_question_pass_rate": {"aggregation": "3/3", ...},
        "semantic_note": "N semantic (LLM-judged) assertions across M runs — reported separately, never counted toward pass/fail above."
      }
    }
    ```
15. Console: print `per_question_pass_rate` plus, for any run with
    `overall_mechanical_pass == False`, its `failure_reasons`. No
    aggregated cross-question metrics table (counts/rates/hallucination
    rate rolled up) — that's Step 20.

### CLI

16. `python -m eval.grade [results_file]` — if `results_file` omitted,
    grade the most recently modified `eval/results/run_*.json`. `make
    eval-grade` invokes it with no arg (grades the latest capture) —
    matches this Makefile's existing no-arg-target style; pass an
    explicit path via `python -m eval.grade path/to/run_x.json` for a
    specific older capture.

## Edge cases & errors

- A run's `answer_text` cites zero transaction_ids: citation check trivially
  passes (nothing to be wrong about).
- DB unreachable for the existence sub-check: don't fail the whole grade
  pass — set `db_checked: false` on every run's citation result and rely
  on the grounding check (which is fully offline) for `fabricated_ids`/
  `valid_ids` classification. Note in console output that DB existence
  corroboration was skipped.
- `golden_questions.json`'s assertions for a question id don't match
  `ASSERTION_CHECKS`' expected count: raise a clear `RuntimeError` at
  startup (before grading anything) naming the mismatched id — don't
  grade against a stale/misaligned checker list silently.
- A results file with zero runs, or a `--questions`-filtered 19A capture
  missing some question ids: grade whatever's present; `summary.
  per_question_pass_rate` only lists ids that actually appear.
- LLM judge call fails (API error) or returns unparseable output: report
  that run's semantic assertion as `passed: None` with the raw response
  or error in `reason`, don't crash the whole grading pass.

## Out of scope

- The aggregated metrics report/markdown table (Step 20) — hallucination
  rate, overall accuracy, freshness rollup across the whole run. This
  step's job stops at per-run/per-question pass-fail-with-reasons.
- The README (Step 21), the demo video (Step 22).
- Modifying `eval/run_eval.py`, `demo/golden_questions.json`,
  `agent/loop.py`, or any MCP tool.
- Re-deriving ground truth (already captured by 19A) — this step only
  compares against it.

## Acceptance criteria

- [ ] `eval/grade.py` (+ any supporting modules, e.g.
      `eval/mechanical_checks.py`, `eval/llm_judge.py`) exists; `python -m
      eval.grade` and `make eval-grade` both work, defaulting to the
      latest capture file.
- [ ] Citation validity, numeric accuracy (where defined), all 16
      assertion checks (15 mechanical + 1 semantic), and tool routing are
      all implemented per the Context/Behavior mapping above.
- [ ] Output written to `eval/results/graded_<timestamp>.json` with the
      structure in Behavior #14; console prints per-question pass rates
      plus failure reasons for any failing run.
- [ ] Mechanical and semantic tiers are visibly separated in the output —
      semantic assertions never affect `overall_mechanical_pass`.
- [ ] `eval/results/graded_*.json` added to `.gitignore` alongside
      `eval/results/*.json`'s existing entry (or covered by the existing
      glob — confirm which).
- [ ] **Deliberate failure-catching verification**: hand-edit a captured
      run's `answer_text` in a copy of a results file to cite a fabricated
      transaction_id (a syntactically valid UUID not present in the DB or
      in that run's tool results), run the grader against it, and confirm
      `fabricated_ids` catches it and `overall_mechanical_pass` flips to
      `False` for that run with a clear reason.
- [ ] Explain-back (two concepts): (1) why mechanical checks are
      preferred over LLM-as-judge here; (2) what the numeric-accuracy
      tolerances are compensating for. Delivered to the user after
      verification.
- [ ] Update `PROJECT_STATE.md`: check off Step 19, move current
      status/next action to Step 20. Do not start Step 20.
