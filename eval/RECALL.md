# HNSW Recall Measurement

Commit `cadaf73`.

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

## Headline: Recall@10 @ ef_search=1000

Exact top-10 contains ties on 35/35 queries (ordinary 28/28, isolated 7/7).

### id_recall (set-overlap, undefined under ties)

| group | n | mean | min | p25 | median | p75 | max |
|---|---|---|---|---|---|---|---|
| all | 35 | 0.97 | 0.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| ordinary | 28 | 0.96 | 0.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| isolated | 7 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |

All 35: mean 0.97. Ordinary (28): mean 0.96. Isolated (7): mean 1.00.

### distance_recall (position-for-position, tie-aware)

| group | n | mean | min | p25 | median | p75 | max |
|---|---|---|---|---|---|---|---|
| all | 35 | 0.97 | 0.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| ordinary | 28 | 0.96 | 0.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| isolated | 7 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |

All 35: mean 0.97. Ordinary (28): mean 0.96. Isolated (7): mean 1.00.

## Corpus duplication (Sub-commit 20C-2)

Finding 2 from the sweep above (ordinary recall ~0.20 at low ef_search) traces to bit-identical `embedded_text` values at scale, not to HNSW quality. This corpus's synthetic error text is generated from a small closed set of templates (`producer/errors.py`, Step 5's design) crossed with a handful of `method` and `gateway` values, so the same exact string - and therefore the same exact embedding vector - recurs across thousands of rows.

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
| tied | 35 | 0.97 | 0.00 | 1.00 | 1.00 | 1.00 | 1.00 |

The tie-free subset is **empty** - every one of this query set's queries has ties in its exact top-10. At a 339x duplication ratio (this many rows sharing this few distinct text values), that is expected rather than a gap in the query set: with this much exact duplication, essentially any query into this corpus lands on a tied top-k. The cross-check therefore can't run in the direction originally planned (comparing tie-free vs. tied), but the reason it can't run *is* the finding - it is direct, mechanical confirmation that duplication, not HNSW quality, is what makes id_recall unreliable on this corpus.

## Corpus limitations

This corpus's error text is synthetic, generated from a small closed set of templates (Step 5's design choice, not a pgvector or HNSW property) - roughly a handful of template strings per error family, crossed with a handful of `method` and `gateway` values in the embedded-text format `"{method} payment via {gateway} failed: {error_text}"`. That combinatorics is small enough that, at the row counts this environment has accumulated, most rows collide on an exact template+method+gateway combination and therefore share a bit-identical embedding. A templated generator producing degenerate (highly clustered, heavily tied) vector-space structure is expected, not a bug - real production error text (stack traces, free-form messages) would not tie this way. This bounds what `recall@k` can mean here: on this corpus, id_recall is frequently undefined (the true top-k is not a unique set), so distance_recall is the only metric that means what it says. Disclosing this makes the surrounding numbers more credible, not less - a recall report that didn't mention it would leave the reader unable to tell whether a low id_recall meant bad retrieval or an undefined ground truth.

## Per-query results

| group | family | id_recall | distance_recall | tied | query |
|---|---|---|---|---|---|
| ordinary | insufficient_funds | 0.00 | 0.00 | True | the account didn't have enough money for the charge |
| ordinary | insufficient_funds | 1.00 | 1.00 | True | payment declined due to insufficient funds |
| ordinary | insufficient_funds | 1.00 | 1.00 | True | not enough balance available to cover the transaction |
| ordinary | insufficient_funds | 1.00 | 1.00 | True | card was declined because of low funds |
| ordinary | do_not_honor | 1.00 | 1.00 | True | the issuing bank declined the transaction, no reason given |
| ordinary | do_not_honor | 1.00 | 1.00 | True | do not honor response from the card issuer |
| ordinary | do_not_honor | 1.00 | 1.00 | True | bank refused to authorize the charge |
| ordinary | do_not_honor | 1.00 | 1.00 | True | issuer declined the payment without stating why |
| ordinary | expired_card | 1.00 | 1.00 | True | the card on file has expired |
| ordinary | expired_card | 1.00 | 1.00 | True | payment failed because the card's expiration date has passed |
| ordinary | expired_card | 1.00 | 1.00 | True | customer's card is no longer valid, past its expiry |
| ordinary | expired_card | 1.00 | 1.00 | True | declined due to an expired card |
| ordinary | invalid_cvv | 1.00 | 1.00 | True | the security code entered didn't match |
| ordinary | invalid_cvv | 1.00 | 1.00 | True | CVV verification failed on the card |
| ordinary | invalid_cvv | 1.00 | 1.00 | True | wrong card verification code provided |
| ordinary | invalid_cvv | 1.00 | 1.00 | True | payment rejected because the CVC was incorrect |
| ordinary | gateway_timeout | 1.00 | 1.00 | True | the payment gateway took too long to respond |
| ordinary | gateway_timeout | 1.00 | 1.00 | True | upstream processor timed out during the transaction |
| ordinary | gateway_timeout | 1.00 | 1.00 | True | connection to the payment gateway timed out |
| ordinary | gateway_timeout | 1.00 | 1.00 | True | gateway did not respond within the expected time |
| ordinary | network_error | 1.00 | 1.00 | True | network connection was reset before completing the request |
| ordinary | network_error | 1.00 | 1.00 | True | TLS handshake failed while connecting to the gateway |
| ordinary | network_error | 1.00 | 1.00 | True | the payment processor was unreachable over the network |
| ordinary | network_error | 1.00 | 1.00 | True | connection reset error while reaching the gateway |
| ordinary | fraud_suspected | 1.00 | 1.00 | True | the transaction was flagged as potentially fraudulent |
| ordinary | fraud_suspected | 1.00 | 1.00 | True | risk engine blocked the payment as suspicious |
| ordinary | fraud_suspected | 1.00 | 1.00 | True | charge was declined due to suspected fraud |
| ordinary | fraud_suspected | 1.00 | 1.00 | True | a high risk score triggered a fraud block |
| isolated | novel_error | 1.00 | 1.00 | True | currency mismatch: expected US dollars but got Japanese yen |
| isolated | novel_error | 1.00 | 1.00 | True | a fallback was denied over a locale override tied to currency |
| isolated | novel_error | 1.00 | 1.00 | True | merchant config v2 rejected the charge over a currency issue |
| isolated | novel_error | 1.00 | 1.00 | True | the payment failed because the currency didn't match the charge |
| isolated | novel_error | 1.00 | 1.00 | True | locale settings blocked a fallback over a mismatched currency |
| isolated | novel_error | 1.00 | 1.00 | True | a conflict between USD and JPY caused the charge to fail |
| isolated | novel_error | 1.00 | 1.00 | True | a merchant config error, mismatched currency, blocked fallback |

## ef_search sweep: does isolated recall recover?

Both metrics shown; `distance_recall` is the one that reflects actual retrieval quality.

| ef_search | ordinary id_recall | ordinary distance_recall | isolated id_recall | isolated distance_recall |
|---|---|---|---|---|
| 40 | 0.19 | 0.82 | 0.20 | 0.86 |
| 100 | 0.58 | 0.86 | 0.57 | 0.86 |
| 400 | 0.69 | 0.89 | 0.61 | 0.86 |
| 1000 | 0.96 | 0.96 | 1.00 | 1.00 |

Isolated-vector distance_recall rose from 0.86 at ef_search=40 to 1.00 at ef_search=1000 - more query-time search effort did recover recall on this corpus's isolated family. This does not reproduce Step 10B's original finding: that case was a single vector inserted with zero prior neighbors of its kind in the graph. Here the isolated family (591 rows) has accumulated across many repeated incident injections over roughly 10 days of demo/eval runs, so it now forms its own small but real, navigable cluster - the isolation the original anecdote described has organically healed in this environment. See Sub-commit 20C-3 for a controlled experiment that reconstructs the original single-fresh-vector condition directly.

## Reproduction

`make eval-recall` (or `python -m eval.recall`). Requires the stack up (`make up`) with data already ingested - no fresh injection needed.
