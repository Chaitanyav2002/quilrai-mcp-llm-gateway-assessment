"""Customer support MCP server over stdio.

Run: python -m task1_mcp_server.server
"""

import asyncio
import json
import logging
import sys
import uuid
from typing import Annotated, Any

import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage
from mcp_types import INVALID_PARAMS, INVALID_REQUEST, PARSE_ERROR
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator

log = logging.getLogger("customer-mcp")

MAX_REFUND = 10_000  # assumed per-refund cap, bigger refunds need a person

# [0-9] not \d, \d also matches digits like "١"
CustomerId = Annotated[str, StringConstraints(pattern=r"^CUST-[0-9]{5}$"),
                       Field(description="Customer id: CUST- and 5 digits, e.g. CUST-12345")]


class StrictArgs(BaseModel):
    # no type coercion, no unknown fields
    model_config = ConfigDict(strict=True, extra="forbid")


class GetCustomerRecordArgs(StrictArgs):
    customer_id: CustomerId


class TriggerRefundArgs(StrictArgs):
    customer_id: CustomerId
    amount: float = Field(gt=0, le=MAX_REFUND, allow_inf_nan=False,
                          description=f"Above 0, at most {MAX_REFUND}, up to 2 decimal places")
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=500)] = Field(
        description="Why the refund is issued, at least 10 characters")

    @field_validator("amount")
    @classmethod
    def whole_cents(cls, value: float) -> float:
        if round(value, 2) != value:
            raise ValueError("amount can have at most 2 decimal places")
        return value


CUSTOMERS = {
    "CUST-10001": {"customer_id": "CUST-10001", "name": "Ada Park", "tier": "gold", "status": "active"},
    "CUST-10002": {"customer_id": "CUST-10002", "name": "Ravi Menon", "tier": "standard", "status": "active"},
}


def get_customer_record(args: GetCustomerRecordArgs) -> types.CallToolResult:
    record = CUSTOMERS.get(args.customer_id)
    if record is None:
        return tool_error(f"No customer with id {args.customer_id}")
    return tool_result(record)


def trigger_refund(args: TriggerRefundArgs) -> types.CallToolResult:
    if args.customer_id not in CUSTOMERS:
        return tool_error(f"No customer with id {args.customer_id}")
    refund = {
        "refund_id": f"RF-{uuid.uuid4().hex[:12]}",
        "customer_id": args.customer_id,
        "amount": args.amount,
        "reason": args.reason,
        "status": "accepted",
    }
    log.info("refund accepted refund_id=%s customer_id=%s", refund["refund_id"], args.customer_id)
    return tool_result(refund)


TOOLS = {
    "get_customer_record": (GetCustomerRecordArgs, get_customer_record, "Look up a customer by id."),
    "trigger_refund": (TriggerRefundArgs, trigger_refund, "Issue a refund to a customer."),
}


def tool_result(data: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(text=json.dumps(data))],
        structured_content=data,
    )


def tool_error(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=message)], is_error=True)


def invalid_params(tool: str, exc: ValidationError) -> MCPError:
    # field names and rules only, never the rejected values
    problems = [
        {"field": ".".join(str(part) for part in err["loc"]), "message": err["msg"]}
        for err in exc.errors(include_input=False, include_url=False, include_context=False)
    ]
    return MCPError(code=INVALID_PARAMS, message=f"Invalid arguments for tool {tool}", data=problems)


async def list_tools(ctx, params) -> types.ListToolsResult:
    return types.ListToolsResult(tools=[
        types.Tool(name=name, description=description, input_schema=model.model_json_schema())
        for name, (model, _, description) in TOOLS.items()
    ])


async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    log.info("tool call name=%r", params.name)
    if params.name not in TOOLS:
        raise MCPError(code=INVALID_PARAMS, message=f"Unknown tool: {params.name}")

    model, handler, _ = TOOLS[params.name]
    try:
        args = model.model_validate(params.arguments or {})
    except ValidationError as exc:
        log.info("rejected arguments for %r: %d problem(s)", params.name, exc.error_count())
        raise invalid_params(params.name, exc) from None
    return handler(args)


server = Server("customer-support", version="1.0.0", on_list_tools=list_tools, on_call_tool=call_tool)


class AnswerUnreadableLines:
    """The SDK drops lines it can't parse. This answers them with -32700 / -32600."""

    def __init__(self, inner, write_stream):
        self._inner = inner
        self._write = write_stream

    def __getattr__(self, name):
        # the SDK reads attributes like last_context from the stream
        return getattr(self._inner, name)

    # the SDK uses async for today; receive() is kept so a future caller can't skip the check
    async def receive(self):
        return await self._next(self._inner.receive)

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._next(self._inner.__anext__)

    async def _next(self, read):
        while True:
            item = await read()
            if not isinstance(item, Exception):
                return item
            await self._write.send(SessionMessage(unreadable_line_reply(item)))

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.aclose()


def unreadable_line_reply(exc: Exception) -> types.JSONRPCError:
    not_json = isinstance(exc, ValidationError) and any(e["type"] == "json_invalid" for e in exc.errors())
    code, message = (PARSE_ERROR, "Parse error") if not_json else (INVALID_REQUEST, "Invalid Request")
    log.warning("unreadable line on stdin, answered %s", code)
    return types.JSONRPCError(jsonrpc="2.0", id=None, error=types.ErrorData(code=code, message=message))


async def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("starting customer-support MCP server on stdio")
    async with stdio_server() as (read_stream, write_stream):
        # print() output is buffered and would otherwise reach the wire at exit
        real_stdout, sys.stdout = sys.stdout, sys.stderr
        try:
            reader = AnswerUnreadableLines(read_stream, write_stream)
            await server.run(reader, write_stream, server.create_initialization_options())
        finally:
            sys.stdout = real_stdout


if __name__ == "__main__":
    asyncio.run(main())
