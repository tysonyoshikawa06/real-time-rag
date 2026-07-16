# Eval Metrics Report

Generated from `eval\results\graded_20260715_011631.json` (source run: `eval\results\run_20260715_004828.json`).

## Headline metrics

### Citation validity / hallucination rate

- n = 11 citation(s) across 6 run(s)
- Valid: 11/11
- Fabricated (want zero): 0/11
- Ungrounded but real: 0/11
- Hallucination rate: 0.0% ((fabricated + ungrounded_but_real) / total cited)

### Aggregation accuracy

Tolerance (from this graded file's own `config`): counts within 0.02 relative, rates within 0.02 absolute. Counts and rates are never blended into one number.

- **Counts** (n=7 extracted category value(s), `aggregation` question): 100.0% within tolerance, mean absolute error 3.0 (count units)
- **Rates** (n=1 extracted rate value(s), `gateway_rate` question): 0.0% within tolerance, mean absolute error 6.4% (percentage points)

### Incident detection rate

One mechanical assertion per incident type, never blended across types (state which assertion each row measures):

- **gateway_degradation** (n=1, via `gateway_rate` assertion #0 — 'names the gateway stripe-proxy as the outlier'): 1/1 passed (100%)
- **fraud_burst** (n=1, via `fraud_pattern` assertion #0 — 'names the card BIN 411111 as the shared pattern'): 1/1 passed (100%)
- **novel_error_pattern** (n=1, via `novel_error` assertion #2 — 'cites at least one real transaction_id matching the novel error'): 1/1 passed (100%)

### Tool routing accuracy

- Overall (n=6): 6/6 (100%)

| question_id | n | passed | rate |
|---|---|---|---|
| aggregation | 1 | 1 | 100% |
| gateway_rate | 1 | 1 | 100% |
| fraud_pattern | 1 | 1 | 100% |
| novel_error | 1 | 1 | 100% |
| freshness | 1 | 1 | 100% |
| hallucination_control | 1 | 1 | 100% |

### Negative control (`hallucination_control`)

- n = 1
- Overall mechanical pass: 1/1 (100%)
- `citation.fabricated_ids` empty for all runs: True

### Freshness

Read from the run file's captured `ground_truth.sql` for the `freshness` question — reflects the live stream at capture time, not a backlog drain. This report makes no live DB call of its own.

n = 1 repeat(s).

| repeat | window | event_count | p50 (s) | p95 (s) | p99 (s) | max (s) |
|---|---|---|---|---|---|---|
| 1 | 5 minutes | 5988 | 0.60 | 1.10 | 1.16 | 1.27 |

## Methodology

- Model: not recorded
- Run started: not recorded
- Run finished: not recorded
- Repeats per question: not recorded
- Golden set path: not recorded
- Golden set sha256: not recorded
- Git commit: not recorded
- Post-clear pause (s): not recorded
- Post-gate buffer (s): not recorded
- Ground-truth windows source: not recorded

_This run file predates the `run_metadata` patch (Step 20A), so every field above is "not recorded" — this is correct, not a bug._

Ground truth is derived independently of the agent's own tool calls: each golden question has a dedicated function in `eval/ground_truth_queries.py` that re-runs the equivalent query directly against Postgres, and incident questions additionally cross-check the scenario engine's own `ground_truth.incident_records` log — captured immediately after the agent answered, never re-derived from the agent's own tool output.

- `asked_at` → `ground_truth.captured_at` gap across 6 run(s): min 3.6s, mean 9.4s, max 18.4s (the delay between the agent's tool call and the grader's independent snapshot; see Tolerance rationale below).

## Measured vs judged

Every semantic-tier assertion below was judged by an LLM, not a mechanical check — LLM-judged, weaker evidence than the mechanical checks above. Never counted into any headline percentage.

- **novel_error** (repeat 1): describes an error pattern distinct in kind from ordinary decline/timeout/fraud reasons
  - passed: True
  - reason: The answer correctly identifies the exact FATAL currency_mismatch error string and explicitly distinguishes it as a config/currency-mapping fault distinct from ordinary decline/timeout/fraud reasons.

## Tolerance rationale

Numeric comparisons use a tolerance band, not exact equality, because the stream keeps advancing between the moment the agent makes its tool call and the moment the grader's independent SQL snapshot runs a few seconds later — on a live stream that gap alone can shift raw counts and rates measurably, especially mid-incident when a rate is actively ramping.

- Count tolerance: 0.02 relative
- Rate tolerance: 0.02 absolute (percentage points)
- Freshness staleness threshold: 300 seconds

## Failure analysis

2 failing run(s):

### gateway_rate (repeat 1)
- **numeric_accuracy**: expected 'extracted value(s) within tolerance of ground truth', actual {'attempted': True, 'extracted': 0.379, 'ground_truth': 0.4431, 'within_tolerance': False, 'passed': False}

### fraud_pattern (repeat 1)
- **assertion: cites at least one real transaction_id from the matching rows**: expected 'passed', actual 'cited valid ids overlapping ground-truth matches: []'

### Per-question pass rate

- aggregation: 1/1
- gateway_rate: 0/1
- fraud_pattern: 0/1
- novel_error: 1/1
- freshness: 1/1
- hallucination_control: 1/1

### Known limitations

- Sample size: 1 repeat(s) per question.
- Single-environment caveat: all runs come from one local docker-compose environment, not a fleet of independent trials.
- Stochastic-behavior caveat: the agent's tool-use path and phrasing can vary run to run for the same question, so a single failing repeat is not proof of a systemic bug (and a single passing repeat is not proof of robustness).
- Timing-drift mechanism: the agent's tool call and the grader's independent ground-truth snapshot happen seconds apart on a live stream, so any comparison against a moving target can drift outside tolerance even when the agent's underlying reasoning and tool use were both correct — most visible during actively-ramping incidents.

## Reproduction

```
make eval-run       # capture a fresh run (add --repeats N for more trials)
make eval-grade     # grade the latest run against independent ground truth
make eval-report    # regenerate this report from the latest graded file
```
