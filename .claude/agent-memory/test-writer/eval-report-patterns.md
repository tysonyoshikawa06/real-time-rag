---
name: eval-report-structural-guarantee
description: Structural isolation testing for report generators — ensuring tests never touch real output files through explicit path parameters
metadata:
  type: feedback
---

## Pattern: Structural isolation proof for filesystem output

For Step 20B (eval report generator) and similar features that write to filesystem artifacts, the spec demands a structural guarantee that tests cannot reach the real file, not just cleanup discipline.

### Implementation
- Write an autouse session-scoped fixture that:
  1. Captures `hashlib.sha256()` of the real file before tests run (if it exists)
  2. Yields (tests run)
  3. Re-hashes after all tests complete and asserts byte-identical (or file never existed)

This proves the structural fix holds: tests use explicit `output_path=tmp_path / "REPORT.md"` everywhere, and the real `eval/REPORT.md` is never touched.

### Why
Prior version of this test used autouse save/restore fixture and was explicitly rejected. A crash or Ctrl-C mid-test could strand fake numbers in the real file. The fix is passing explicit output paths, not backup/restore.

### Code template
```python
@pytest.fixture(scope="session", autouse=True)
def _real_report_isolation_proof(tmp_path_factory):
    real_file_path = Path("eval") / "REPORT.md"
    real_hash_before = None
    if real_file_path.exists():
        real_hash_before = hashlib.sha256(real_file_path.read_bytes()).hexdigest()

    yield  # Tests run

    if real_hash_before is not None:
        real_hash_after = hashlib.sha256(real_file_path.read_bytes()).hexdigest()
        assert real_hash_before == real_hash_after
    else:
        assert not real_file_path.exists()
```

## Pattern: Synthetic fixture JSON files

For tests that consume graded/run JSON files (Steps 19-20), use factory functions to build minimal but complete structures:

- `_make_graded_runs_entry()`: single graded_runs dict entry with optional citation/numeric/routing/assertion data
- `_make_run_file()`: full run_*.json with configurable run_metadata (test both presence and absence)
- `_make_graded_file()`: full graded_*.json linking to a specific run file
- Write these to `tmp_path` using `json.dumps()` and `Path.write_text()`

Don't use the real eval/results/ files as fixtures. Use synthetic ones for isolation.

### Key test cases for Step 20B specifically
- **With run_metadata**: Test that model, git_commit, timestamps, etc. appear in report
- **Without run_metadata** (graceful degradation): Test that ALL provenance fields show "not recorded", not guesses or missing sections
- **Partial run_metadata** (single null field): Test that missing field shows "not recorded", rest render normally
- **Zero citations**: Test "not measured (no citations issued)" not ZeroDivisionError
- **Null numeric_accuracy.extracted**: Test excluded from denominator, not counted as failure

## Pattern: Assertion index specificity

When a spec names a specific assertion index for a question (e.g., "novel_error assertion index 2"), create test graded_runs entry with multiple assertions and verify the right one is used, not a blend or the wrong index.

```python
# novel_error index 0 = semantic (LLM-judged), index 2 = mechanical (cites real transaction_id)
# Headline detection rate must use index 2, never index 0
```

## Datetime handling for test compatibility

- Use `datetime.now(datetime.UTC)` (not `timezone.utc`) for Python 3.13+ compatibility
- Call `.isoformat()` to serialize to string in JSON
- For ground_truth.sql fields, create minimal but complete structure with all required keys (p50, p95, p99, max, event_count, window)
