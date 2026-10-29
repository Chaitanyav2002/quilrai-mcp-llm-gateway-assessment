# AI Field Developer assessment

Four tasks on MCP servers, MCP and LLM gateways, stream guardrails and model routing. All in Python (3.11 or newer for `asyncio.timeout`; developed and tested on 3.14), each in its own folder with a README covering how to run it, the design decisions and what the tests prove.

| Task | Folder | Summary |
|---|---|---|
| 1. MCP server with strict validation | [`task1_mcp_server/`](task1_mcp_server/) | stdio MCP server on the official SDK, Pydantic strict schemas, `-32602` for bad input, stdout kept to pure JSON-RPC |
| 2. MCP security gateway | [`task2_mcp_gateway/`](task2_mcp_gateway/) | Reverse proxy that reads the role from a signed JWT and returns `-32001` for `admin_*` calls by non-admins without touching the downstream server |
| 3. Streaming PII redaction | [`task3_pii_stream_guard/`](task3_pii_stream_guard/) | LLM gateway that redacts emails, SSNs and card numbers from an SSE stream as it flows, even when a value is split across chunks |
| 4. Rate limiter and fallback router | [`task4_model_router/`](task4_model_router/) | 50k tokens per minute per API key in a SQLite sliding window, failover on 429 or 3000 ms timeout, sanitized errors |

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest -q          # 185 tests across all four tasks
```

Each task folder shows how to run its servers by hand. Configuration comes from environment variables; see [`.env.example`](.env.example). No secrets are stored in the repo.

## How I tested

Every task uses real processes or real HTTP where it matters: Task 1 drives the server binary over stdio, and Task 3 measures time to first token through live uvicorn servers. For each task I also broke the code on purpose (removed a check, weakened a transaction, disabled the timeout) to confirm the tests fail when they should. The task READMEs list those checks.

## Notes

- `mcp` is pinned to 2.3.0. In this version FastMCP was renamed `MCPServer`, which turns validation errors into tool results rather than JSON-RPC errors. Task 1 explains why it uses the low-level server instead.
- The mock servers in each folder stand in for real providers, so everything runs offline with no API keys.
