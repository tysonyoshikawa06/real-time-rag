---
name: spec-format
description: The format for "Claude-readable specs" — the structured spec the orchestrator writes and that the coder and test-writer both build from. Use whenever writing, reading, or updating a spec file under .claude/specs/, even if not explicitly asked. Preloaded by the coder and test-writer agents.
---

# Claude-readable spec format

A spec is the single source of truth for **one feature**. The orchestrator
writes it to `.claude/specs/<feature>.spec.md` after locking requirements with
the user. The coder implements from it; the test-writer derives tests from it
*independently*. If the spec is vague, code and tests drift — so specs must be
concrete and testable.

## Required sections

### Feature
One sentence: what this feature is. The feature is the unit of work and maps
1:1 to a single test file.

### Context
Where this lives in the system — modules, endpoints, tables. Populate this from
the graphify graph, not by grepping or reading files: run `graphify query`,
`graphify path`, or `graphify explain` and cite the resulting node
IDs/file:line refs. Only fall back to direct Read/Grep for the one piece the
graph can't answer, and pull just that piece — don't dump whole files in.

### Behavior
Numbered, testable statements of what the feature must do. Each should be
checkable by a test. Prefer "given X, when Y, then Z" form.

### Inputs / Outputs
Exact shapes: request/response models, parameters, return types, status codes.

### Edge cases & errors
Empty input, unauthorized, not found, conflicts, limits — each with the expected
behavior. These become test cases too.

### Out of scope
What this feature explicitly does NOT do, so the coder doesn't over-build and
the test-writer doesn't test phantom behavior.

### Acceptance criteria
The checklist that means "done". Every item must be covered by at least one test.

## Rules
- Behavior and edge cases are written so a test can be derived from each line
  **without reading the implementation**. This is what keeps tests independent.
- Implement a feature part by part if needed, but the spec describes the whole
  feature — the one test file for that feature grows to match.
- If implementation reveals the spec was wrong or incomplete, update the spec
  first (and flag it to the user), then code/tests — never silently diverge.
- Keep the spec token-light: reference graph node IDs and file:line, never
  paste code blocks or full file contents. The coder and test-writer can look
  up the reference themselves — the spec only needs to point at it.
