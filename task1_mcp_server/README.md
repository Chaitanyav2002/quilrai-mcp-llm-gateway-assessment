# Task 1: Customer support MCP server (stdio)

An MCP server built on the official Python SDK (`mcp` 2.3.0) with two tools:

| Tool | Arguments | Rules |
|---|---|---|
| `get_customer_record` | `customer_id` | `CUST-` followed by exactly 5 ASCII digits |
| `trigger_refund` | `customer_id`, `amount`, `reason` | `amount` is above 0, at most 10,000, with at most 2 decimal places; `reason` is 10 to 500 characters after trimming |

## Run it

```bash
python -m task1_mcp_server.server          # speaks JSON-RPC on stdin/stdout
pytest task1_mcp_server -q                 # 42 tests, each one starts the real server process
```

To try it with an MCP client, add the command to its server config, for example:

```json
{ "mcpServers": { "customer-support": { "command": "python", "args": ["-m", "task1_mcp_server.server"], "cwd": "/path/to/repo" } } }
```

## How errors map to the wire

| Situation | Response |
|---|---|
| Arguments fail validation (bad id format, `amount` of 0, `0.001`, `10000.01`, `"25"` as a string, `true`, `Infinity`, short reason, extra field, missing field) | JSON-RPC error `-32602`, with `data` listing each bad field and the rule it broke |
| Unknown tool name | JSON-RPC error `-32602` (the MCP spec's code for this case; the method `tools/call` itself exists) |
| Line that is not JSON | JSON-RPC error `-32700`, `id: null` |
| Valid JSON that is not a JSON-RPC message | JSON-RPC error `-32600`, `id: null` |
| Well-formed call for a customer that does not exist | Normal result with `isError: true`, so the model can read it and recover |

The rejected values themselves are never echoed back in errors, only field names and rules.

## Design notes

**Why the low-level `Server` and not `MCPServer` (formerly FastMCP).** I checked the SDK source: `MCPServer` catches a Pydantic `ValidationError` on tool arguments and returns it as a tool result with `isError: true`. That is a reasonable default for letting a model correct itself, but this task asks for standard JSON-RPC error codes. The low-level `Server` lets the `tools/call` handler raise `MCPError(-32602)`, which the SDK sends as a real JSON-RPC error. Business failures (unknown customer) still use `isError`, because the request itself was valid.

**Strict validation.** Pydantic models run in strict mode with `extra="forbid"`, so there is no silent coercion (`"25.50"` to 25.5, `true` to 1) and no unknown fields. A few edge cases the obvious version gets wrong:
- `\d` also matches non-ASCII digits such as `١٠٠٠١`, so the pattern uses `[0-9]`.
- The SDK's JSON parser accepts the non-standard `Infinity` token, so `amount` sets `allow_inf_nan=False`.
- A reason padded with spaces to 10 characters is trimmed before the length check.
- `40` is accepted as an amount: JSON has a single number type, so `40` and `40.0` are the same value.
- The task only says "positive float". A refund tool that accepts `1e300` or `0.0001` is not useful, so I added a cap of 10,000 per refund (my assumption, one constant) and a 2-decimal-place rule.

**stdout stays pure JSON-RPC, in three layers.**
1. All logging goes to stderr, and a test fails if `server.py` contains a `print()` call.
2. While serving, the SDK points file descriptor 1 at stderr, and the server also points Python's `sys.stdout` at stderr. The second part matters: I found that a stray `print` was buffered in `sys.stdout` and flushed onto the wire at exit, after the SDK had restored fd 1.
3. An end-to-end test runs a full session (valid calls, invalid calls, a garbage line) and parses every byte of stdout as JSON-RPC.

**Parse errors get an answer.** The SDK drops lines it cannot parse (it logs them at debug level only). A small wrapper around the read stream answers them with `-32700` or `-32600` as JSON-RPC 2.0 requires, and the session keeps going.

## Tests and what they prove

- Every malformed input listed above returns `-32602`. Each one fails if the matching rule is removed: I checked by swapping `[0-9]` for `\d`, by turning strict mode off, and by adding a stray `print`.
- Unknown customer returns `isError`, not a protocol error.
- Unreadable lines get `-32700` or `-32600`, and the session still answers a `ping` afterwards.
- Every stdout line of a mixed session is valid JSON-RPC, and the logs show up on stderr.

## Not included

- The data is an in-memory stand-in. A real refund would also need an idempotency key, so a client retry does not pay twice, and a check against what the customer actually paid.
