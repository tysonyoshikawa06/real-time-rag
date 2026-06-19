# Orchestrator workflow

This file drives the **main Claude Code session** as the orchestrator for a
spec → code + tests → run → fix loop. Do not delegate orchestration to a
subagent; the main thread coordinates the agents.

## Before anything: consult the graph

Always check the graphify knowledge graph before exploring or changing code.
- If `graphify-out/graph.json` exists, query it first instead of grepping:
  `graphify query "<question>"`, `graphify path "<A>" "<B>"`,
  `graphify explain "<node>"`. Read `graphify-out/GRAPH_REPORT.md` for the
  architecture overview.
- If no graph exists, or it doesn't answer the question, dive into the files
  directly (Glob/Grep/Read). This is allowed whenever you need to make a change,
  pinpoint a bug, or locate/run tests.
Every agent does the same via the `graph-first` skill.

## 1. Lock the spec (do not skip)

When given a feature request:
1. Ask **specific** clarifying questions — scope, inputs/outputs, edge cases,
   acceptance criteria. Do not start until you are 100% sure what the user wants.
2. Confirm your understanding back to the user and get explicit sign-off.
3. Rewrite the agreed requirement into a Claude-readable spec following the
   `spec-format` skill, and write it to `.claude/specs/<feature>.spec.md`.

The spec file is the single source of truth. The coder and test-writer both work
from it and hand off through it — **not** through conversation context.

## 2. Implement feature by feature

Work one feature at a time. For the current feature:
- Dispatch the **coder** to implement it from the spec file.
- Dispatch the **test-writer** to write tests from the **same spec file**, not
  from the coder's code. These two can run in parallel — their independence is
  what makes the tests a real check.
- One test file per feature. As a feature is built part by part, the test-writer
  appends cases to that one file rather than creating new files.

## 3. Run-and-fix loop

Once the coder and test-writer both finish the feature:
1. Dispatch the **test-runner** to run that feature's tests and report failures.
2. If any test fails, dispatch the **coder** to debug and fix the *code* (never
   the tests), then dispatch the test-runner again.
3. Repeat until all tests pass, or until **5 fix attempts**, then stop and
   surface the remaining failures with what was tried.

The coder must never edit test files (a hook enforces this). If a test is
genuinely wrong, stop and raise it with the user — do not let the implementer
rewrite its own target.

## 4. Capture learnings

At the end of a feature or session, run the `retro` skill to consolidate what
the agents learned and propose updates to the skill/agent files. Show proposed
edits to the user before applying them.

## Delegation

Name agents explicitly so delegation is deterministic: "Use the coder subagent
to…", "Use the test-runner subagent to…".

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
