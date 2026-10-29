# Task 1: Customer support MCP server

An MCP server over stdio, built on the official Python SDK (`mcp` 2.3.0), with two tools:

- `get_customer_record(customer_id)`: the id must look like `CUST-12345`.
- `trigger_refund(customer_id, amount, reason)`: amount above 0 and at most 10,000 with up to 2 decimals, reason 10 to 500 characters after trimming.

## Run

```bash
python -m task1_mcp_server.server     # JSON-RPC on stdin/stdout
pytest task1_mcp_server -q            # 43 tests, most run the real server process
```

To use it from an MCP client, add it to the client's server config:

```json
{ "mcpServers": { "customer-support": { "command": "python", "args": ["-m", "task1_mcp_server.server"], "cwd": "/path/to/repo" } } }
```

## Errors

| Case | Response |
|---|---|
| Bad arguments (wrong id format, `0`, `0.001`, `"25"`, `true`, `Infinity`, short reason, extra or missing field) | `-32602`, with a list of the bad fields. The rejected values are not echoed back. |
| Unknown tool | `-32602`, which is what the MCP spec uses |
| A line that is not JSON | `-32700`, `id: null` |
| JSON that is not a JSON-RPC message | `-32600`, `id: null` |
| Valid call, but the customer does not exist | Normal result with `isError: true`, so the model can read it |

## Notes

- I used the low-level `Server` rather than `MCPServer` (the old FastMCP). In SDK 2.3.0, `MCPServer` turns validation errors into `isError` tool results, and this task asks for JSON-RPC error codes.
- Validation uses Pydantic strict mode with no extra fields allowed, so `"25"` and `true` are not quietly turned into numbers. A few edge cases: the id uses `[0-9]` because `\d` also matches digits like `١`, `Infinity` is rejected, the reason is trimmed before its length is checked, and `40` is a valid amount since JSON has only one number type.
- The 10,000 cap and the 2-decimal rule are my own assumption. The task only says "positive float".
- Keeping stdout clean: logs go to stderr, a test fails if `server.py` has a `print()`, and while serving `sys.stdout` points at stderr too. I added the last part after finding that a stray `print` was buffered and written onto the wire when the process exited.
- The SDK silently drops lines it can't parse. A small wrapper around the read stream answers them with `-32700` or `-32600`, as JSON-RPC requires.

## Tests

Every bad input above has a test, as do the unknown customer case and the parse errors (the session keeps working afterwards). One test runs a full session and checks that every line on stdout is valid JSON-RPC while the logs end up on stderr. I also broke the server on purpose (`\d` in the pattern, strict mode off, a stray `print`) to make sure the tests catch it.

## Not included

The customer data is in memory. A real refund would also need an idempotency key, so a retry does not pay twice, and a check against what the customer actually paid.
