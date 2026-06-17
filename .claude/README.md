# Orchestrated TDD setup for Claude Code

A drop-in `.claude/` config (plus a root `CLAUDE.md`) for a spec-driven,
test-validated build loop. The **normal `claude` session is the orchestrator** —
`CLAUDE.md` auto-loads and tells it how to drive three subagents.

## Layout

```
CLAUDE.md                         # orchestrator workflow (repo ROOT, auto-loaded)
.claude/
├── agents/
│   ├── coder.md                  # Read,Edit,Write,Bash,Grep,Glob · can't edit tests (hook)
│   ├── test-writer.md            # writes tests from the SPEC, not the code
│   └── test-runner.md            # read-only · runs + reports failures
├── skills/
│   ├── spec-format/              # the Claude-readable spec contract
│   ├── run-tests/                # project test recipe — FILL IN THE BRACKETS
│   ├── graph-first/              # consult graphify before touching files
│   └── retro/                    # update skills/agents from past sessions
├── scripts/
│   └── block-test-edits.sh       # PreToolUse hook: blocks coder edits to tests
└── specs/                        # orchestrator writes <feature>.spec.md here
```

## The loop

1. **Lock the spec.** The orchestrator asks specific questions until it's 100%
   sure, confirms with you, then writes `.claude/specs/<feature>.spec.md` in the
   `spec-format` shape.
2. **Build + test, in parallel.** The **coder** implements from the spec; the
   **test-writer** writes tests from the _same spec, independently_ (one file per
   feature, appended as the feature grows). Handoff is through the spec file on
   disk, not conversation context.
3. **Run + fix.** The **test-runner** runs the feature's tests and reports
   failures. The orchestrator sends failures back to the coder to fix the _code_,
   then reruns — up to 5 attempts, then it stops and surfaces what's left.
4. **Retro.** At the end, the `retro` skill consolidates learnings and proposes
   edits to the skills/agents for your approval.

## How the requirements are wired

- **Graph first:** `graph-first` is preloaded into all three agents and stated at
  the top of `CLAUDE.md`. Each consults `graphify-out/graph.json` before reading
  files, and falls back to direct file access when there's no graph or it needs
  specifics.
- **Test validity:** the test-writer derives tests from the spec, never the code,
  and the coder is _mechanically_ blocked from editing test files by the
  `block-test-edits.sh` hook (not just told not to).
- **Self-improvement:** every agent runs `memory: project`, accumulating a
  `MEMORY.md` under `.claude/agent-memory/<agent>/`; `retro` turns those into
  approved edits to the markdowns.
- **Each agent uses its skills:** preloaded via each agent's `skills:` field.

## Before you run it — required setup

1. **Fill in `skills/run-tests/SKILL.md`.** It ships with `[ brackets ]` for your
   test command, env, and file layout. Or generate it: run Claude Code's bundled
   `/run-skill-generator` once and point the agents at the result.
2. **Make the hook executable and install `jq`:**
   ```bash
   chmod +x .claude/scripts/block-test-edits.sh
   # jq is required by the hook: apt install jq / brew install jq
   ```
   Zip extraction may drop the executable bit — re-run `chmod +x` after unzip.
3. **Tune the hook regex** in `block-test-edits.sh` to match your real test paths.
4. **Restart Claude Code** (or use `/agents` and `/skills`) so the new files load.
5. **Commit `.claude/` and `CLAUDE.md`** so the whole setup travels with the repo.
