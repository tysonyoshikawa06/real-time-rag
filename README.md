# Streaming RAG: operational intelligence over a live payment stream

An AI agent that answers operational questions about a live payment-transaction
stream — grounded in retrieved data, with cited transaction IDs — where events
become queryable within seconds of happening. The hard part is routing:
counting questions ("what's the failure rate by gateway?") need exact SQL,
meaning questions ("is anything weird in the errors?") need vector search over
messy free text, and the agent has to pick the right one and cite its evidence
either way.

## Key results

Measured on `eval/results/graded_20260715_011631.json` (source run
`eval/results/run_20260715_004828.json`, captured 2026-07-15). Full report:
[`eval/REPORT.md`](eval/REPORT.md). Recall study: [`eval/RECALL.md`](eval/RECALL.md).

**This run predates the `run_metadata` capture patch** (added after this report
was generated), so the report's own Methodology section records model, run
date, and git commit as `not recorded` — that is stated in the report itself,
not omitted here. The agent that answers questions in this system runs on
`claude-sonnet-5` (`agent/loop.py`); that's a fact about the code, not a
provenance field this particular eval run captured. `eval/RECALL.md`'s
measurements *are* pinned to a commit: `e48c4b1`.

**Every row below is n=1 repeat per question** unless stated otherwise —
single trials, not yet a stabilized pass rate (`make eval-run --repeats 3+`
would give one). Read the counts as "this is what happened once," not
"this is the system's success rate."

| Metric | Result | n |
|---|---|---|
| Citation validity (hallucination rate) | 11/11 valid, 0 fabricated, 0 ungrounded-but-real → **0.0%** | 11 citations / 6 runs |
| Aggregation accuracy — counts | 100% within 2% relative tolerance, MAE 3.0 | 7 extracted values |
| Aggregation accuracy — rates | 0% within 2pp absolute tolerance, MAE 6.4pp | 1 extracted value |
| Incident detection — gateway degradation | 1/1 (100%) | 1 |
| Incident detection — fraud burst | 1/1 (100%) | 1 |
| Incident detection — novel error pattern | 1/1 (100%, one assertion LLM-judged) | 1 |
| Negative control (hallucination question) | 1/1 (100%) | 1 |
| Tool routing accuracy | 6/6 (100%) | 6 |
| Freshness (5-min window) | p50 0.60s · p95 1.10s · p99 1.16s · max 1.27s | 5,988 events |

The rate-accuracy and fraud-detection rows above aren't as clean as they look
in isolation — `gateway_rate` and `fraud_pattern` are two of the six runs in
this graded file, and both *failed* their strict pass/fail check (see
[Engineering findings](#engineering-findings) and `eval/REPORT.md`'s Failure
analysis section). Both failures trace to the same mechanism: the agent's tool
call and the grader's independent ground-truth SQL snapshot happen a few
seconds apart on a live ~20 events/sec stream, and that gap alone can move a
count or rate outside tolerance — not a grounding or citation failure in
either case.

## Architecture

```mermaid
flowchart LR
    subgraph Stream["Producer"]
        P["scenario engine:<br/>gateway degradation,<br/>fraud burst, novel error"]
    end
    P -->|JSON events| K[("Kafka<br/>topic: transactions")]
    K --> C["Consumer<br/>(batch, then write)"]
    C -.->|commit offset<br/>after DB commit| K

    subgraph DB["Postgres + pgvector — one engine"]
        T[("transactions<br/>structured fields")]
        E[("embeddings<br/>384-dim vectors")]
    end
    C -->|every event<br/>ON CONFLICT DO NOTHING| T
    C -->|failure events only (~5-10%)<br/>enrich, then embed| E

    T --> MCP["MCP server (FastMCP)"]
    E --> MCP

    MCP -->|query_stats| A["Agent<br/>Anthropic tool-use loop"]
    MCP -->|semantic_search| A
    MCP -->|get_transactions| A
    MCP -->|system_freshness| A

    A --> CLI["CLI chat<br/>cites transaction_ids"]
```

Structured fields (amount, method, status, gateway) always land in
`transactions`. Only the ~5-10% of events carrying messy free-text errors
additionally get enriched and embedded into `embeddings` — that selective
branch is why the diagram forks after the consumer instead of embedding
everything.

## Quickstart (Windows)

Prerequisites: [Docker Desktop](https://www.docker.com/products/docker-desktop/),
Python 3.11+, [`uv`](https://docs.astral.sh/uv/getting-started/installation/),
and `make` — Windows doesn't ship `make`; install it with
`winget install ezwinports.make` or `choco install make`, or run each
Makefile target's underlying `uv run python -m ...` command directly (see
`Makefile`, every target is one line).

```
git clone <repo-url>
cd real-time-rag

uv sync

copy .env.example .env
# edit .env: set ANTHROPIC_API_KEY and POSTGRES_PASSWORD (any local value works for POSTGRES_PASSWORD)

make up
make smoke-db
```

Then, in two **separate terminals** (both need to keep running — `make chat`
won't have anything to talk about otherwise):

```
make produce      # terminal 2: generates the event stream
make consume      # terminal 3: loads the embedding model on first run
                   # (downloads ~80MB the very first time — this is normal, not hung)
```

In a fourth terminal:

```
make chat
```

Ask it something like `transactions in the last 10 minutes by method` or
`is anything wrong with our gateways right now?`.

To see the whole story in one command instead — brings up containers,
starts the producer/consumer if they aren't already running, injects all
three incident types, and asks the agent grounded questions about each:

```
make demo
```

## How it works

The producer generates a realistic baseline stream — weighted payment
methods, log-uniform amounts, a ~4% baseline failure rate — and a
control-file-driven scenario engine polls every 0.5s to bias live generation
into one of three incident types (gateway degradation, fraud burst, novel
error pattern), logging its own ground truth independently of anything the
database or agent later reports.

The consumer batches events (100 events or 1 second, whichever comes first)
and writes each batch inside one Postgres transaction: all events into
`transactions` first, then the failure subset embedded and inserted into
`embeddings`. Kafka offsets commit only *after* that DB transaction succeeds.
Kafka's own at-least-once delivery means a crash mid-batch causes redelivery,
but both inserts use `ON CONFLICT (transaction_id) DO NOTHING`, so a replayed
batch is silently absorbed — at-least-once delivery plus idempotent writes
adds up to effectively-once.

Embedding is selective and enriched: only events with `error_text` (roughly
5-10% of the stream) get embedded, and the text isn't embedded raw. A code
like `NSF` or `ERR_05` carries little meaning alone, so it's wrapped in
context first — `"{method} payment via {gateway} failed: {error_text}"` —
before going through the local `all-MiniLM-L6-v2` model. That enriched string
is stored alongside the vector, so any embedding is inspectable without
re-deriving it.

Retrieval is hybrid and time-filtered at the SQL layer, not in application
code: every query pushes its window and any status/gateway/method filter into
the `WHERE` clause before either path runs. For vector search specifically,
the search function first runs a cheap `COUNT(*)` over that same filtered
window; if the candidate set is small enough (≤50,000 rows) it forces an exact
sequential scan for guaranteed recall, otherwise it falls back to the HNSW
index. In practice, the narrow recent-window queries an operational agent
actually makes take the exact path almost every time — see
[Engineering findings](#engineering-findings) for why that fallback exists.

Four MCP tools are the agent's only way to touch the data: `query_stats`
(SQL aggregation — counts, rates, group-bys over a window), `semantic_search`
(vector search over recent failure text), `get_transactions` (fetch full rows
by ID or filter — the citation drill-down step), and `system_freshness`
(ingest-lag percentiles). All four validate their inputs through a shared
module before touching a connection — bounded windows, capped limits, an
enum/column allowlist, fully parameterized SQL.

The agent itself is a plain Anthropic API tool-use loop, no framework: send
messages, if `stop_reason` is `tool_use` execute every requested tool call
through the MCP bridge and loop, otherwise return the text. Its system prompt
carries the actual grounding contract — never answer from memory, every
factual claim traces to a tool call made in this conversation, empty results
are reported plainly rather than papered over, and every specific-behavior
claim carries a real transaction ID returned by a tool, never a constructed
one.

## Design decisions & tradeoffs

- **Kafka in KRaft mode, single broker.** Gets real producer/consumer-group
  semantics, offset commits, and resumability without standing up a 3-node
  cluster. Costs production realism — no replication, one point of failure.
- **Postgres + pgvector in one engine, not a separate vector DB.** One engine
  means one connection pool, ordinary joins between structured and vector
  data, and one thing to run locally. Costs the specialized ANN tuning and
  scale headroom a dedicated vector database would offer.
- **Local embeddings behind a swappable `Embedder` interface.**
  Sentence-transformers running on-box means ingest throughput isn't capped
  by an API rate limit, and embedding is free. Costs embedding quality
  relative to a hosted model — swappable later without touching callers.
- **Selective embedding.** Only the ~5-10% of events carrying free text get
  embedded, matching the actual retrieval need — structured aggregation never
  touches a vector. Costs that only failure text is semantically searchable;
  a success event has nothing to search for meaning in.
- **Enrichment before embedding.** Wrapping an opaque error code in
  method/gateway context before embedding gives the model a payment-failure
  frame instead of a bare string. Costs a fixed, hand-picked template shaping
  the resulting vector space.
- **Adaptive exact-scan vs. HNSW.** A cheap `COUNT(*)` decides the path:
  exact scan under 50,000 filtered candidates, HNSW above it. Guarantees
  100% recall on the narrow, recent-window queries that dominate real usage.
  Costs a full scan on broad, unfiltered queries — accepted because that's
  not the operational case.
- **Four narrow, validated tools instead of raw SQL access.** Bounds what the
  agent can do (column whitelist, capped windows/limits, parameterized
  values) and makes each tool's docstring do double duty as routing guidance.
  Costs generality — a new question shape needs a new tool, not a new query.
- **Clamp-vs-reject validation.** A too-large-but-sensible value (e.g.
  `limit=99999`) clamps to the max with a note the agent can read and
  self-correct from. An ambiguous or unsafe value (bad enum, non-positive
  integer, a transaction ID that would silently vanish from the requested
  set) rejects outright instead of guessing.

## Engineering findings

**The ANN recall investigation.** An early anecdote (Week 3) found that a
single, genuinely isolated novel-error vector was missed by HNSW even at
`ef_search=1000`, which motivated the exact-scan fallback described above.
Re-measuring that claim later (`eval/recall.py`, `eval/isolation_experiment.py`)
did not go cleanly: a bug in how the measurement script set Postgres's
scan-mode GUCs meant `SET LOCAL enable_seqscan=off` from one "hnsw" call was
silently leaking into the next "exact" call in the same connection, quietly
turning most of the "exact" ground truth into something other than a true
sequential scan — which is also why an earlier draft of this measurement
reported implausibly high recall. After fixing that (every call now sets
every relevant GUC explicitly, on and off), the live `embeddings` table's 591
accumulated novel-error rows — built up over roughly 10 days of repeated demo
and eval injections — no longer offered a clean test of the original
condition, since that history is itself a confound. So `eval/isolation_experiment.py`
rebuilds Step 10B's original condition directly, in a throwaway scratch
schema that never touches production tables: a 5,000-row ordinary baseline,
novel rows inserted incrementally at cluster sizes 1→100 with the HNSW index
never rebuilt. At `ef_search=40` — pgvector's own default, and what
production actually runs at on the unfiltered query path — `novel_recall`
was 0.00 at cluster size 1, climbing to 1.00 only once the cluster reached
roughly 25 rows: a clean reproduction of the original finding under
controlled conditions. At `ef_search=1000` — the specific value the original
anecdote named — the same isolated vector was recalled reliably
(`novel_recall=1.00`) from cluster size 1 onward, which does **not**
reproduce the "even at ef_search=1000" framing; enough query-time search
effort compensated for the missing graph edges in this run. The corrected
claim: this is a transient window tied to how connected the graph is around
a vector at insert time, not a permanent property of HNSW — and that's
precisely why the exact-scan fallback matters operationally, since a novel
error is by definition isolated exactly when you most need to detect it,
before the cluster has had time to grow.

**The tied-duplicates artifact.** Set-overlap recall (`id_recall`) turned out
to be close to meaningless on this corpus. The synthetic error text comes
from a small closed set of templates (`producer/errors.py`) crossed with a
handful of `method`/`gateway` values, so exact-duplicate embeddings are
common: 104,552 rows in `embeddings` reduce to only 308 distinct
`embedded_text` values, a 339.5x duplication ratio, with the largest clusters
running 1,000+ rows each. At that ratio, every one of the 35 queries in the
recall sweep had ties in its exact top-10 — the tie-free subset used as a
cross-check came back empty (0/35) — meaning "the true top-k" is frequently
not a unique set to begin with, and comparing IDs against an arbitrary slice
of a distance tie isn't a meaningful measurement. `distance_recall` (which
compares the two sorted distance arrays position-for-position, so an
equidistant substitution still counts as a match) is the metric that
actually reflects retrieval quality here, and it's the one `eval/RECALL.md`
leads with: mean 0.23 overall, 0.29 for ordinary queries, 0.00 for the small
isolated-query group. A templated generator producing this much clustering
is a corpus artifact, not a pgvector or HNSW property — real free-form error
text would not tie this way.

## Limitations & what I'd do next

The transaction and error data is entirely synthetic, drawn from a small
closed set of templates — that's what produces the extreme embedding
duplication documented above, and it means the vector-space structure here is
not representative of real, free-form error text. Everything runs local-only
via docker-compose; there is no deployment. All measurements come from a
single long-lived development environment, not a fleet of independent trials,
and several incident-question answers show visible residue from many earlier
manual verification runs against the same fixed targets. The agent's tool-use
path and phrasing are stochastic — a single failing repeat isn't proof of a
systemic bug, and a single passing repeat isn't proof of robustness, which is
why `eval/REPORT.md` reports pass rates rather than pass/fail. The current
report is n=1 repeat per question; `make eval-run --repeats 3` (or higher)
would produce a more stable rate. LLM-as-judge grading is used for exactly
one assertion (`novel_error`'s semantic check) and is never blended into any
headline percentage.

Next steps, roughly in order: containerize the consumer instead of running it
as a bare process; move the consumer to a real multi-partition consumer
group instead of a single instance; deploy the stack instead of local-only;
replace the templated error corpus with richer, non-templated text so
duplication stops being the dominant confound in recall measurement; and
re-run the recall sweep on a freshly built index at larger scale, since the
current live corpus's history is itself now a confound (per the findings
above).

## Repo tour

| Path | What's there |
|---|---|
| `producer/` | Baseline event generator + scenario engine (incident injection, ground-truth log) |
| `consumer/` | Kafka → Postgres batch writer, selective embedding, freshness/search queries |
| `mcp_server/` | FastMCP server exposing the four retrieval tools, plus shared input validation |
| `agent/` | Anthropic tool-use loop, system prompt, MCP bridge, CLI chat |
| `demo/` | Scripted end-to-end demo orchestrator + the golden question set |
| `eval/` | Golden-question runner, offline grader, recall measurement, generated reports |
| `infra/` | docker-compose (Kafka KRaft, Postgres+pgvector), schema, smoke tests |
| `tests/` | Independent test suite, written from specs rather than from the implementation |
| `.claude/` | Feature specs (`specs/`), agent/skill definitions driving this project's build process |

Regenerate the numbers above:

```
make eval-run       # capture a fresh run against the live agent
make eval-grade     # grade it against independent ground truth
make eval-report    # regenerate eval/REPORT.md from the latest graded file
```
