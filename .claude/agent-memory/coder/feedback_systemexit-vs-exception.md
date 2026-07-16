---
name: systemexit-vs-exception
description: Don't reuse this repo's SystemExit-for-missing-file house style inside a documented public Python API function meant to be called directly (not just via CLI main())
metadata:
  type: feedback
---

`eval/grade.py` and `eval/run_eval.py` both raise `SystemExit(...)` from small
helper functions (`_resolve_results_path`, `_select_questions`, etc.) for
missing-file / bad-arg errors, relying on Python's default top-level handling
of `SystemExit` to print a clean message with no traceback when run as a
script. This is a fine pattern for functions that only ever run inside a
`main()`/CLI call chain.

It breaks when the function is a **documented public Python API** that a spec
says tests call directly (e.g. `eval/report.py`'s `generate_report(graded_path,
output_path=None) -> Path`, called straight from `tests/test_eval_report.py`,
no CLI involved). `SystemExit` subclasses `BaseException`, not `Exception` —
`pytest.raises(Exception)` (the obvious, idiomatic assertion for "this call
errors out") never catches it, so the test fails even though the code's error
handling is otherwise correct and well-messaged.

**Why:** Caught during Step 20B (`eval/report.py`) — `generate_report` first
raised `SystemExit` for missing/unparseable graded or run files (mirroring
`grade.py`'s style) and ruff/manual smoke tests looked fine, but
`test_missing_graded_file` / `test_unparseable_graded_json` /
`test_missing_run_file_referenced_by_graded` all used
`pytest.raises(Exception)` and failed on that exact BaseException-vs-Exception
gap.

**How to apply:** When a spec explicitly calls out a function as "the real
entry point... this is what tests call" (i.e. a library function, not a CLI
helper), raise ordinary exceptions from it (`FileNotFoundError`, `ValueError`,
etc.) with a clear message naming the file(s). Reserve `SystemExit` for
`main()` itself (or CLI-only helpers it alone calls, like a
"no files found, pick a default" resolver) — catch the library's regular
exceptions there and re-raise as `SystemExit(str(exc))` so CLI usage still
gets a clean, traceback-free exit. See [[stale-test-triage]] for the broader
habit of re-reading the spec's exact wording before assuming a test is wrong.
