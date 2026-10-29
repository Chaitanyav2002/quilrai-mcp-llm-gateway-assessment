"""Small fake MCP server for the gateway tests. Records every request in app.state.received.

Send "X-Mock-Respond-With: sse" to get the reply as a server-sent event.

Run: uvicorn task2_mcp_gateway.mock_server:app --port 9001
"""

import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

TOOLS = [
    {"name": "get_weather", "description": "Current weather for a city.",
     "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}},
    {"name": "get_admin_contact", "description": "Who to contact for account help.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "admin_reset_key", "description": "Rotate the API key for a tenant.",
     "inputSchema": {"type": "object", "properties": {"tenant": {"type": "string"}}}},
]


def handle(message: dict) -> dict:
    method, request_id = message.get("method"), message.get("id")
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "mock-downstream", "version": "1.0.0"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": f"called {message['params']['name']}"}]}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def create_mock_server() -> FastAPI:
    app = FastAPI(title="mock downstream MCP server")
    app.state.received = []

    @app.post("/mcp")
    async def mcp(request: Request):
        body = await request.body()
        app.state.received.append({"body": body, "headers": dict(request.headers)})
        message = json.loads(body)
        if "method" not in message or "id" not in message:
            return JSONResponse(None, status_code=202)  # notification or client response

        reply = handle(message)
        headers = {}
        if session_id := request.headers.get("mcp-session-id"):
            headers["Mcp-Session-Id"] = session_id
        if request.headers.get("x-mock-respond-with") == "sse":
            event = f"event: message\ndata: {json.dumps(reply)}\n\n"
            return StreamingResponse(iter([event]), media_type="text/event-stream", headers=headers)
        return JSONResponse(reply, headers=headers)

    @app.api_route("/mcp", methods=["GET", "DELETE"])
    async def stream_or_close(request: Request):
        app.state.received.append({"method": request.method, "body": b"", "headers": dict(request.headers)})
        if request.method == "DELETE":
            return JSONResponse(None, status_code=204)
        return StreamingResponse(iter([": stream open\n\n"]), media_type="text/event-stream")

    return app


app = create_mock_server()
