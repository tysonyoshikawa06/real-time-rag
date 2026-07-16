# test-writer memory index

- [DB/MCP test patterns](db-test-patterns.md) — rollback-isolated Postgres seeding under live stream data, exploding-conn validation checks, fastmcp in-memory client without pytest-asyncio, FK-linked table seeding (embeddings+register_vector), deterministic hashed fake Embedder, json.dumps as a serializability check
- [JSON schema validation](json-schema-validation.md) — static data file schema testing: structure, required fields, type checking, constraints, uniqueness, placeholder detection, re-serialization
- [Eval report structural guarantee](eval-report-patterns.md) — session-scoped hash isolation proof for filesystem output, synthetic graded/run JSON factories, assertion index specificity, run_metadata presence/absence testing, datetime.UTC compatibility
