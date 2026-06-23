---
name: retro
description: Capture what the agents learned this session and propose updates to the skill and agent markdown files so the setup improves over time. Use at the end of a feature or session, or when the user asks to "save learnings", "update the agents", or "do a retro". Invoked by the orchestrator.
---

# Retro: learn and update the setup

Turn this session's lessons into durable improvements to the `.claude/` config.
This is how the agents get better across sessions.

## Sources of learning

- Each agent runs with `memory: project`, so it keeps a `MEMORY.md` under
  `.claude/agent-memory/<agent>/`. Read these for accumulated notes.
- The current session: recurring fixes, spec gaps that caused rework, test
  patterns that worked, gotchas in the run recipe, wrong graph assumptions.
- At the end of every change, update the `PROJECTSTATE.md` file at the root of this project
  to reflect the changes you made.

## What to do

1. Summarize concrete, **reusable** lessons (not one-off details).
2. Map each lesson to the file that should change:
   - a convention or recipe detail → the relevant skill (`run-tests`,
     `spec-format`, `graph-first`)
   - a behavior change for a role → that agent's markdown
   - cross-session notes that aren't rules yet → leave them in the agent's
     `MEMORY.md`
3. Draft the exact edits.
4. **Show the proposed edits to the user and get approval before applying them.**
   Never silently rewrite an agent's or skill's own definition.
5. Apply approved edits. Changes to agent/skill markdown load at the next session
   start (or immediately via the `/agents` and `/skills` interfaces).

## Guardrails

- Keep skills focused — tighten wording, don't pile on. A skill that grows
  unbounded triggers worse and costs more context.
- Don't promote a one-time workaround into a permanent rule.
- Preserve each `name` and `description` triggering text unless improving the
  trigger is the explicit goal.
