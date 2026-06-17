---
name: run-tests
description: The project-specific recipe for running this app's test suite — commands, environment setup, how to run one feature's tests, and how to read results. Use whenever running, debugging, or locating tests, even if not explicitly asked. Preloaded by the test-runner agent and available to the coder for the fix loop.
---

# Test recipe

Project-specific facts about how this app builds and tests. Keep this current —
this is the one file to edit when the test command or setup changes.

## Prerequisites / environment

- Python 3.11+ with a virtual environment: `python -m venv .venv && .venv\Scripts\activate` (Windows) or `source .venv/bin/activate` (Linux/macOS), then `pip install -e ".[dev]"`.
- Docker services must be running before integration tests: `docker compose -f infra/docker-compose.yml up -d` (Kafka KRaft single-node, Postgres + pgvector).
- No `.env.test` file yet — tests use defaults matching the Docker Compose service ports. If one is added later, load it with `pytest-dotenv` or equivalent.
- No migrations to run manually — the consumer/app code applies schema on startup (or tests create tables in fixtures).

## Run the whole suite

```bash
pytest -q
```

## Run one feature's tests

Tests are organized one file per feature. Run just the current feature's file in
the fix loop so you don't re-run everything:

```bash
pytest tests/test_<feature>.py -q
```

For example: `pytest tests/test_producer.py -q` or `pytest tests/test_consumer.py -q`.

## Run a single test

```bash
pytest tests/test_<feature>.py::test_case_name -q
```

## Test file layout

- One test file per feature, named `tests/test_<feature>.py`.
- Features map to top-level directories: `producer`, `consumer`, `mcp_server`, `agent`, `eval`.
- New cases for a growing feature are appended to that file, not split into new ones.
- Shared fixtures live in `tests/conftest.py`.

## Reading results

- The pass/fail summary appears at the end of pytest's stdout output.
- Report only failing tests and their error/trace; keep passing-test noise out of
  the parent conversation.

## Known gotchas

- Integration tests that touch Kafka or Postgres require Docker services up — unit tests should mock these or be skipped if services are unavailable.
- Embedding tests (sentence-transformers, all-MiniLM-L6-v2) download the model on first run; can be slow. Consider caching in CI or mocking for unit tests.
- On Windows, ensure the venv's `Scripts\activate` is used, not `bin/activate`.
- Kafka consumer tests may need short poll timeouts to avoid hanging; use pytest timeout plugin (`--timeout=30`) if tests stall.
