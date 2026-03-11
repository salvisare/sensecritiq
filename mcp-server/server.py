"""
SenseCritiq MCP Server
"""
import os, json, httpx
from typing import Any
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

API_KEY = os.environ.get("SCQ_API_KEY", "")
API_BASE_URL = os.environ.get("SCQ_API_BASE_URL", "https://api.sensecritiq.com").rstrip("/")

if not API_KEY:
    raise EnvironmentError("SCQ_API_KEY is not set. Get your key at: https://sensecritiq.com/dashboard")

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
    "User-Agent": "sensecritiq-mcp/0.1.0",
}

async def _request(method, path, **kwargs):
    url = f"{API_BASE_URL}{path}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.request(method, url, headers=HEADERS, **kwargs)
        r.raise_for_status()
        return r.json()

server = Server("sensecritiq")

@server.list_tools()
async def list_tools():
    return [
        types.Tool(name="upload_research_file", description="Upload a research artifact (audio, video, transcript, or survey) to SenseCritiq for processing. Returns a session_id to track progress. Supported: mp3, mp4, wav, m4a, txt, pdf, docx.", inputSchema={"type":"object","properties":{"file_path":{"type":"string"},"session_name":{"type":"string"},"project_id":{"type":"string"},"tags":{"type":"array","items":{"type":"string"}}},"required":["file_path","session_name"]}),
        types.Tool(name="get_session_status", description="Check processing status of a research session: queued → processing → ready → failed. Poll after upload until 'ready'.", inputSchema={"type":"object","properties":{"session_id":{"type":"string"}},"required":["session_id"]}),
        types.Tool(name="get_synthesis", description="Retrieve synthesised themes, key findings, quote count and participant count for a completed session.", inputSchema={"type":"object","properties":{"session_id":{"type":"string"}},"required":["session_id"]}),
        types.Tool(name="get_quotes", description="Retrieve verbatim quotes from a session, optionally filtered by theme. Each quote includes speaker, timestamp, and theme tag.", inputSchema={"type":"object","properties":{"session_id":{"type":"string"},"theme_id":{"type":"string"}},"required":["session_id"]}),
        types.Tool(name="search_research", description="Semantic search across all research sessions and projects. Ask natural language questions like 'what have users said about checkout friction?'", inputSchema={"type":"object","properties":{"query":{"type":"string"},"project_id":{"type":"string"},"date_from":{"type":"string"},"date_to":{"type":"string"}},"required":["query"]}),
        types.Tool(name="list_sessions", description="List research sessions, optionally filtered by project. Returns name, date, status, and ID.", inputSchema={"type":"object","properties":{"project_id":{"type":"string"},"limit":{"type":"integer","default":20},"offset":{"type":"integer","default":0}},"required":[]}),
        types.Tool(name="generate_report", description="Generate a formatted research report. Returns a download URL valid for 1 hour. Formats: markdown, pdf, notion.", inputSchema={"type":"object","properties":{"session_id":{"type":"string"},"format":{"type":"string","enum":["markdown","pdf","notion"],"default":"markdown"}},"required":["session_id","format"]}),
    ]

@server.call_tool()
async def call_tool(name, arguments):
    try:
        result = await _dispatch(name, arguments)
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]
    except httpx.HTTPStatusError as e:
        return [types.TextContent(type="text", text=json.dumps({"error": f"API error {e.response.status_code}", "detail": e.response.text}, indent=2))]
    except httpx.RequestError as e:
        return [types.TextContent(type="text", text=json.dumps({"error": "Could not reach SenseCritiq API", "detail": str(e)}, indent=2))]

async def _dispatch(name, args):
    if name == "upload_research_file":
        file_path = args["file_path"]
        with open(file_path, "rb") as f:
            file_bytes = f.read()
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(f"{API_BASE_URL}/sessions", headers={k:v for k,v in HEADERS.items() if k != "Content-Type"}, files={"file": (os.path.basename(file_path), file_bytes)}, data={"session_name": args["session_name"], "project_id": args.get("project_id",""), "tags": json.dumps(args.get("tags",[]))})
            r.raise_for_status(); return r.json()
    elif name == "get_session_status":
        return await _request("GET", f"/sessions/{args['session_id']}/status")
    elif name == "get_synthesis":
        return await _request("GET", f"/sessions/{args['session_id']}/synthesis")
    elif name == "get_quotes":
        params = {"theme_id": args["theme_id"]} if "theme_id" in args else {}
        return await _request("GET", f"/sessions/{args['session_id']}/quotes", params=params)
    elif name == "search_research":
        payload = {k: args[k] for k in ["query","project_id","date_from","date_to"] if k in args}
        return await _request("POST", "/search", json=payload)
    elif name == "list_sessions":
        params = {"limit": args.get("limit",20), "offset": args.get("offset",0)}
        if "project_id" in args: params["project_id"] = args["project_id"]
        return await _request("GET", "/sessions", params=params)
    elif name == "generate_report":
        return await _request("POST", f"/sessions/{args['session_id']}/report", json={"format": args.get("format","markdown")})
    else:
        return {"error": f"Unknown tool: {name}"}

async def main():
    async with stdio_server() as streams:
        await server.run(streams[0], streams[1], server.create_initialization_options())

if __name__ == "__main__":
    import asyncio; asyncio.run(main())
