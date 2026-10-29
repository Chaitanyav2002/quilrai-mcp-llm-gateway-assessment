"""Runs the real server process and talks raw JSON-RPC to it, so stdout can be checked byte for byte."""

import ast
import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INVALID_PARAMS = -32602
TIMEOUT = 10


class StdioSession:
    def __init__(self, proc: asyncio.subprocess.Process):
        self.proc = proc
        self.stdout_lines: list[bytes] = []
        self._next_id = 1

    async def send(self, message: dict | str) -> None:
        line = message if isinstance(message, str) else json.dumps(message)
        self.proc.stdin.write(line.encode() + b"\n")
        await self.proc.stdin.drain()

    async def request(self, method: str, params: dict | None = None) -> dict:
        request_id = self._next_id
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        await self.send(message)
        while True:
            reply = await self.next_message()
            if reply.get("id") == request_id:
                return reply

    async def next_message(self) -> dict:
        line = await asyncio.wait_for(self.proc.stdout.readline(), TIMEOUT)
        assert line, "server closed stdout before answering"
        self.stdout_lines.append(line)
        return json.loads(line)

    async def call_tool(self, name: str, arguments: dict | None) -> dict:
        return await self.request("tools/call", {"name": name, "arguments": arguments})

    async def close(self) -> bytes:
        self.proc.stdin.close()
        rest, stderr = await asyncio.wait_for(self.proc.communicate(), TIMEOUT)
        self.stdout_lines.extend(rest.splitlines(keepends=True))
        return stderr


@pytest.fixture
async def session():
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "task1_mcp_server.server",
        cwd=ROOT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    s = StdioSession(proc)
    init = await s.request("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1.0"},
    })
    assert "result" in init, init
    await s.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    yield s
    if proc.returncode is None:
        await s.close()


def error_code(reply: dict) -> int | None:
    return reply.get("error", {}).get("code")


# protocol flow

async def test_lists_both_tools_with_their_constraints(session):
    reply = await session.request("tools/list")
    tools = {t["name"]: t for t in reply["result"]["tools"]}

    assert set(tools) == {"get_customer_record", "trigger_refund"}
    refund = tools["trigger_refund"]["inputSchema"]
    assert refund["required"] == ["customer_id", "amount", "reason"]
    assert refund["properties"]["reason"]["minLength"] == 10
    assert refund["properties"]["amount"]["exclusiveMinimum"] == 0
    assert refund["additionalProperties"] is False


async def test_get_customer_record_returns_the_record(session):
    reply = await session.call_tool("get_customer_record", {"customer_id": "CUST-10001"})

    result = reply["result"]
    assert not result.get("isError")
    assert result["structuredContent"]["customer_id"] == "CUST-10001"


async def test_trigger_refund_accepts_valid_input(session):
    reply = await session.call_tool("trigger_refund", {
        "customer_id": "CUST-10001", "amount": 25.5, "reason": "Item arrived damaged",
    })

    refund = reply["result"]["structuredContent"]
    assert refund["status"] == "accepted"
    assert refund["amount"] == 25.5


async def test_integer_amount_is_a_valid_float(session):
    # 40 and 40.0 are the same JSON number
    reply = await session.call_tool("trigger_refund", {
        "customer_id": "CUST-10001", "amount": 40, "reason": "Duplicate charge on card",
    })

    assert reply["result"]["structuredContent"]["amount"] == 40.0


@pytest.mark.parametrize("amount", [0.01, 19.99, 10_000])
async def test_amount_limits_are_inclusive(session, amount):
    reply = await session.call_tool("trigger_refund", {
        "customer_id": "CUST-10001", "amount": amount, "reason": "Item arrived damaged",
    })

    assert reply["result"]["structuredContent"]["amount"] == amount


async def test_unknown_customer_is_a_tool_error_not_a_protocol_error(session):
    # valid request, the customer just doesn't exist
    reply = await session.call_tool("get_customer_record", {"customer_id": "CUST-99999"})

    assert "error" not in reply
    assert reply["result"]["isError"] is True


# validation

@pytest.mark.parametrize("arguments", [
    {"customer_id": "cust-10001"},          # wrong case
    {"customer_id": "CUST-1000"},           # 4 digits
    {"customer_id": "CUST-100011"},         # 6 digits
    {"customer_id": "CUST-1000A"},          # letter
    {"customer_id": " CUST-10001"},         # leading space
    {"customer_id": "CUST-10001\n"},        # trailing newline
    {"customer_id": "CUST-١٠٠٠١"},          # Arabic-Indic digits (\d would allow them)
    {"customer_id": 10001},                 # not a string
    {"customer_id": None},
    {},                                     # missing
    {"customer_id": "CUST-10001", "admin": True},  # unexpected field
])
async def test_get_customer_record_rejects_malformed_input(session, arguments):
    reply = await session.call_tool("get_customer_record", arguments)

    assert error_code(reply) == INVALID_PARAMS, reply


@pytest.mark.parametrize("override", [
    {"amount": 0},
    {"amount": -5.0},
    {"amount": "25.50"},                    # number sent as a string
    {"amount": True},                       # bool is an int subclass in Python
    {"amount": None},
    {"amount": 0.001},                      # fraction of a cent
    {"amount": 10_000.01},                  # over the per-refund cap
    {"amount": 1e300},
    {"reason": "too short"},                # 9 characters
    {"reason": "          "},               # 10 spaces
    {"reason": "   short     "},            # padded to 10+, but only 5 real characters
    {"reason": 1234567890},
    {"customer_id": "CUST-123"},
])
async def test_trigger_refund_rejects_malformed_input(session, override):
    arguments = {"customer_id": "CUST-10001", "amount": 25.5, "reason": "Item arrived damaged"}
    arguments.update(override)

    reply = await session.call_tool("trigger_refund", arguments)

    assert error_code(reply) == INVALID_PARAMS, reply


async def test_non_finite_amount_is_rejected(session):
    # the SDK's JSON parser accepts Infinity
    await session.send(
        '{"jsonrpc":"2.0","id":900,"method":"tools/call","params":{"name":"trigger_refund",'
        '"arguments":{"customer_id":"CUST-10001","amount":Infinity,"reason":"Item arrived damaged"}}}'
    )
    reply = await session.next_message()

    assert reply["id"] == 900
    assert error_code(reply) == INVALID_PARAMS


@pytest.mark.parametrize("line, code", [
    ("this is not json", -32700),           # Parse error
    ('{"jsonrpc": "2.0", "id": 1', -32700),  # truncated
    ('{"hello": "world"}', -32600),          # valid JSON, not a JSON-RPC message
    ("[]", -32600),
])
async def test_unreadable_lines_get_a_json_rpc_error_and_the_session_survives(session, line, code):
    await session.send(line)
    reply = await session.next_message()

    assert reply == {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": reply["error"]["message"]}}
    assert error_code(reply) == code
    assert "result" in await session.request("ping")


async def test_missing_arguments_object_is_rejected(session):
    reply = await session.call_tool("trigger_refund", None)

    assert error_code(reply) == INVALID_PARAMS


async def test_validation_error_names_the_field_without_echoing_the_value(session):
    reply = await session.call_tool("trigger_refund", {
        "customer_id": "CUST-10001", "amount": -1, "reason": "secret-ish reason text",
    })

    fields = [problem["field"] for problem in reply["error"]["data"]]
    assert fields == ["amount"]
    assert "secret-ish" not in json.dumps(reply)


async def test_unknown_tool_is_invalid_params(session):
    # the MCP spec uses -32602 for an unknown tool
    reply = await session.call_tool("delete_customer", {"customer_id": "CUST-10001"})

    assert error_code(reply) == INVALID_PARAMS


# stdio isolation

async def test_stdout_carries_only_json_rpc_and_logs_go_to_stderr(session):
    await session.request("tools/list")
    await session.call_tool("get_customer_record", {"customer_id": "CUST-10001"})
    await session.call_tool("trigger_refund", {"customer_id": "bad", "amount": 1, "reason": "x"})
    await session.send("this is not json")
    await session.request("ping")

    stderr = await session.close()

    assert session.stdout_lines
    for line in session.stdout_lines:
        message = json.loads(line)  # every stdout line must parse
        assert message["jsonrpc"] == "2.0"
        assert "result" in message or "error" in message
    assert b"tool call" in stderr  # the logs exist, they just live on stderr


def test_server_source_has_no_print_calls():
    tree = ast.parse((ROOT / "task1_mcp_server" / "server.py").read_text())
    prints = [n.lineno for n in ast.walk(tree)
              if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "print"]

    assert prints == [], f"print() would write to stdout, lines {prints}"
