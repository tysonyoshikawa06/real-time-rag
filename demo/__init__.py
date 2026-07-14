"""Scripted, repeatable demo of the streaming-RAG stack (Step 18A).

See demo/run_demo.py for the orchestrator itself. Builds no new retrieval,
injection, or tool-use logic - it only sequences existing pieces
(producer.inject, agent.loop.run_loop, agent.mcp_bridge.MCPBridge) into a
narrated, repeatable session.
"""
