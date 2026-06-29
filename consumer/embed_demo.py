"""Similarity demo: embed example error strings and print the cosine-similarity matrix.

Run with:  make embed-demo

What to look for
----------------
"insufficient_funds" and "not sufficient funds available" score high (>= 0.70)
against each other — they describe the same concept with different phrasing.

"NSF" intentionally scores LOW here (~0.15). On its own, "NSF" is ambiguous:
the model has seen it mean "National Science Foundation", "No Such File", and
more. Without context it cannot confidently map "NSF" near "insufficient funds".
This is *exactly* why Step 9B enriches before embedding: by constructing
  "card payment via stripe-proxy failed: NSF"
we place NSF in a payment-failure context that removes the ambiguity.

"Gateway timeout" and "card expired" score low against the insufficient-funds
phrases because they describe completely different failure modes — network
latency vs. stale credentials. Their vectors point in different directions.

Why cosine similarity captures meaning
---------------------------------------
all-MiniLM-L6-v2 was trained to map text with the same meaning to nearby
points in 384-dimensional space, regardless of surface form. "insufficient_funds"
(tokenized as "insufficient" + "funds") and "not sufficient funds available"
end up in the same region because they appear in similar contexts in training
data. Their angle is small, so cos(angle) is close to 1.

Cosine similarity is the cosine of that angle. Since LocalEmbedder normalizes
vectors to unit length (L2 norm = 1), the dot product of any two vectors equals
their cosine similarity directly — no division needed.

This is why Step 12's semantic search finds novel phrasings of known errors
without an exact keyword match.
"""

import numpy as np

from consumer.embedder import LocalEmbedder

EXAMPLES = [
    "insufficient_funds",
    "NSF",
    "not sufficient funds available",
    "Gateway timeout after 30000ms upstream=stripe-proxy-7",
    "card expired",
]


def main() -> None:
    print("Loading all-MiniLM-L6-v2 (downloads ~80 MB on first run, then cached)...\n")
    embedder = LocalEmbedder()

    vecs = np.array(embedder.embed(EXAMPLES))
    # Vectors are L2-normalised, so dot product == cosine similarity.
    sim: np.ndarray = vecs @ vecs.T

    n = len(EXAMPLES)

    print("Cosine-similarity matrix (1.0 = same meaning, 0.0 = unrelated):\n")

    # Header row: "     [0]   [1]   [2]   [3]   [4]"
    print("    " + "".join(f"  [{j}]" for j in range(n)))

    # Data rows: "[i]  0.75  0.79  ..."
    for i in range(n):
        row = "".join(f"  {sim[i, j]:.2f}" for j in range(n))
        print(f"[{i}]{row}")

    # Legend
    print()
    for i, label in enumerate(EXAMPLES):
        print(f"[{i}] {label}")

    print("\nExpected: [0]<->[2] >= 0.70 (same concept); [1]=NSF scores lower (ambiguous alone)")
    print("Cross-group [0-2] vs [3,4] < 0.25  |  enrichment (Step 9B) fixes NSF's low score")


if __name__ == "__main__":
    main()
