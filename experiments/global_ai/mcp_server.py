"""Minimal stdio MCP transport over synthetic read-only tools; actor fixed by host argv."""
import argparse
import json
import sys

from experiments.global_ai.core import Context, Denied, Store, Tools, TOOL_SCHEMAS, seed


def dispatch(request, tools):
    method = request.get("method")
    if method == "initialize":
        return {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "synthetic-family-tools", "version": "0.1"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{**t, "annotations": {"readOnlyHint": True, "destructiveHint": False,
                                                 "openWorldHint": False}} for t in TOOL_SCHEMAS]}
    if method == "tools/call":
        params = request.get("params", {})
        try:
            result = tools.call(params.get("name"), params.get("arguments", {}))
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": False}
        except Denied:
            return {"content": [{"type": "text", "text": "Access denied or invalid tool arguments"}], "isError": True}
    raise NotImplementedError("Unsupported method")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--actor", choices=["alice", "bob"], required=True)
    parser.add_argument("--scope", choices=["personal", "family"], default="personal")
    args = parser.parse_args()
    store = Store()
    seed(store)
    tools = Tools(Context(args.actor, args.scope), store)
    for line in sys.stdin:
        request_id = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("Invalid request")
            if "id" not in request:
                continue
            request_id = request["id"]
            result = dispatch(request, tools)
            response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        except NotImplementedError:
            response = {"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32601, "message": "Method not found"}}
        except (ValueError, TypeError):
            response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": "Invalid request"}}
        print(json.dumps(response, ensure_ascii=False), flush=True)
    store.close()


if __name__ == "__main__":
    main()
