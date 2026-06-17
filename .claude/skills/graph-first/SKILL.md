---
name: graph-first
description: Standing rule for every agent — consult the graphify knowledge graph before exploring or changing code, and fall back to direct file access when needed. Use at the start of any task that touches the codebase, even if not explicitly asked. Preloaded by all agents.
---

# Graph first

Before exploring, changing, or testing code, consult the graphify graph. It is
faster and cheaper than reading the whole tree.

## How
1. Check whether `graphify-out/graph.json` exists.
2. If it does, query it for what you need **before** reading files:
   ```bash
   graphify query "<your question>"
   graphify path "<NodeA>" "<NodeB>"    # how two things connect
   graphify explain "<Node>"             # what one thing is / does
   ```
   `graphify-out/GRAPH_REPORT.md` gives the architecture overview and god nodes.
3. Use the graph to locate the right files and understand connections, then open
   only the files you actually need.

## When to go straight to files
You are allowed to read or edit files directly — skipping or supplementing the
graph — when:
- no `graphify-out/` exists yet, or
- the graph doesn't answer the question, or
- you need to make a change, pinpoint a specific bug, or run/inspect tests.

The graph orients you; the files are ground truth. Lead with the graph, confirm
in the files.
