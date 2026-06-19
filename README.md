# Streaming RAG: real-time operational intelligence over a live event stream

A real-time streaming RAG system that processes synthetic payment-transaction events through Kafka into Postgres, combining SQL aggregation with vector similarity search to answer operational queries via a Claude-powered agent.

## Architecture

| Component | Directory | Description |
|-----------|-----------|-------------|
| Producer | `producer/` | Synthetic event generator + scenario engine |
| Consumer | `consumer/` | Kafka consumer → Postgres + selective embedding pipeline |
| MCP Server | `mcp_server/` | FastMCP server with hybrid retrieval tools |
| Agent | `agent/` | Anthropic API tool-use loop + CLI chat |
| Eval | `eval/` | Golden-question harness graded against ground-truth JSONL |
| Infra | `infra/` | docker-compose: Kafka (KRaft), Postgres + pgvector |

## Status: Week 1 — infrastructure
