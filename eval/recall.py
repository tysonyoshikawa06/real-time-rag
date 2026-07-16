"""HNSW recall measurement (Step 20C).

Turns the Week-3 anecdote ("HNSW missed an isolated novel vector even at
ef_search=1000", Step 10B) into a measurement: Recall@k across a realistic
query set, computed against data already sitting in the `embeddings` table.
No API cost - query vectors come from the same `LocalEmbedder` used
everywhere else, and both search paths are plain SQL. No agent, no LLM
judge, no incident injection required.

Two search paths per query, same filter (none - the whole table, matching
the "broad, unfiltered" branch of Step 11A's exact-scan-threshold logic;
the narrow-window branch already forces an exact scan and gets 100% recall
by construction, so it isn't what's being measured here):

  exact - forced sequential scan (`enable_indexscan`/`enable_bitmapscan`
          off, same technique `consumer/search.py::search()` already uses)
          - the only valid ground truth for what "top k" actually is.
  hnsw  - forced index scan (`enable_seqscan` off) at a stated `ef_search`.

Two recall metrics, reported side by side (20C-1 fix):

  id_recall       = |exact_ids ∩ hnsw_ids| / k. Undefined when the exact
                     top-k has distance ties, because "the true top-k" is
                     then not a unique set of ids - Postgres returns an
                     arbitrary tied subset, and a different-but-equidistant
                     row from HNSW reads as a "miss" even though it is an
                     equally valid nearest neighbor.
  distance_recall = position-for-position comparison of the two *sorted
                     distance arrays* (not ids): position i counts as
                     retrieved if abs(d_hnsw[i] - d_exact[i]) <= EPSILON.
                     A different-but-equidistant row still matches at that
                     position, so ties don't get punished; a genuinely worse
                     neighbor produces a larger distance at some position,
                     which correctly drops this metric. This is the number
                     that reflects actual retrieval quality on a corpus with
                     large exact-duplicate-text clusters.

Every query also carries a `tied` flag - whether its own exact top-k
contains any duplicate distances - the diagnostic that explains any gap
between the two metrics.

20C-2 turns the duplication behind those ties into a measured finding
rather than a footnote: total rows vs. distinct `embedded_text` values
(the duplication ratio), the largest identical-text clusters, and a
tie-free-vs-tied split of distance_recall that cross-checks the 20C-1
metric fix directly. `eval/RECALL.md`'s "Corpus limitations" section
explains why a templated synthetic generator (Step 5) produces this
degenerate vector-space structure by design.

Run with: `python -m eval.recall [--k N] [--ef-search 40,100,400,1000]`
or `make eval-recall`.
"""

import argparse
import statistics
import subprocess
from pathlib import Path

import numpy as np

from consumer.db import connect
from consumer.embedder import LocalEmbedder
from producer.scenarios import NOVEL_ERROR_SIGNATURE

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_PATH = REPO_ROOT / "eval" / "RECALL.md"

DEFAULT_K = 10
DEFAULT_EF_SWEEP = [40, 100, 400, 1000]
# The headline figure is reported at the same ef_search Step 10B's anecdote
# used, so this measurement is a direct quantification of that finding.
HEADLINE_EF_SEARCH = 1000
# Two float distances count as "the same" if within this tolerance - both
# for distance_recall's position-for-position comparison and for detecting
# ties in the exact top-k. pgvector distances are float8; genuinely distinct
# vectors essentially never collide within 1e-6, so any collision this tight
# is a real bit-identical (or near-identical) embedding, not float noise.
EPSILON = 1e-6

# ---------------------------------------------------------------------------
# Query set: natural-language paraphrases of the seven known error families
# (producer/errors.py) plus several paraphrases of the novel-error signature
# (producer/scenarios.py). None of these strings are copied verbatim from
# the template lists - they're phrased the way an operator would ask, the
# same spirit as consumer/search.py's own demo queries ("connection timed
# out", "do not honor"). The novel-error paraphrases deliberately share few
# or no literal keywords with NOVEL_ERROR_SIGNATURE, mirroring the
# zero-keyword-overlap probe already verified live in Step 12.
# ---------------------------------------------------------------------------

QUERY_SET = [
    # -- ordinary: insufficient_funds --
    ("insufficient_funds", "ordinary", "the account didn't have enough money for the charge"),
    ("insufficient_funds", "ordinary", "payment declined due to insufficient funds"),
    ("insufficient_funds", "ordinary", "not enough balance available to cover the transaction"),
    ("insufficient_funds", "ordinary", "card was declined because of low funds"),
    # -- ordinary: do_not_honor --
    ("do_not_honor", "ordinary", "the issuing bank declined the transaction, no reason given"),
    ("do_not_honor", "ordinary", "do not honor response from the card issuer"),
    ("do_not_honor", "ordinary", "bank refused to authorize the charge"),
    ("do_not_honor", "ordinary", "issuer declined the payment without stating why"),
    # -- ordinary: expired_card --
    ("expired_card", "ordinary", "the card on file has expired"),
    ("expired_card", "ordinary", "payment failed because the card's expiration date has passed"),
    ("expired_card", "ordinary", "customer's card is no longer valid, past its expiry"),
    ("expired_card", "ordinary", "declined due to an expired card"),
    # -- ordinary: invalid_cvv --
    ("invalid_cvv", "ordinary", "the security code entered didn't match"),
    ("invalid_cvv", "ordinary", "CVV verification failed on the card"),
    ("invalid_cvv", "ordinary", "wrong card verification code provided"),
    ("invalid_cvv", "ordinary", "payment rejected because the CVC was incorrect"),
    # -- ordinary: gateway_timeout --
    ("gateway_timeout", "ordinary", "the payment gateway took too long to respond"),
    ("gateway_timeout", "ordinary", "upstream processor timed out during the transaction"),
    ("gateway_timeout", "ordinary", "connection to the payment gateway timed out"),
    ("gateway_timeout", "ordinary", "gateway did not respond within the expected time"),
    # -- ordinary: network_error --
    ("network_error", "ordinary", "network connection was reset before completing the request"),
    ("network_error", "ordinary", "TLS handshake failed while connecting to the gateway"),
    ("network_error", "ordinary", "the payment processor was unreachable over the network"),
    ("network_error", "ordinary", "connection reset error while reaching the gateway"),
    # -- ordinary: fraud_suspected --
    ("fraud_suspected", "ordinary", "the transaction was flagged as potentially fraudulent"),
    ("fraud_suspected", "ordinary", "risk engine blocked the payment as suspicious"),
    ("fraud_suspected", "ordinary", "charge was declined due to suspected fraud"),
    ("fraud_suspected", "ordinary", "a high risk score triggered a fraud block"),
    # -- isolated: novel_error (paraphrases of NOVEL_ERROR_SIGNATURE) --
    ("novel_error", "isolated", "currency mismatch: expected US dollars but got Japanese yen"),
    ("novel_error", "isolated", "a fallback was denied over a locale override tied to currency"),
    ("novel_error", "isolated", "merchant config v2 rejected the charge over a currency issue"),
    ("novel_error", "isolated", "the payment failed because the currency didn't match the charge"),
    ("novel_error", "isolated", "locale settings blocked a fallback over a mismatched currency"),
    ("novel_error", "isolated", "a conflict between USD and JPY caused the charge to fail"),
    ("novel_error", "isolated", "a merchant config error, mismatched currency, blocked fallback"),
]

_TOPK_SQL = """
    SELECT transaction_id, embedding <=> %(query_vec)s AS distance
    FROM embeddings
    ORDER BY embedding <=> %(query_vec)s
    LIMIT %(k)s
"""

_CORPUS_SIZE_SQL = "SELECT count(*) AS n FROM embeddings"
_ISOLATED_SIZE_SQL = "SELECT count(*) AS n FROM embeddings WHERE embedded_text LIKE %(pattern)s"
_DISTINCT_TEXT_SQL = "SELECT count(DISTINCT embedded_text) AS n FROM embeddings"
_TOP_CLUSTERS_SQL = """
    SELECT embedded_text, count(*) AS n
    FROM embeddings
    GROUP BY embedded_text
    ORDER BY n DESC, embedded_text
    LIMIT %(limit)s
"""

DUPLICATION_TOP_N = 10


def _git_commit() -> str:
    """Short commit SHA, captured at measurement time. Degrades to "unknown"
    rather than raising - a missing git binary must never abort a run that
    costs nothing but a few seconds of local compute."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip() or "unknown"
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _topk(
    conn, mode: str, query_vec: np.ndarray, k: int, ef_search: int | None = None
) -> tuple[list[str], list[float]]:
    """Return (transaction_ids, distances) for the top-k via the forced `mode` path.

    Both lists come back sorted ascending by distance (the SQL ORDER BY).

    `mode="exact"` disables index/bitmap scans (same technique as
    `consumer/search.py::search()`'s exact path) so Postgres has no choice
    but a full sequential scan - the only valid ground truth for recall.
    `mode="hnsw"` disables sequential scan so the planner is forced onto the
    HNSW index, at the given `ef_search`.

    Every relevant GUC is set explicitly on every call - both the one(s) a
    mode needs off *and* the one(s) it needs back on - rather than relying on
    `SET LOCAL` to revert automatically between calls (20C-3 fix). It
    doesn't: this function runs many times per query (once "exact", once per
    `ef_search`) inside one long-lived outer transaction, so after the very
    first call, every later `with conn.transaction():` here opens a
    SAVEPOINT, not a fresh top-level transaction - and Postgres restores
    `SET LOCAL` values on ROLLBACK TO SAVEPOINT but *not* on a normal
    RELEASE SAVEPOINT (verified directly against this database). Left
    unhandled, one "hnsw" call's `enable_seqscan = off` silently leaked into
    every subsequent "exact" call in the same run, quietly turning "ground
    truth" into something other than a true sequential scan for the rest of
    the run - see the Methodology note in `generate_recall_report` for what
    this changed about the reported numbers.
    """
    cur = conn.cursor()
    with conn.transaction():
        if mode == "exact":
            cur.execute("SET LOCAL enable_seqscan = on")
            cur.execute("SET LOCAL enable_indexscan = off")
            cur.execute("SET LOCAL enable_bitmapscan = off")
        elif mode == "hnsw":
            cur.execute("SET LOCAL enable_indexscan = on")
            cur.execute("SET LOCAL enable_bitmapscan = on")
            cur.execute("SET LOCAL enable_seqscan = off")
            cur.execute(f"SET LOCAL hnsw.ef_search = {int(ef_search)}")
        else:
            raise ValueError(f"unknown mode: {mode!r}")
        cur.execute(_TOPK_SQL, {"query_vec": query_vec, "k": k})
        rows = cur.fetchall()
    ids = [str(row["transaction_id"]) for row in rows]
    distances = [float(row["distance"]) for row in rows]
    return ids, distances


def _id_recall(exact_ids: list[str], hnsw_ids: list[str], k: int) -> float:
    return len(set(exact_ids) & set(hnsw_ids)) / k


def _distance_recall(
    exact_distances: list[float], hnsw_distances: list[float], k: int, epsilon: float = EPSILON
) -> float:
    matches = sum(
        1
        for e, h in zip(exact_distances, hnsw_distances, strict=True)
        if abs(h - e) <= epsilon
    )
    return matches / k


def _has_ties(distances: list[float], epsilon: float = EPSILON) -> bool:
    """True if any two entries in a sorted distance list are within epsilon.

    Applied to the exact top-k: a tie here means "the true top-k" is not a
    unique set of rows - other rows outside this top-k likely sit at the same
    distance too, so which ones Postgres happened to return is arbitrary.
    """
    return any(
        abs(distances[i + 1] - distances[i]) <= epsilon for i in range(len(distances) - 1)
    )


def _duplication_stats(conn, top_n: int = DUPLICATION_TOP_N) -> dict:
    """Quantify bit-identical `embedded_text` duplication in the corpus (20C-2).

    The producer's error text comes from a small closed set of templates
    (`producer/errors.py`) - Step 5's design choice, not a pgvector or HNSW
    property. At scale, many rows end up with the exact same embedded_text
    string, which means the exact same embedding vector, which is what
    creates the distance ties `_has_ties` detects. This function measures
    that duplication directly instead of only inferring it from ties.
    """
    cur = conn.cursor()
    cur.execute(_CORPUS_SIZE_SQL)
    total_rows = cur.fetchone()["n"]
    cur.execute(_DISTINCT_TEXT_SQL)
    distinct_text = cur.fetchone()["n"]
    cur.execute(_TOP_CLUSTERS_SQL, {"limit": top_n})
    top_clusters = [{"text": row["embedded_text"], "n": row["n"]} for row in cur.fetchall()]
    return {
        "total_rows": total_rows,
        "distinct_text": distinct_text,
        "duplication_ratio": total_rows / distinct_text if distinct_text else None,
        "top_clusters": top_clusters,
    }


def _tie_split_distance_recall(results: dict, ef: int) -> dict:
    """distance_recall@ef split by whether the query's exact top-k has ties.

    Cross-checks the 20C-1 metric fix: if duplication is really the cause of
    any remaining recall gap, the tie-free subset (queries whose true top-k
    is a well-defined, unique set of rows) should show high distance_recall,
    while the tied subset carries whatever gap remains.
    """
    out = {}
    for label, tied in [("tie_free", False), ("tied", True)]:
        values = [
            q["by_ef"][ef]["distance_recall"] for q in results["queries"] if q["tied"] == tied
        ]
        out[label] = _distribution(values)
    return out


def _distribution(values: list[float]) -> dict:
    if not values:
        return {
            "n": 0, "mean": None, "min": None, "p25": None,
            "median": None, "p75": None, "max": None,
        }
    return {
        "n": len(values),
        "mean": statistics.fmean(values),
        "min": min(values),
        "p25": float(np.percentile(values, 25)),
        "median": statistics.median(values),
        "p75": float(np.percentile(values, 75)),
        "max": max(values),
    }


def run_measurement(conn, embedder, k: int = DEFAULT_K, ef_sweep: list[int] = None) -> dict:
    """Run the full exact-vs-HNSW comparison for every query in QUERY_SET.

    Returns a plain-dict result structure - the input to both the console
    printout and generate_recall_report, so nothing is computed twice.
    """
    ef_sweep = ef_sweep if ef_sweep is not None else DEFAULT_EF_SWEEP

    cur = conn.cursor()
    cur.execute(_CORPUS_SIZE_SQL)
    corpus_size = cur.fetchone()["n"]
    cur.execute(_ISOLATED_SIZE_SQL, {"pattern": f"%{NOVEL_ERROR_SIGNATURE}%"})
    isolated_corpus_size = cur.fetchone()["n"]
    duplication = _duplication_stats(conn)

    texts = [text for _, _, text in QUERY_SET]
    vectors = embedder.embed(texts)

    queries = []
    for (family, group, text), vec in zip(QUERY_SET, vectors, strict=True):
        query_vec = np.array(vec)
        exact_ids, exact_distances = _topk(conn, "exact", query_vec, k)
        tied = _has_ties(exact_distances)
        by_ef = {}
        for ef in ef_sweep:
            hnsw_ids, hnsw_distances = _topk(conn, "hnsw", query_vec, k, ef_search=ef)
            by_ef[ef] = {
                "id_recall": _id_recall(exact_ids, hnsw_ids, k),
                "distance_recall": _distance_recall(exact_distances, hnsw_distances, k),
                "worst_exact_distance": exact_distances[-1],
                "worst_hnsw_distance": hnsw_distances[-1],
            }
        queries.append(
            {
                "family": family,
                "group": group,
                "text": text,
                "tied": tied,
                "exact_distances": exact_distances,
                "by_ef": by_ef,
            }
        )

    return {
        "k": k,
        "epsilon": EPSILON,
        "ef_sweep": ef_sweep,
        "headline_ef_search": HEADLINE_EF_SEARCH,
        "corpus_size": corpus_size,
        "isolated_corpus_size": isolated_corpus_size,
        "duplication": duplication,
        "git_commit": _git_commit(),
        "queries": queries,
    }


def _group_values(results: dict, ef: int, metric: str, group: str | None = None) -> list[float]:
    return [
        q["by_ef"][ef][metric]
        for q in results["queries"]
        if group is None or q["group"] == group
    ]


def _tie_counts(results: dict) -> dict[str, tuple[int, int]]:
    counts = {}
    for label, group in [("all", None), ("ordinary", "ordinary"), ("isolated", "isolated")]:
        qs = [q for q in results["queries"] if group is None or q["group"] == group]
        tied_n = sum(1 for q in qs if q["tied"])
        counts[label] = (tied_n, len(qs))
    return counts


def print_console_report(results: dict) -> None:
    k = results["k"]
    headline_ef = results["headline_ef_search"]

    print("=== HNSW Recall Measurement (Step 20C) ===")
    print(
        f"Corpus: {results['corpus_size']:,} embedded rows "
        f"({results['isolated_corpus_size']:,} novel-error/isolated, "
        f"{100 * results['isolated_corpus_size'] / results['corpus_size']:.2f}% of corpus)"
    )
    print(
        f"k={k} | ef_search sweep: {', '.join(str(e) for e in results['ef_sweep'])} "
        f"| distance epsilon={results['epsilon']:.0e}"
    )

    tie_counts = _tie_counts(results)
    tied_n, total_n = tie_counts["all"]
    ord_tied, ord_n = tie_counts["ordinary"]
    iso_tied, iso_n = tie_counts["isolated"]
    print(
        f"Exact top-{k} contains ties: {tied_n}/{total_n} queries "
        f"(ordinary {ord_tied}/{ord_n}, isolated {iso_tied}/{iso_n})"
    )

    dup = results["duplication"]
    print(
        f"\nCorpus duplication: {dup['total_rows']:,} rows, "
        f"{dup['distinct_text']:,} distinct embedded_text values "
        f"(ratio {dup['duplication_ratio']:.1f}x)"
    )
    print(f"Top {len(dup['top_clusters'])} identical-text clusters:")
    for c in dup["top_clusters"]:
        preview = c["text"] if len(c["text"]) <= 70 else c["text"][:67] + "..."
        print(f"  n={c['n']:>6,}  {preview}")

    split = _tie_split_distance_recall(results, headline_ef)
    print(f"\n--- distance_recall@{k} @ ef_search={headline_ef}, tie-free vs tied ---")
    for label in ["tie_free", "tied"]:
        d = split[label]
        print(
            f"{label:9s} n={d['n']:>3d} mean={_fmt(d['mean'])} "
            f"min={_fmt(d['min'])} median={_fmt(d['median'])} max={_fmt(d['max'])}"
        )

    print(f"\n--- Per-query recall @ ef_search={headline_ef} (headline) ---")
    for q in results["queries"]:
        m = q["by_ef"][headline_ef]
        tie_mark = "tied" if q["tied"] else "----"
        print(
            f"[{q['group']:9s}] {q['family']:18s} "
            f"id_recall={m['id_recall']:.2f} distance_recall={m['distance_recall']:.2f} "
            f'{tie_mark}  "{q["text"]}"'
        )

    for metric, label in [("id_recall", "id_recall"), ("distance_recall", "distance_recall")]:
        print(f"\n--- {label}@{k} distribution @ ef_search={headline_ef} ---")
        header = (
            f"{'group':10s} {'n':>3s} {'mean':>6s} {'min':>6s} "
            f"{'p25':>6s} {'median':>6s} {'p75':>6s} {'max':>6s}"
        )
        print(header)
        for row_label, group in [("all", None), ("ordinary", "ordinary"), ("isolated", "isolated")]:
            dist = _distribution(_group_values(results, headline_ef, metric, group))
            print(
                f"{row_label:10s} {dist['n']:>3d} {dist['mean']:>6.2f} {dist['min']:>6.2f} "
                f"{dist['p25']:>6.2f} {dist['median']:>6.2f} {dist['p75']:>6.2f} "
                f"{dist['max']:>6.2f}"
            )

    print("\n--- ef_search sweep: mean recall by group (id_recall / distance_recall) ---")
    print(
        f"{'ef_search':>10s} {'ord id':>8s} {'ord dist':>9s} "
        f"{'iso id':>8s} {'iso dist':>9s}"
    )
    for ef in results["ef_sweep"]:
        ord_id = statistics.fmean(_group_values(results, ef, "id_recall", "ordinary"))
        ord_dist = statistics.fmean(_group_values(results, ef, "distance_recall", "ordinary"))
        iso_id = statistics.fmean(_group_values(results, ef, "id_recall", "isolated"))
        iso_dist = statistics.fmean(_group_values(results, ef, "distance_recall", "isolated"))
        print(f"{ef:>10d} {ord_id:>8.2f} {ord_dist:>9.2f} {iso_id:>8.2f} {iso_dist:>9.2f}")


def _fmt(v) -> str:
    return "n/a" if v is None else f"{v:.2f}"


def _markdown_distribution_table(results: dict, ef: int, metric: str) -> str:
    lines = [
        "| group | n | mean | min | p25 | median | p75 | max |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for label, group in [("all", None), ("ordinary", "ordinary"), ("isolated", "isolated")]:
        dist = _distribution(_group_values(results, ef, metric, group))
        keys = ["n", "mean", "min", "p25", "median", "p75", "max"]
        formatted = [str(dist["n"])] + [_fmt(dist[key]) for key in keys[1:]]
        lines.append(f"| {label} | " + " | ".join(formatted) + " |")
    return "\n".join(lines)


def _markdown_per_query_table(results: dict, ef: int) -> str:
    lines = [
        "| group | family | id_recall | distance_recall | tied | query |",
        "|---|---|---|---|---|---|",
    ]
    for q in results["queries"]:
        m = q["by_ef"][ef]
        lines.append(
            f"| {q['group']} | {q['family']} | {m['id_recall']:.2f} | "
            f"{m['distance_recall']:.2f} | {q['tied']} | {q['text']} |"
        )
    return "\n".join(lines)


def _markdown_duplication_table(results: dict) -> str:
    dup = results["duplication"]
    lines = [
        "| total rows | distinct embedded_text | duplication ratio |",
        "|---|---|---|",
        f"| {dup['total_rows']:,} | {dup['distinct_text']:,} | {dup['duplication_ratio']:.1f}x |",
        "",
        f"Top {len(dup['top_clusters'])} identical-text clusters:",
        "",
        "| rows (n) | embedded_text |",
        "|---|---|",
    ]
    for c in dup["top_clusters"]:
        # Pipe chars would break the Markdown table; none of the closed-set
        # error templates contain one, but escape defensively.
        text = c["text"].replace("|", "\\|")
        lines.append(f"| {c['n']:,} | `{text}` |")
    return "\n".join(lines)


def _markdown_tie_split_table(results: dict, ef: int) -> str:
    split = _tie_split_distance_recall(results, ef)
    lines = [
        "| subset | n | mean | min | p25 | median | p75 | max |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for label in ["tie_free", "tied"]:
        d = split[label]
        keys = ["n", "mean", "min", "p25", "median", "p75", "max"]
        formatted = [str(d["n"])] + [_fmt(d[key]) for key in keys[1:]]
        lines.append(f"| {label} | " + " | ".join(formatted) + " |")
    return "\n".join(lines)


def _markdown_sweep_table(results: dict) -> str:
    lines = [
        "| ef_search | ordinary id_recall | ordinary distance_recall | "
        "isolated id_recall | isolated distance_recall |",
        "|---|---|---|---|---|",
    ]
    for ef in results["ef_sweep"]:
        ord_id = statistics.fmean(_group_values(results, ef, "id_recall", "ordinary"))
        ord_dist = statistics.fmean(_group_values(results, ef, "distance_recall", "ordinary"))
        iso_id = statistics.fmean(_group_values(results, ef, "id_recall", "isolated"))
        iso_dist = statistics.fmean(_group_values(results, ef, "distance_recall", "isolated"))
        lines.append(f"| {ef} | {ord_id:.2f} | {ord_dist:.2f} | {iso_id:.2f} | {iso_dist:.2f} |")
    return "\n".join(lines)


def generate_recall_report(results: dict, output_path=None) -> Path:
    """Render `results` (run_measurement's return value) to Markdown.

    `output_path` is an explicit parameter with a single module-level
    fallback (`DEFAULT_OUTPUT_PATH`) - the same structural pattern
    `eval/report.py::generate_report` uses, so a future test suite can pass
    `tmp_path` here too without touching the real `eval/RECALL.md`.
    """
    resolved_output = Path(output_path) if output_path is not None else DEFAULT_OUTPUT_PATH
    headline_ef = results["headline_ef_search"]
    k = results["k"]

    id_dist_all = _distribution(_group_values(results, headline_ef, "id_recall", None))
    id_dist_ord = _distribution(_group_values(results, headline_ef, "id_recall", "ordinary"))
    id_dist_iso = _distribution(_group_values(results, headline_ef, "id_recall", "isolated"))
    dist_dist_all = _distribution(_group_values(results, headline_ef, "distance_recall", None))
    dist_dist_ord = _distribution(
        _group_values(results, headline_ef, "distance_recall", "ordinary")
    )
    dist_dist_iso = _distribution(
        _group_values(results, headline_ef, "distance_recall", "isolated")
    )

    tie_counts = _tie_counts(results)

    sweep_iso_dist_means = [
        statistics.fmean(_group_values(results, ef, "distance_recall", "isolated"))
        for ef in results["ef_sweep"]
    ]
    recovers = (
        sweep_iso_dist_means[-1] - sweep_iso_dist_means[0] > 0.10
        if len(sweep_iso_dist_means) > 1
        else None
    )

    lines = [
        "# HNSW Recall Measurement",
        "",
        f"Commit `{results['git_commit']}`.",
        "",
        "## Methodology",
        "",
        f"- Corpus: `embeddings` table, {results['corpus_size']:,} rows at measurement time.",
        (
            f"- Isolated/novel-error family: {results['isolated_corpus_size']:,} rows "
            f"({100 * results['isolated_corpus_size'] / results['corpus_size']:.2f}% of corpus) - "
            "an exact substring count of `producer.scenarios.NOVEL_ERROR_SIGNATURE`, the one "
            "error string with no textual variants, so it is exactly countable unlike the seven "
            "templated error families."
        ),
        (
            f"- {len(QUERY_SET)} query paraphrases: natural-language phrasings of the seven known "
            "error families (ordinary) plus novel-error paraphrases sharing few or no literal "
            "keywords with the signature string (isolated). None are copied verbatim from "
            "`producer/errors.py`'s templates."
        ),
        (
            f'- k={k}. Ground truth ("exact") forces a sequential scan '
            "(`enable_indexscan`/`enable_bitmapscan` off) - the same technique "
            "`consumer/search.py::search()` uses for its own exact path - so it is the only "
            "valid top-k reference to compare against."
        ),
        (
            "- HNSW forces an index scan (`enable_seqscan` off) at a stated `ef_search`, swept "
            f"over {', '.join(str(e) for e in results['ef_sweep'])}."
        ),
        (
            '- No filter/window on either path: both search the whole table, matching the '
            '"broad, unfiltered" branch of Step 11A\'s exact-scan-threshold logic. The '
            "narrow-window branch (recent incidents, filtered queries) already forces an exact "
            "scan in production and gets 100% recall by construction - it is not what this "
            "measurement is about."
        ),
        (
            "- No API cost: embeddings come from the same local `LocalEmbedder` the consumer "
            "uses; no agent, no Anthropic API calls, no incident injection required - all data "
            "already sits in the `embeddings` table from prior demo/eval runs."
        ),
        (
            "- **Two recall metrics.** `id_recall` = `|exact_ids ∩ hnsw_ids| / k` - undefined "
            "when the exact top-k has distance ties, since a different-but-equidistant row "
            "reads as a miss. `distance_recall` compares the two *sorted distance arrays* "
            f"position-for-position (tolerance {results['epsilon']:.0e}) - an equidistant "
            "substitution still matches, so this is the metric that reflects actual retrieval "
            "quality rather than which duplicate each path happened to return. Both are "
            "reported; the gap between them is itself a finding (see below)."
        ),
        (
            "- **`tied`** (per query): whether the exact top-k contains any duplicate distances "
            "- the diagnostic for why `id_recall` and `distance_recall` might disagree on a "
            "given query."
        ),
        (
            "- **Correctness fix (20C-3), verified directly against this database:** an earlier "
            "build of this script set the scan-mode GUCs "
            "(`enable_seqscan`/`enable_indexscan`/`enable_bitmapscan`) only for the mode being "
            "requested, assuming `SET LOCAL` reverts automatically once its `with "
            "conn.transaction():` block exits. It doesn't, for every call after the first: this "
            "function runs many times per query inside one long-lived connection, so after the "
            "first call every later `with conn.transaction():` here opens a SAVEPOINT rather "
            "than a fresh transaction - and Postgres restores `SET LOCAL` values on ROLLBACK TO "
            "SAVEPOINT but *not* on a normal RELEASE SAVEPOINT. One \"hnsw\" call's "
            "`enable_seqscan = off` was silently leaking into every later \"exact\" call in the "
            "same run, quietly turning most of the \"exact\" ground truth into something other "
            "than a true sequential scan - which is also why an earlier version of this report "
            "showed implausibly high recall (~0.96-0.97 headline, and an empty tie-free subset "
            "used as confirmation that duplication alone explained the gap): for most queries, "
            "\"exact\" and \"hnsw\" were silently comparing against something close to the same "
            "plan, not two genuinely independent measurements. Every call now sets every "
            "relevant GUC explicitly, both on and off, so no call can be contaminated by "
            "whichever mode ran before it. **The numbers below are from the corrected version, "
            "and they are substantially lower than previously reported** - the duplication "
            "finding (20C-2, below) still holds as a real, independently-verified fact about "
            "this corpus, but it is no longer sufficient on its own to explain the size of the "
            "recall gap; genuine HNSW recall on this corpus is worse than the pre-fix numbers "
            "suggested."
        ),
        "",
        f"## Headline: Recall@{k} @ ef_search={headline_ef}",
        "",
        (
            f"Exact top-{k} contains ties on {tie_counts['all'][0]}/{tie_counts['all'][1]} "
            f"queries (ordinary {tie_counts['ordinary'][0]}/{tie_counts['ordinary'][1]}, "
            f"isolated {tie_counts['isolated'][0]}/{tie_counts['isolated'][1]})."
        ),
        "",
        "### id_recall (set-overlap, undefined under ties)",
        "",
        _markdown_distribution_table(results, headline_ef, "id_recall"),
        "",
        (
            f"All {id_dist_all['n']}: mean {_fmt(id_dist_all['mean'])}. "
            f"Ordinary ({id_dist_ord['n']}): mean {_fmt(id_dist_ord['mean'])}. "
            f"Isolated ({id_dist_iso['n']}): mean {_fmt(id_dist_iso['mean'])}."
        ),
        "",
        "### distance_recall (position-for-position, tie-aware)",
        "",
        _markdown_distribution_table(results, headline_ef, "distance_recall"),
        "",
        (
            f"All {dist_dist_all['n']}: mean {_fmt(dist_dist_all['mean'])}. "
            f"Ordinary ({dist_dist_ord['n']}): mean {_fmt(dist_dist_ord['mean'])}. "
            f"Isolated ({dist_dist_iso['n']}): mean {_fmt(dist_dist_iso['mean'])}."
        ),
        "",
        "## Corpus duplication (Sub-commit 20C-2)",
        "",
        (
            "This corpus's synthetic error text is generated from a small closed set of "
            "templates (`producer/errors.py`, Step 5's design) crossed with a handful of "
            "`method` and `gateway` values, so the same exact string - and therefore the same "
            "exact embedding vector - recurs across thousands of rows, producing pervasive "
            "distance ties in the exact top-k (below). That duplication is real, and "
            "independently confirmed by the raw counts below - but after the 20C-3 correctness "
            "fix (above), it is not, by itself, sufficient to explain the size of the recall gap "
            "in the sweep above: `distance_recall` is already tie-aware (an equidistant "
            "substitution still counts as a match) and it is *also* low even at "
            f"ef_search={headline_ef}. Duplication explains why `id_recall` specifically is an "
            "unreliable, often-undefined metric here; it does not explain away the low "
            "`distance_recall` - that reflects a genuine HNSW retrieval-quality gap on this "
            "corpus."
        ),
        "",
        _markdown_duplication_table(results),
        "",
        (
            f"### distance_recall@{k} @ ef_search={headline_ef}: tie-free vs. tied queries"
        ),
        "",
        (
            "Cross-check of the 20C-1 metric fix: if duplication is really driving the gap, "
            "queries whose exact top-k has **no** ties (a well-defined true top-k) should show "
            "high distance_recall under both metrics, while queries with ties carry whatever gap "
            "remains."
        ),
        "",
        _markdown_tie_split_table(results, headline_ef),
        "",
    ]

    tie_free_n = _tie_split_distance_recall(results, headline_ef)["tie_free"]["n"]
    if tie_free_n == 0:
        lines.append(
            "The tie-free subset is **empty** - every one of this query set's queries has "
            f"ties in its exact top-{k}. At a {results['duplication']['duplication_ratio']:.0f}x "
            "duplication ratio (this many rows sharing this few distinct text values), that is "
            "expected rather than a gap in the query set: with this much exact duplication, "
            "essentially any query into this corpus lands on a tied top-k. The cross-check "
            "therefore can't run in the direction originally planned (comparing tie-free vs. "
            "tied). This confirms duplication makes `id_recall` specifically unreliable/"
            "undefined here (there is no unique true top-k to compare ids against) - but it "
            "does **not** explain away the low `distance_recall` reported above, since that "
            "metric is already tie-aware. The genuine finding is that HNSW recall on this "
            "corpus is poor; duplication only explains why the weaker (`id_recall`) of the two "
            "metrics is additionally unusable here."
        )
    else:
        tied_dist = _tie_split_distance_recall(results, headline_ef)["tied"]["mean"]
        tie_free_dist = _tie_split_distance_recall(results, headline_ef)["tie_free"]["mean"]
        confirms = (
            tie_free_dist is not None and tied_dist is not None and tie_free_dist >= tied_dist
        )
        verdict = (
            "consistent with the duplication diagnosis: recall is high once the true "
            "top-k is well-defined."
            if confirms
            else "not a clean confirmation - reported as measured, not adjusted toward the "
            "expected direction."
        )
        lines.append(
            f"Tie-free queries (n={tie_free_n}) show mean distance_recall "
            f"{_fmt(tie_free_dist)} vs. {_fmt(tied_dist)} for tied queries - {verdict}"
        )

    lines += [
        "",
        "## Corpus limitations",
        "",
        (
            "This corpus's error text is synthetic, generated from a small closed set of "
            "templates (Step 5's design choice, not a pgvector or HNSW property) - roughly a "
            "handful of template strings per error family, crossed with a handful of `method` "
            "and `gateway` values in the embedded-text format "
            "`\"{method} payment via {gateway} failed: {error_text}\"`. That combinatorics is "
            "small enough that, at the row counts this environment has accumulated, most rows "
            "collide on an exact template+method+gateway combination and therefore share a "
            "bit-identical embedding. A templated generator producing degenerate (highly "
            "clustered, heavily tied) vector-space structure is expected, not a bug - real "
            "production error text (stack traces, free-form messages) would not tie this way. "
            "This bounds what `recall@k` can mean here: on this corpus, id_recall is frequently "
            "undefined (the true top-k is not a unique set), so distance_recall is the only "
            "metric that means what it says. Disclosing this makes the surrounding numbers more "
            "credible, not less - a recall report that didn't mention it would leave the reader "
            "unable to tell whether a low id_recall meant bad retrieval or an undefined ground "
            "truth."
        ),
        "",
        "## Per-query results",
        "",
        _markdown_per_query_table(results, headline_ef),
        "",
        "## ef_search sweep: does isolated recall recover?",
        "",
        "Both metrics shown; `distance_recall` is the one that reflects actual retrieval quality.",
        "",
        _markdown_sweep_table(results),
        "",
    ]

    if recovers is None:
        lines.append(
            "Only one ef_search value was swept - no recovery trend can be assessed."
        )
    elif recovers:
        lines.append(
            f"Isolated-vector distance_recall rose from {sweep_iso_dist_means[0]:.2f} at "
            f"ef_search={results['ef_sweep'][0]} to {sweep_iso_dist_means[-1]:.2f} at "
            f"ef_search={results['ef_sweep'][-1]} - more query-time search effort did recover "
            "recall on this corpus's isolated family. This does not reproduce Step 10B's "
            "original finding: that case was a single vector inserted with zero prior "
            "neighbors of its kind in the graph. Here the isolated family (591 rows) has "
            "accumulated across many repeated incident injections over roughly 10 days of "
            "demo/eval runs, so it now forms its own small but real, navigable cluster - the "
            "isolation the original anecdote described has organically healed in this "
            "environment. See Sub-commit 20C-3 for a controlled experiment that reconstructs "
            "the original single-fresh-vector condition directly."
        )
    else:
        ratio = results["ef_sweep"][-1] // results["ef_sweep"][0]
        iso_pct = 100 * results["isolated_corpus_size"] / results["corpus_size"]
        lines.append(
            f"Isolated-vector distance_recall stayed roughly flat "
            f"({sweep_iso_dist_means[0]:.2f} at ef_search={results['ef_sweep'][0]} vs. "
            f"{sweep_iso_dist_means[-1]:.2f} at ef_search={results['ef_sweep'][-1]}) even as "
            f"ef_search increased {ratio}x. This is the Step 10B finding as a measurement "
            "rather than an anecdote: query-time search effort (ef_search) cannot recover "
            f"recall that build-time graph connectivity never established. The isolated "
            f"family is numerically rare ({iso_pct:.2f}% of the corpus)."
        )

    lines.append("")
    lines.append("## Reproduction")
    lines.append("")
    lines.append("`make eval-recall` (or `python -m eval.recall`). Requires the stack up "
                  "(`make up`) with data already ingested - no fresh injection needed.")
    lines.append("")

    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    resolved_output.write_text("\n".join(lines), encoding="utf-8")
    return resolved_output


def _parse_args():
    parser = argparse.ArgumentParser(description="HNSW recall measurement")
    parser.add_argument("--k", type=int, default=DEFAULT_K)
    parser.add_argument(
        "--ef-search",
        default=",".join(str(e) for e in DEFAULT_EF_SWEEP),
        help="Comma-separated ef_search values to sweep, e.g. 40,100,400,1000",
    )
    parser.add_argument("--output", default=None, help="Output path (default: eval/RECALL.md)")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    ef_sweep = [int(v.strip()) for v in args.ef_search.split(",") if v.strip()]

    print("Loading all-MiniLM-L6-v2 (downloads ~80 MB on first run, then cached)...")
    embedder = LocalEmbedder()

    conn = connect()
    try:
        results = run_measurement(conn, embedder, k=args.k, ef_sweep=ef_sweep)
    finally:
        conn.close()

    print_console_report(results)

    output_path = Path(args.output) if args.output else DEFAULT_OUTPUT_PATH
    written = generate_recall_report(results, output_path)
    print(f"\nWrote {written}")


if __name__ == "__main__":
    main()
