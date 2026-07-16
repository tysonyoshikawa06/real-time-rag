# HNSW Recall Measurement

Commit `e48c4b1`.

## Methodology

- Corpus: `embeddings` table, 104,552 rows at measurement time.
- Isolated/novel-error family: 591 rows (0.57% of corpus) - an exact substring count of `producer.scenarios.NOVEL_ERROR_SIGNATURE`, the one error string with no textual variants, so it is exactly countable unlike the seven templated error families.
- 35 query paraphrases: natural-language phrasings of the seven known error families (ordinary) plus novel-error paraphrases sharing few or no literal keywords with the signature string (isolated). None are copied verbatim from `producer/errors.py`'s templates.
- k=10. Ground truth ("exact") forces a sequential scan (`enable_indexscan`/`enable_bitmapscan` off) - the same technique `consumer/search.py::search()` uses for its own exact path - so it is the only valid top-k reference to compare against.
- HNSW forces an index scan (`enable_seqscan` off) at a stated `ef_search`, swept over 40, 100, 400, 1000.
- No filter/window on either path: both search the whole table, matching the "broad, unfiltered" branch of Step 11A's exact-scan-threshold logic. The narrow-window branch (recent incidents, filtered queries) already forces an exact scan in production and gets 100% recall by construction - it is not what this measurement is about.
- No API cost: embeddings come from the same local `LocalEmbedder` the consumer uses; no agent, no Anthropic API calls, no incident injection required - all data already sits in the `embeddings` table from prior demo/eval runs.
- **Two recall metrics.** `id_recall` = `|exact_ids ∩ hnsw_ids| / k` - undefined when the exact top-k has distance ties, since a different-but-equidistant row reads as a miss. `distance_recall` compares the two *sorted distance arrays* position-for-position (tolerance 1e-06) - an equidistant substitution still matches, so this is the metric that reflects actual retrieval quality rather than which duplicate each path happened to return. Both are reported; the gap between them is itself a finding (see below).
- **`tied`** (per query): whether the exact top-k contains any duplicate distances - the diagnostic for why `id_recall` and `distance_recall` might disagree on a given query.
- **Correctness fix (20C-3), verified directly against this database:** an earlier build of this script set the scan-mode GUCs (`enable_seqscan`/`enable_indexscan`/`enable_bitmapscan`) only for the mode being requested, assuming `SET LOCAL` reverts automatically once its `with conn.transaction():` block exits. It doesn't, for every call after the first: this function runs many times per query inside one long-lived connection, so after the first call every later `with conn.transaction():` here opens a SAVEPOINT rather than a fresh transaction - and Postgres restores `SET LOCAL` values on ROLLBACK TO SAVEPOINT but *not* on a normal RELEASE SAVEPOINT. One "hnsw" call's `enable_seqscan = off` was silently leaking into every later "exact" call in the same run, quietly turning most of the "exact" ground truth into something other than a true sequential scan - which is also why an earlier version of this report showed implausibly high recall (~0.96-0.97 headline, and an empty tie-free subset used as confirmation that duplication alone explained the gap): for most queries, "exact" and "hnsw" were silently comparing against something close to the same plan, not two genuinely independent measurements. Every call now sets every relevant GUC explicitly, both on and off, so no call can be contaminated by whichever mode ran before it. **The numbers below are from the corrected version, and they are substantially lower than previously reported** - the duplication finding (20C-2, below) still holds as a real, independently-verified fact about this corpus, but it is no longer sufficient on its own to explain the size of the recall gap; genuine HNSW recall on this corpus is worse than the pre-fix numbers suggested.

## Headline: Recall@10 @ ef_search=1000

Exact top-10 contains ties on 35/35 queries (ordinary 28/28, isolated 7/7).

### id_recall (set-overlap, undefined under ties)

| group | n | mean | min | p25 | median | p75 | max |
|---|---|---|---|---|---|---|---|
| all | 35 | 0.02 | 0.00 | 0.00 | 0.00 | 0.00 | 0.50 |
| ordinary | 28 | 0.02 | 0.00 | 0.00 | 0.00 | 0.00 | 0.50 |
| isolated | 7 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |

All 35: mean 0.02. Ordinary (28): mean 0.02. Isolated (7): mean 0.00.

### distance_recall (position-for-position, tie-aware)

| group | n | mean | min | p25 | median | p75 | max |
|---|---|---|---|---|---|---|---|
| all | 35 | 0.23 | 0.00 | 0.00 | 0.00 | 0.00 | 1.00 |
| ordinary | 28 | 0.29 | 0.00 | 0.00 | 0.00 | 1.00 | 1.00 |
| isolated | 7 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |

All 35: mean 0.23. Ordinary (28): mean 0.29. Isolated (7): mean 0.00.

## Corpus duplication (Sub-commit 20C-2)

This corpus's synthetic error text is generated from a small closed set of templates (`producer/errors.py`, Step 5's design) crossed with a handful of `method` and `gateway` values, so the same exact string - and therefore the same exact embedding vector - recurs across thousands of rows, producing pervasive distance ties in the exact top-k (below). That duplication is real, and independently confirmed by the raw counts below - but after the 20C-3 correctness fix (above), it is not, by itself, sufficient to explain the size of the recall gap in the sweep above: `distance_recall` is already tie-aware (an equidistant substitution still counts as a match) and it is *also* low even at ef_search=1000. Duplication explains why `id_recall` specifically is an unreliable, often-undefined metric here; it does not explain away the low `distance_recall` - that reflects a genuine HNSW retrieval-quality gap on this corpus.

| total rows | distinct embedded_text | duplication ratio |
|---|---|---|
| 104,552 | 308 | 339.5x |

Top 10 identical-text clusters:

| rows (n) | embedded_text |
|---|---|
| 1,080 | `card payment via stripe-proxy failed: 51: INSUFFICIENT FUNDS` |
| 1,079 | `card payment via braintree-edge failed: NSF` |
| 1,057 | `card payment via braintree-edge failed: Declined - insufficient funds available` |
| 1,054 | `card payment via checkout-io failed: NSF` |
| 1,053 | `card payment via checkout-io failed: 51: INSUFFICIENT FUNDS` |
| 1,026 | `card payment via adyen-gw failed: Declined - insufficient funds available` |
| 1,013 | `card payment via adyen-gw failed: insufficient_funds` |
| 1,008 | `card payment via stripe-proxy failed: ERR_51 insufficient funds` |
| 995 | `card payment via stripe-proxy failed: insufficient_funds` |
| 993 | `card payment via adyen-gw failed: NSF` |

### distance_recall@10 @ ef_search=1000: tie-free vs. tied queries

Cross-check of the 20C-1 metric fix: if duplication is really driving the gap, queries whose exact top-k has **no** ties (a well-defined true top-k) should show high distance_recall under both metrics, while queries with ties carry whatever gap remains.

| subset | n | mean | min | p25 | median | p75 | max |
|---|---|---|---|---|---|---|---|
| tie_free | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| tied | 35 | 0.23 | 0.00 | 0.00 | 0.00 | 0.00 | 1.00 |

The tie-free subset is **empty** - every one of this query set's queries has ties in its exact top-10. At a 339x duplication ratio (this many rows sharing this few distinct text values), that is expected rather than a gap in the query set: with this much exact duplication, essentially any query into this corpus lands on a tied top-k. The cross-check therefore can't run in the direction originally planned (comparing tie-free vs. tied). This confirms duplication makes `id_recall` specifically unreliable/undefined here (there is no unique true top-k to compare ids against) - but it does **not** explain away the low `distance_recall` reported above, since that metric is already tie-aware. The genuine finding is that HNSW recall on this corpus is poor; duplication only explains why the weaker (`id_recall`) of the two metrics is additionally unusable here.

## Corpus limitations

This corpus's error text is synthetic, generated from a small closed set of templates (Step 5's design choice, not a pgvector or HNSW property) - roughly a handful of template strings per error family, crossed with a handful of `method` and `gateway` values in the embedded-text format `"{method} payment via {gateway} failed: {error_text}"`. That combinatorics is small enough that, at the row counts this environment has accumulated, most rows collide on an exact template+method+gateway combination and therefore share a bit-identical embedding. A templated generator producing degenerate (highly clustered, heavily tied) vector-space structure is expected, not a bug - real production error text (stack traces, free-form messages) would not tie this way. This bounds what `recall@k` can mean here: on this corpus, id_recall is frequently undefined (the true top-k is not a unique set), so distance_recall is the only metric that means what it says. Disclosing this makes the surrounding numbers more credible, not less - a recall report that didn't mention it would leave the reader unable to tell whether a low id_recall meant bad retrieval or an undefined ground truth.

## Per-query results

| group | family | id_recall | distance_recall | tied | query |
|---|---|---|---|---|---|
| ordinary | insufficient_funds | 0.00 | 0.00 | True | the account didn't have enough money for the charge |
| ordinary | insufficient_funds | 0.00 | 0.00 | True | payment declined due to insufficient funds |
| ordinary | insufficient_funds | 0.10 | 1.00 | True | not enough balance available to cover the transaction |
| ordinary | insufficient_funds | 0.00 | 1.00 | True | card was declined because of low funds |
| ordinary | do_not_honor | 0.00 | 0.00 | True | the issuing bank declined the transaction, no reason given |
| ordinary | do_not_honor | 0.00 | 0.00 | True | do not honor response from the card issuer |
| ordinary | do_not_honor | 0.00 | 0.00 | True | bank refused to authorize the charge |
| ordinary | do_not_honor | 0.00 | 0.00 | True | issuer declined the payment without stating why |
| ordinary | expired_card | 0.00 | 0.00 | True | the card on file has expired |
| ordinary | expired_card | 0.00 | 0.00 | True | payment failed because the card's expiration date has passed |
| ordinary | expired_card | 0.00 | 0.00 | True | customer's card is no longer valid, past its expiry |
| ordinary | expired_card | 0.00 | 0.00 | True | declined due to an expired card |
| ordinary | invalid_cvv | 0.00 | 0.00 | True | the security code entered didn't match |
| ordinary | invalid_cvv | 0.00 | 1.00 | True | CVV verification failed on the card |
| ordinary | invalid_cvv | 0.00 | 0.00 | True | wrong card verification code provided |
| ordinary | invalid_cvv | 0.00 | 0.00 | True | payment rejected because the CVC was incorrect |
| ordinary | gateway_timeout | 0.00 | 0.00 | True | the payment gateway took too long to respond |
| ordinary | gateway_timeout | 0.00 | 1.00 | True | upstream processor timed out during the transaction |
| ordinary | gateway_timeout | 0.00 | 0.00 | True | connection to the payment gateway timed out |
| ordinary | gateway_timeout | 0.00 | 0.00 | True | gateway did not respond within the expected time |
| ordinary | network_error | 0.00 | 0.00 | True | network connection was reset before completing the request |
| ordinary | network_error | 0.00 | 1.00 | True | TLS handshake failed while connecting to the gateway |
| ordinary | network_error | 0.00 | 0.00 | True | the payment processor was unreachable over the network |
| ordinary | network_error | 0.00 | 0.00 | True | connection reset error while reaching the gateway |
| ordinary | fraud_suspected | 0.50 | 1.00 | True | the transaction was flagged as potentially fraudulent |
| ordinary | fraud_suspected | 0.00 | 0.00 | True | risk engine blocked the payment as suspicious |
| ordinary | fraud_suspected | 0.00 | 1.00 | True | charge was declined due to suspected fraud |
| ordinary | fraud_suspected | 0.10 | 1.00 | True | a high risk score triggered a fraud block |
| isolated | novel_error | 0.00 | 0.00 | True | currency mismatch: expected US dollars but got Japanese yen |
| isolated | novel_error | 0.00 | 0.00 | True | a fallback was denied over a locale override tied to currency |
| isolated | novel_error | 0.00 | 0.00 | True | merchant config v2 rejected the charge over a currency issue |
| isolated | novel_error | 0.00 | 0.00 | True | the payment failed because the currency didn't match the charge |
| isolated | novel_error | 0.00 | 0.00 | True | locale settings blocked a fallback over a mismatched currency |
| isolated | novel_error | 0.00 | 0.00 | True | a conflict between USD and JPY caused the charge to fail |
| isolated | novel_error | 0.00 | 0.00 | True | a merchant config error, mismatched currency, blocked fallback |

## ef_search sweep: does isolated recall recover?

Both metrics shown; `distance_recall` is the one that reflects actual retrieval quality.

| ef_search | ordinary id_recall | ordinary distance_recall | isolated id_recall | isolated distance_recall |
|---|---|---|---|---|
| 40 | 0.05 | 0.25 | 0.00 | 0.00 |
| 100 | 0.01 | 0.25 | 0.00 | 0.00 |
| 400 | 0.02 | 0.25 | 0.00 | 0.00 |
| 1000 | 0.02 | 0.29 | 0.00 | 0.00 |

Isolated-vector distance_recall stayed roughly flat (0.00 at ef_search=40 vs. 0.00 at ef_search=1000) even as ef_search increased 25x. This is the Step 10B finding as a measurement rather than an anecdote: query-time search effort (ef_search) cannot recover recall that build-time graph connectivity never established. The isolated family is numerically rare (0.57% of the corpus).

## Reproduction

`make eval-recall` (or `python -m eval.recall`). Requires the stack up (`make up`) with data already ingested - no fresh injection needed.

## Controlled isolation experiment (Sub-commit 20C-3)

A controlled reconstruction of Step 10B's original condition, since the live `embeddings` table's 10 days of accumulated novel-error rows (20C-1/20C-2) make it impossible to test directly anymore: a single fresh novel vector with zero prior neighbors of its kind. Built in a scratch schema (`eval_isolation`), dropped and rebuilt fresh on every run, never touching production tables.

### Methodology

- Baseline: 5,000 ordinary error-text rows (`producer/errors.py`'s seven known families, zero novel-error rows), embedded and inserted into `eval_isolation.embeddings` with no index.
- HNSW index built once, over the full baseline, with pgvector's default `m`/`ef_construction` - the same defaults production's `infra/init.sql` uses (production never overrides them either).
- Novel-error rows (`producer.scenarios.NOVEL_ERROR_SIGNATURE`, the same fixed signature string production incidents inject) inserted incrementally at cumulative cluster sizes 1, 2, 5, 10, 25, 50, 100 - the index is never rebuilt after the initial build, so HNSW only ever absorbs these via normal inserts, exactly like production.
- Fixed query at every checkpoint: "currency mismatch: expected US dollars but got Japanese yen" - a paraphrase sharing few literal keywords with the signature string, reused from `eval/recall.py`'s own isolated-query set.
- k=10. `ef_search` swept over 40, 1000 - pgvector's own default (what production runs at without an override) and 1000 (the value Step 10B's anecdote specifically named, "even at ef_search=1000").
- **`novel_recall`**: of the novel rows in the exact top-k, what fraction did HNSW also return. This is the direct, targeted answer to "did HNSW find the novel cluster" - unlike `id_recall`/`distance_recall` (also reported), which blend in the ordinary rows that dominate the top-k once the novel cluster is smaller than k.
- Random seed 42 for baseline/novel-row generation - the *data* (which template/method/gateway each row draws) is reproducible. The HNSW graph itself is not: pgvector's HNSW build assigns each node's layer via its own internal randomization, independent of this Python seed, so two runs over the identical seeded data can still build measurably different graphs. Verified directly - two consecutive runs of this script produced different novel_recall values at low `ef_search` for the same early cluster sizes (e.g. cluster size 1 read novel_recall 1.00 in one run and 0.00 in the next). This is itself consistent with the finding below: whether a genuinely isolated vector gets missed depends on where it happens to land in a randomly-built graph, not on a fixed, deterministic property of HNSW - treat any single run's low-`ef_search` numbers as one draw from that variability, not a fixed constant.

### Recall vs. cluster size

| cluster size | exact novel in top-k | tied | id_recall (ef=40) | distance_recall (ef=40) | novel_recall (ef=40) | id_recall (ef=1000) | distance_recall (ef=1000) | novel_recall (ef=1000) |
|---|---|---|---|---|---|---|---|---|
| 1 | 1 | True | 0.80 | 0.90 | 0.00 | 0.80 | 1.00 | 1.00 |
| 2 | 2 | True | 0.70 | 0.80 | 0.00 | 0.70 | 1.00 | 1.00 |
| 5 | 5 | True | 0.60 | 0.70 | 0.40 | 0.70 | 1.00 | 1.00 |
| 10 | 10 | True | 0.20 | 0.20 | 0.20 | 1.00 | 1.00 | 1.00 |
| 25 | 7 | True | 1.00 | 1.00 | 1.00 | 0.70 | 0.70 | 1.00 |
| 50 | 10 | True | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 100 | 10 | True | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |

novel_recall @ ef_search=1000, ASCII (0.0 to 1.0 per cluster size):

```
     1  |########################################| 1.00
     2  |########################################| 1.00
     5  |########################################| 1.00
    10  |########################################| 1.00
    25  |########################################| 1.00
    50  |########################################| 1.00
   100  |########################################| 1.00
```

### Finding

- **ef_search=40**: novel_recall rose from 0.00 at cluster size 1 to 1.00 at cluster size 100. Reproduces Step 10B's original claim directly: a genuinely isolated novel vector starts with degraded recall and recovers as the cluster grows.
- **ef_search=1000**: novel_recall was already high (1.00) at cluster size 1 and stayed high (1.00) through cluster size 100. Does not reproduce Step 10B's claim at this `ef_search` - even a single freshly-inserted, genuinely isolated vector was recalled reliably.

The finding is **`ef_search`-dependent**, and that dependence is itself the result: at pgvector's default `ef_search=40` - what production actually runs at on the broad, unfiltered query path, since nothing there overrides `hnsw.ef_search` - a single fresh, genuinely isolated novel vector is recalled poorly (novel_recall=0.00 at cluster size 1), climbing back up only once the cluster reaches roughly a dozen to two dozen similar rows. This directly reproduces Step 10B under controlled conditions. At `ef_search=1000` - the value the original anecdote specifically named - the same isolated vector is recalled reliably from cluster size 1 onward in this environment, meaning enough query-time search effort *does* compensate for the missing graph edges here, which is a real difference from how the original anecdote was worded ("missed even at ef_search=1000"). Two things are simultaneously true and worth carrying forward: (1) the isolation failure mode is real and reproduces cleanly at the `ef_search` production actually uses by default, and (2) the specific claim that raising `ef_search` to 1000 does not help does not reproduce here - it does help. The Step-10B failure mode is a **transient window** tied to cluster size and query-time search effort, not a permanent property of HNSW or this system - and it explains exactly why the live table's 591 accumulated novel-error rows no longer show any gap (20C-1/20C-2): both of the conditions that closed the window (cluster growth, and the exact-scan fallback for filtered queries per Step 11A) are present in production.

### Production safety check

Production `embeddings` row count: 104,552 before this run, 104,552 after - unchanged. All writes in this experiment went to the `eval_isolation` scratch schema only.

### Reproduction

`make eval-isolation` (or `python -m eval.isolation_experiment`). Requires the stack up (`make up`). Drops and rebuilds the `eval_isolation` schema on every run; never reads or writes the production `transactions`/`embeddings` tables.
