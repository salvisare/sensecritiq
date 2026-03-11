"""
SenseCritiq Phase 2 — Chat API
-------------------------------
SSE streaming endpoint. Receives a user message, runs Claude as an
orchestrator with the SenseCritiq tool catalogue, and streams the
response back to the browser in real time.

Authentication: Clerk JWT (web users) OR Bearer API key (MCP / API users).
Both resolve to an account_id that is used for all DB access.
"""

import json
import os
import asyncio
from typing import AsyncIterator, Optional
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
import anthropic
import boto3
import uuid
import modal
from databases import Database

router = APIRouter()

# ── Anthropic client ──────────────────────────────────────────────────────────
_anthropic = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# ── Tool definitions (mirrors the MCP tool catalogue) ────────────────────────
TOOLS = [
    {
        "name": "upload_research_file",
        "description": (
            "Upload a research artifact (audio, video, transcript, or survey) for processing. "
            "Returns a session_id. Processing is async — use get_session_status to poll."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to the file on the researcher's machine"},
                "session_name": {"type": "string", "description": "Human-readable name for this session"},
                "project_id": {"type": "string", "description": "Optional project UUID to group this session under"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags"},
            },
            "required": ["file_path", "session_name"],
        },
    },
    {
        "name": "get_session_status",
        "description": "Check the processing status of a session. Status values: queued, processing, ready, failed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "UUID of the session to check"},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "get_synthesis",
        "description": (
            "Retrieve the synthesised insights for a completed session: themes, key findings, "
            "quote count, and participant count. Only works when status=ready."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "UUID of the session"},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "get_quotes",
        "description": (
            "Retrieve verbatim quotes from a session, optionally filtered by theme. "
            "Each quote includes speaker label, timestamp, and theme tag."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "UUID of the session"},
                "theme_id": {"type": "string", "description": "Optional theme ID to filter quotes"},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "search_research",
        "description": (
            "Semantic search across all research sessions. Ask natural language questions "
            "like 'what have users said about onboarding?' Returns ranked quotes with session context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language search query"},
                "project_id": {"type": "string", "description": "Optional project UUID to scope the search"},
                "date_from": {"type": "string", "description": "ISO date string, e.g. 2026-01-01"},
                "date_to": {"type": "string", "description": "ISO date string, e.g. 2026-03-31"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_sessions",
        "description": "List research sessions, optionally filtered by project. Returns name, date, status, and ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Optional project UUID"},
                "limit": {"type": "integer", "description": "Max results (default 20)"},
                "offset": {"type": "integer", "description": "Pagination offset"},
            },
            "required": [],
        },
    },
    {
        "name": "generate_report",
        "description": "Generate a formatted research report for a session. Returns a download URL valid for 1 hour.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "UUID of the session"},
                "format": {
                    "type": "string",
                    "enum": ["markdown", "pdf", "notion"],
                    "description": "Output format",
                },
            },
            "required": ["session_id", "format"],
        },
    },
]

SYSTEM_PROMPT = """\
You are SenseCritiq, an AI research synthesis assistant. You help UX researchers make sense of their research data.

You have access to a set of tools that let you upload research artifacts, retrieve synthesised insights, search across research history, and generate reports.

**Core principles:**
- Every finding you present must be grounded in a verbatim quote. Never state a theme without citing the source.
- When displaying quotes, always include the speaker label and timestamp if available.
- Format quotes as blockquotes so they are visually distinct.
- Be concise in your synthesis. Researchers want insights, not summaries.
- When a session is processing, tell the user and offer to check back when it's ready.
- If a search returns no results, say so clearly and suggest alternative queries.

**Citing sources:**
Use inline citation markers like [Session: Interview 3, P2, 14:32] after each finding.
When multiple quotes support a finding, list them all.
"""

# ── Request / response models ─────────────────────────────────────────────────
class ChatMessage(BaseModel):
    role: str   # "user" | "assistant"
    content: str

class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None  # None = start new conversation
    session_id: Optional[str] = None       # anchor to a specific session

# ── Auth dependency ───────────────────────────────────────────────────────────
async def get_account_id(request: Request) -> str:
    """
    Accepts either:
    - Clerk JWT in Authorization: Bearer <jwt>  (web UI users)
    - Unkey API key in Authorization: Bearer scq_live_... (API users)
    Returns account_id string.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    token = auth_header[len("Bearer "):]

    # Clerk JWT has 3 parts separated by 2 dots (header.payload.signature)
    # API keys start with scq_
    if not token.startswith("scq_"):
        from api.portal import _verify_clerk_token
        try:
            user_info = await _verify_clerk_token(request)
            clerk_user_id = user_info.get("clerk_user_id", "")
            email = user_info.get("email", "")
            db: Database = request.app.state.db
            row = await db.fetch_one(
                "SELECT id FROM accounts WHERE clerk_user_id = :cuid OR email = :email LIMIT 1",
                {"cuid": clerk_user_id, "email": email},
            )
            if not row:
                raise HTTPException(status_code=401, detail="Account not found")
            return str(row["id"])
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

    # API key
    from middleware.auth import verify_api_key
    return await verify_api_key(request)


# ── Internal tool dispatcher ──────────────────────────────────────────────────
async def dispatch_tool(
    tool_name: str,
    tool_input: dict,
    account_id: str,
    db: Database,
    base_url: str,
) -> dict:
    """
    Calls the appropriate backend service for each tool.
    Returns a dict that Claude receives as the tool result.
    """
    import httpx

    # For Phase 2 we route through the existing REST endpoints so we don't
    # duplicate business logic. Internal calls skip auth by using a shared
    # internal secret header (set via INTERNAL_SECRET env var).
    headers = {
        "X-Internal-Account-Id": account_id,
        "X-Internal-Secret": os.environ.get("INTERNAL_SECRET", ""),
    }

    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=120) as client:
        if tool_name == "upload_research_file":
            resp = await client.post("/v1/sessions", json={
                "file_path": tool_input["file_path"],
                "session_name": tool_input["session_name"],
                "project_id": tool_input.get("project_id"),
                "tags": tool_input.get("tags", []),
            })
            return resp.json()

        elif tool_name == "get_session_status":
            resp = await client.get(f"/v1/sessions/{tool_input['session_id']}/status")
            return resp.json()

        elif tool_name == "get_synthesis":
            resp = await client.get(f"/v1/sessions/{tool_input['session_id']}/synthesis")
            return resp.json()

        elif tool_name == "get_quotes":
            params = {}
            if "theme_id" in tool_input:
                params["theme_id"] = tool_input["theme_id"]
            resp = await client.get(f"/v1/sessions/{tool_input['session_id']}/quotes", params=params)
            return resp.json()

        elif tool_name == "search_research":
            resp = await client.post("/v1/search", json=tool_input)
            return resp.json()

        elif tool_name == "list_sessions":
            params = {
                "limit": tool_input.get("limit", 20),
                "offset": tool_input.get("offset", 0),
            }
            if "project_id" in tool_input:
                params["project_id"] = tool_input["project_id"]
            resp = await client.get("/v1/sessions", params=params)
            return resp.json()

        elif tool_name == "generate_report":
            resp = await client.post(f"/v1/sessions/{tool_input['session_id']}/report", json={
                "format": tool_input["format"],
            })
            return resp.json()

    return {"error": f"Unknown tool: {tool_name}"}


# ── Conversation persistence ──────────────────────────────────────────────────
async def get_or_create_conversation(
    db: Database,
    account_id: str,
    conversation_id: Optional[str],
    first_message: str,
    session_id: Optional[str] = None,
) -> str:
    if conversation_id:
        row = await db.fetch_one(
            "SELECT id FROM conversations WHERE id = :id AND account_id = :aid",
            {"id": conversation_id, "aid": account_id},
        )
        if row:
            return str(row["id"])

    # Create new conversation with auto-title from first message
    title = first_message[:60] + ("…" if len(first_message) > 60 else "")
    new_id = await db.execute(
        """INSERT INTO conversations (account_id, title, session_id)
           VALUES (:aid, :title, :sid) RETURNING id""",
        {"aid": account_id, "title": title, "sid": session_id},
    )
    return str(new_id)


async def load_history(db: Database, conversation_id: str) -> list[dict]:
    rows = await db.fetch_all(
        """SELECT role, content FROM messages
           WHERE conversation_id = :cid
           ORDER BY created_at ASC""",
        {"cid": conversation_id},
    )
    return [{"role": r["role"], "content": r["content"]} for r in rows if r["role"] in ("user", "assistant")]


async def save_message(db: Database, conversation_id: str, role: str, content: str):
    await db.execute(
        """INSERT INTO messages (conversation_id, role, content)
           VALUES (:cid, :role, :content)""",
        {"cid": conversation_id, "role": role, "content": content},
    )


# ── SSE streaming generator ───────────────────────────────────────────────────
async def stream_chat(
    message: str,
    conversation_id: str,
    account_id: str,
    session_id: Optional[str],
    db: Database,
    base_url: str,
) -> AsyncIterator[str]:
    """
    Yields SSE-formatted strings.
    Event types:
      - text:   {"type": "text", "delta": "..."}
      - tool:   {"type": "tool_start", "name": "...", "input": {...}}
      - result: {"type": "tool_result", "name": "...", "result": {...}}
      - done:   {"type": "done", "conversation_id": "..."}
      - error:  {"type": "error", "message": "..."}
    """

    def sse(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    # Load history
    history = await load_history(db, conversation_id)

    # Append current user message
    await save_message(db, conversation_id, "user", message)
    history.append({"role": "user", "content": message})

    full_assistant_text = ""
    messages = list(history)

    # Agentic loop: Claude can call tools multiple times before responding
    while True:
        # Stream from Claude
        tool_use_blocks = []
        current_text = ""

        with _anthropic.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        ) as stream:
            for event in stream:
                if hasattr(event, "type"):
                    if event.type == "content_block_start":
                        if hasattr(event, "content_block"):
                            if event.content_block.type == "tool_use":
                                tool_use_blocks.append({
                                    "id": event.content_block.id,
                                    "name": event.content_block.name,
                                    "input": {},
                                    "_json_buf": "",
                                })
                                yield sse({"type": "tool_start", "name": event.content_block.name, "input": {}})

                    elif event.type == "content_block_delta":
                        if hasattr(event, "delta"):
                            if event.delta.type == "text_delta":
                                current_text += event.delta.text
                                full_assistant_text += event.delta.text
                                yield sse({"type": "text", "delta": event.delta.text})
                            elif event.delta.type == "input_json_delta" and tool_use_blocks:
                                tool_use_blocks[-1]["_json_buf"] += event.delta.partial_json

            final_message = stream.get_final_message()

        # Parse tool inputs
        for block in tool_use_blocks:
            if block["_json_buf"]:
                try:
                    block["input"] = json.loads(block["_json_buf"])
                except json.JSONDecodeError:
                    block["input"] = {}

        # If no tool calls, we're done
        if not tool_use_blocks:
            break

        # Add assistant message with tool use blocks to history
        assistant_content = []
        if current_text:
            assistant_content.append({"type": "text", "text": current_text})
        for block in tool_use_blocks:
            assistant_content.append({
                "type": "tool_use",
                "id": block["id"],
                "name": block["name"],
                "input": block["input"],
            })
        messages.append({"role": "assistant", "content": assistant_content})

        # Execute each tool and collect results
        tool_results = []
        for block in tool_use_blocks:
            result = await dispatch_tool(block["name"], block["input"], account_id, db, base_url)
            yield sse({"type": "tool_result", "name": block["name"], "result": result})
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": json.dumps(result),
            })

        messages.append({"role": "user", "content": tool_results})

        # If the final stop reason wasn't tool_use, break
        if final_message.stop_reason != "tool_use":
            break

    # Save assistant response to DB
    if full_assistant_text:
        await save_message(db, conversation_id, "assistant", full_assistant_text)

    yield sse({"type": "done", "conversation_id": conversation_id})


# ── Route ─────────────────────────────────────────────────────────────────────
@router.post("/v1/chat")
async def chat_endpoint(
    body: ChatRequest,
    request: Request,
    account_id: str = Depends(get_account_id),
):
    db: Database = request.app.state.db
    base_url = os.environ.get("INTERNAL_BASE_URL", "http://localhost:8000")

    conversation_id = await get_or_create_conversation(
        db, account_id, body.conversation_id, body.message, body.session_id
    )

    return StreamingResponse(
        stream_chat(
            message=body.message,
            conversation_id=conversation_id,
            account_id=account_id,
            session_id=body.session_id,
            db=db,
            base_url=base_url,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disables Nginx buffering
            "Access-Control-Allow-Origin": "*",
        },
    )


# ── Web file upload endpoint ──────────────────────────────────────────────────
@router.post("/v1/upload")
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    session_name: str = Form(...),
    account_id: str = Depends(get_account_id),
):
    """
    Accepts a multipart file upload from the browser, stores it in R2,
    creates a session record, and spawns the Modal pipeline.
    Returns session_id immediately so the frontend can poll status.
    """
    db: Database = request.app.state.db

    # Read file bytes
    contents = await file.read()
    filename = file.filename or "upload"
    session_id = str(uuid.uuid4())

    # Upload to R2
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    )
    s3_key = f"uploads/{account_id}/{session_id}/{filename}"
    s3.put_object(
        Bucket=os.environ["R2_BUCKET_NAME"],
        Key=s3_key,
        Body=contents,
        ContentType=file.content_type or "application/octet-stream",
    )

    # Create session record
    await db.execute(
        """INSERT INTO sessions
               (id, account_id, name, status, file_s3_key, created_at)
           VALUES (:id, :account_id, :name, 'queued', :s3_key, NOW())""",
        {"id": session_id, "account_id": account_id,
         "name": session_name, "s3_key": s3_key},
    )

    # Spawn Modal pipeline
    try:
        process_session = modal.Function.from_name(
            "sensecritiq-pipeline", "process_session"
        )
        await process_session.spawn.aio(session_id, s3_key, filename)
    except Exception as e:
        print(f"[upload] Modal spawn failed: {e}")

    return JSONResponse({"session_id": session_id, "status": "queued"})


# ── Conversations list endpoint ───────────────────────────────────────────────
@router.get("/v1/conversations")
async def list_conversations(
    request: Request,
    account_id: str = Depends(get_account_id),
    limit: int = 20,
    offset: int = 0,
):
    db: Database = request.app.state.db
    rows = await db.fetch_all(
        """SELECT id, title, created_at, updated_at
           FROM conversations
           WHERE account_id = :aid
           ORDER BY updated_at DESC
           LIMIT :limit OFFSET :offset""",
        {"aid": account_id, "limit": limit, "offset": offset},
    )
    return [dict(r) for r in rows]
