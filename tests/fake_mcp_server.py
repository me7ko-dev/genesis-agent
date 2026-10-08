"""A tiny MCP server over stdio for the tests (no SDK): two tools, a stray log
line in stdout, and a ping to the client in the middle of a call."""
import json
import sys

print("fake server starting (not JSON)", flush=True)
TOOLS = [
    {"name": "echo", "description": "Echo the text back",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "create_issue", "description": "Create an issue",
     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}}},
]


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    msg = json.loads(line)
    method, rid = msg.get("method"), msg.get("id")
    if rid is None:
        continue  # notification
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "1"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        send({"jsonrpc": "2.0", "id": "srv-1", "method": "ping"})
        reply = json.loads(sys.stdin.readline())
        assert reply.get("id") == "srv-1" and "result" in reply
        name, args = msg["params"]["name"], msg["params"].get("arguments") or {}
        if name == "echo":
            result = {"content": [{"type": "text", "text": "echo: " + args.get("text", "")}]}
        elif name == "create_issue":
            result = {"content": [{"type": "text", "text": "created #7 " + args.get("title", "")}]}
        else:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "unknown tool"}})
            continue
        send({"jsonrpc": "2.0", "id": rid, "result": result})
    else:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no"}})
