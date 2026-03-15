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
import uuid as _uuid_mod
import decimal as _decimal_mod
import datetime as _datetime_mod
from typing import AsyncIterator, Optional
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
import anthropic
import boto3
import uuid
from databases import Database

router = APIRouter()


# ── JSON serialiser that handles Postgres types ───────────────────────────────
def _json_default(obj):
    """Handles UUID, Decimal, datetime — all common Postgres return types."""
    if isinstance(obj, _uuid_mod.UUID):
        return str(obj)
    if isinstance(obj, _decimal_mod.Decimal):
        return float(obj)
    if isinstance(obj, (_datetime_mod.datetime, _datetime_mod.date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def safe_json_dumps(obj) -> str:
    return json.dumps(obj, default=_json_default)


def _is_valid_uuid(val: str) -> bool:
    """Return True only if val is a well-formed UUID string."""
    try:
        _uuid_mod.UUID(str(val))
        return True
    except (ValueError, AttributeError):
        return False


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

**Analysis on retrieved data:**
After fetching data with your tools, you CAN and SHOULD perform analysis directly on that data. This includes:
- Word frequency counts and negative/positive language analysis across quotes
- Building custom tables (e.g. most common words per theme, sentiment by speaker)
- Pattern detection, clustering, and comparisons across sessions or themes
- Counting, ranking, summarising, or transforming any retrieved content

You are a language model with full analytical capability — your tools fetch the raw data, but YOU do the analysis. Never refuse an analytical request on the grounds that you lack a tool for it. Fetch the relevant data first, then analyse it yourself.
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
    Resolves tool calls directly against the database.
    Avoids internal HTTP round-trips and auth complexity.
    """

    if tool_name == "upload_research_file":
        # Handled via /v1/upload — this path is only hit if Claude calls the
        # tool directly without a pre-uploaded file.
        return {
            "error": "File upload via tool not supported in web UI. "
                     "Use the attachment button to upload a file."
        }

    # Validate any session_id before it hits the DB — stub IDs like "session-001"
    # or "sess_a1b2c3d4" are not valid UUIDs and will cause asyncpg to crash.
    if "session_id" in tool_input:
        sid_candidate = tool_input.get("session_id", "")
        if not _is_valid_uuid(sid_candidate):
            return {
                "error": f"Session ID '{sid_candidate}' is not a valid session. "
                         "Please use list_sessions to find your real sessions."
            }

    # ── Tool dispatch (separate chain — must not be elif of the UUID check above)
    if tool_name == "get_session_status":
        sid = tool_input.get("session_id", "")
        row = await db.fetch_one(
            "SELECT id, status, quote_count, created_at, completed_at "
            "FROM sessions WHERE id = :id AND account_id = :aid",
            {"id": sid, "aid": account_id},
        )
        if not row:
            return {"error": f"Session {sid} not found"}
        return {
            "session_id": sid,
            "status": row["status"],
            "progress_pct": 100 if row["status"] == "ready" else 0,
            "completed_at": str(row["completed_at"]) if row["completed_at"] else None,
        }

    elif tool_name == "get_synthesis":
        sid = tool_input.get("session_id", "")
        row = await db.fetch_one(
            "SELECT id, name, status, themes, findings, quote_count "
            "FROM sessions WHERE id = :id AND account_id = :aid",
            {"id": sid, "aid": account_id},
        )
        if not row:
            return {"error": f"Session {sid} not found"}
        if row["status"] != "ready":
            return {
                "session_id": sid,
                "status": row["status"],
                "message": "Session is still processing. Check back in a few minutes.",
            }
        import json as _json
        themes = row["themes"]
        findings = row["findings"]
        if isinstance(themes, str):
            themes = _json.loads(themes)
        if isinstance(findings, str):
            findings = _json.loads(findings)
        participant_row = await db.fetch_one(
            "SELECT COUNT(DISTINCT speaker) AS cnt FROM quotes WHERE session_id = :sid AND speaker IS NOT NULL",
            {"sid": sid},
        )
        participant_count = int(participant_row["cnt"]) if participant_row else 0
        return {
            "session_id": sid,
            "session_name": row["name"],
            "themes": themes or [],
            "key_findings": findings or [],
            "quote_count": row["quote_count"] or 0,
            "participant_count": participant_count,
        }

    elif tool_name == "get_quotes":
        sid = tool_input.get("session_id", "")
        try:
            q = ("SELECT id, text, speaker, timestamp_sec, theme_label "
                 "FROM quotes WHERE session_id = :sid")
            params: dict = {"sid": sid}
            if "theme_id" in tool_input:
                # theme_id in the tool maps to theme_label in the DB
                q += " AND theme_label = :tlabel"
                params["tlabel"] = tool_input["theme_id"]
            rows = await db.fetch_all(q, params)
            quotes = [
                {
                    "id": str(r["id"]),
                    "text": r["text"],
                    "speaker": r["speaker"],
                    "timestamp_sec": r["timestamp_sec"],
                    "theme_label": r["theme_label"],
                }
                for r in rows
            ]
        except Exception:
            quotes = []
        return {"quotes": quotes, "total": len(quotes)}

    elif tool_name == "search_research":
        import json as _json
        import openai as _openai

        query: str = tool_input.get("query", "").strip()
        if not query:
            return {"query": query, "results": [], "total": 0}

        results = []
        used_vector_search = False

        # ── Try vector (semantic) search first ───────────────────────────────
        try:
            _oa = _openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
            q_resp = _oa.embeddings.create(
                model="text-embedding-3-small",
                input=query,
            )
            query_vec = _json.dumps(q_resp.data[0].embedding)

            vec_q = """
                SELECT
                    qt.text, qt.speaker, qt.timestamp_sec, qt.theme_label,
                    s.id AS session_id, s.name AS session_name, s.created_at,
                    1 - (qt.embedding <=> CAST(:qvec AS vector)) AS similarity
                FROM quotes qt
                JOIN sessions s ON s.id = qt.session_id
                WHERE qt.account_id = :aid
                  AND qt.embedding IS NOT NULL
                ORDER BY qt.embedding <=> CAST(:qvec AS vector)
                LIMIT 15
            """
            params: dict = {"aid": account_id, "qvec": query_vec}
            if "project_id" in tool_input:
                vec_q = vec_q.replace(
                    "AND qt.embedding IS NOT NULL",
                    "AND qt.embedding IS NOT NULL AND s.project = :pid",
                )
                params["pid"] = tool_input["project_id"]
            if "date_from" in tool_input:
                vec_q = vec_q.replace("LIMIT 15", "AND s.created_at >= CAST(:df AS date) LIMIT 15")
                params["df"] = tool_input["date_from"]
            if "date_to" in tool_input:
                vec_q = vec_q.replace("LIMIT 15", "AND s.created_at <= CAST(:dt AS date) LIMIT 15")
                params["dt"] = tool_input["date_to"]

            rows = await db.fetch_all(vec_q, params)
            for r in rows:
                ts = r["timestamp_sec"]
                ts_str = None
                if ts is not None:
                    h, rem = divmod(ts, 3600)
                    m, s_ = divmod(rem, 60)
                    ts_str = f"{h:02d}:{m:02d}:{s_:02d}"
                results.append({
                    "relevance_score": round(float(r["similarity"]), 4),
                    "quote": r["text"],
                    "speaker": r["speaker"],
                    "timestamp": ts_str,
                    "session_id": str(r["session_id"]),
                    "session_name": r["session_name"],
                    "date": str(r["created_at"])[:10] if r["created_at"] else None,
                    "theme": r["theme_label"],
                    "search_type": "semantic",
                })
            if results:
                used_vector_search = True
                print(f"[dispatch search] vector search returned {len(results)} results")
        except Exception as e:
            print(f"[dispatch search] vector search failed, falling back to ILIKE: {e}")

        # ── Fallback: ILIKE keyword search ───────────────────────────────────
        if not used_vector_search:
            pattern = f"%{query}%"
            try:
                kw_q = """
                    SELECT
                        qt.text, qt.speaker, qt.timestamp_sec, qt.theme_label,
                        s.id AS session_id, s.name AS session_name, s.created_at
                    FROM quotes qt
                    JOIN sessions s ON s.id = qt.session_id
                    WHERE qt.account_id = :aid AND qt.text ILIKE :pattern
                    ORDER BY s.created_at DESC LIMIT 20
                """
                rows = await db.fetch_all(kw_q, {"aid": account_id, "pattern": pattern})
                for r in rows:
                    ts = r["timestamp_sec"]
                    ts_str = None
                    if ts is not None:
                        h, rem = divmod(ts, 3600)
                        m, s_ = divmod(rem, 60)
                        ts_str = f"{h:02d}:{m:02d}:{s_:02d}"
                    results.append({
                        "relevance_score": 0.90,
                        "quote": r["text"],
                        "speaker": r["speaker"],
                        "timestamp": ts_str,
                        "session_id": str(r["session_id"]),
                        "session_name": r["session_name"],
                        "date": str(r["created_at"])[:10] if r["created_at"] else None,
                        "theme": r["theme_label"],
                        "search_type": "keyword",
                    })
            except Exception as e:
                print(f"[dispatch search] ILIKE search failed: {e}")

        # ── Session metadata search (always runs as supplement) ───────────────
        try:
            pattern = f"%{query}%"
            sq = """
                SELECT id, name, findings, created_at
                FROM sessions
                WHERE account_id = :aid
                  AND (name ILIKE :pattern OR findings::text ILIKE :pattern)
                ORDER BY created_at DESC LIMIT 5
            """
            srows = await db.fetch_all(sq, {"aid": account_id, "pattern": pattern})
            seen_sessions = {r["session_id"] for r in results}
            for r in srows:
                sid = str(r["id"])
                if sid in seen_sessions:
                    continue
                findings_raw = r["findings"]
                if isinstance(findings_raw, str):
                    try:
                        findings_raw = _json.loads(findings_raw)
                    except Exception:
                        findings_raw = []
                first_quote = ""
                if findings_raw and isinstance(findings_raw, list):
                    sq_obj = findings_raw[0].get("supporting_quote", {}) if findings_raw else {}
                    first_quote = sq_obj.get("text", "") if sq_obj else ""
                results.append({
                    "relevance_score": 0.70,
                    "quote": first_quote,
                    "speaker": None,
                    "timestamp": None,
                    "session_id": sid,
                    "session_name": r["name"],
                    "date": str(r["created_at"])[:10] if r["created_at"] else None,
                    "theme": None,
                    "synthesis_summary": f"Session matched query: '{query}'",
                    "search_type": "keyword",
                })
        except Exception as e:
            print(f"[dispatch search] session metadata search failed: {e}")

        results.sort(key=lambda x: x["relevance_score"], reverse=True)
        return {
            "query": query,
            "results": results,
            "total": len(results),
            "search_type": "semantic" if used_vector_search else "keyword",
        }

    elif tool_name == "list_sessions":
        limit = int(tool_input.get("limit", 20))
        offset = int(tool_input.get("offset", 0))
        q = ("SELECT id, name, status, quote_count, created_at, completed_at "
             "FROM sessions WHERE account_id = :aid")
        params: dict = {"aid": account_id}
        if "project_id" in tool_input:
            q += " AND project_id = :pid"
            params["pid"] = tool_input["project_id"]
        q += " ORDER BY created_at DESC LIMIT :lim OFFSET :off"
        params["lim"] = limit
        params["off"] = offset
        rows = await db.fetch_all(q, params)
        sessions = []
        for r in rows:
            sessions.append({
                "id": str(r["id"]),
                "name": r["name"],
                "status": r["status"],
                "quote_count": r["quote_count"],
                "created_at": str(r["created_at"]),
                "completed_at": str(r["completed_at"]) if r["completed_at"] else None,
            })
        return {"sessions": sessions, "total": len(sessions)}

    elif tool_name == "generate_report":
        # Import the shared report logic
        import os as _os, json as _json, io as _io, boto3 as _boto3
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        import base64 as _b64

        sid = tool_input.get("session_id", "")
        fmt = tool_input.get("format", "markdown")

        row = await db.fetch_one(
            "SELECT id, name, status, themes, findings FROM sessions WHERE id = :id AND account_id = :aid",
            {"id": sid, "aid": account_id},
        )
        if not row:
            return {"error": f"Session {sid} not found"}
        if row["status"] != "ready":
            return {"error": f"Session not ready (status: {row['status']})"}

        themes = row["themes"] or []
        findings = row["findings"] or []
        if isinstance(themes, str):
            try: themes = _json.loads(themes)
            except Exception: themes = []
        if isinstance(findings, str):
            try: findings = _json.loads(findings)
            except Exception: findings = []

        quote_rows = await db.fetch_all(
            "SELECT text, speaker, timestamp_sec, theme_label FROM quotes WHERE session_id = :sid ORDER BY timestamp_sec",
            {"sid": sid},
        )
        quotes = []
        for q in quote_rows:
            ts = q["timestamp_sec"]
            ts_str = None
            if ts is not None:
                h, rem = divmod(ts, 3600)
                m, s_ = divmod(rem, 60)
                ts_str = f"{h:02d}:{m:02d}:{s_:02d}"
            quotes.append({"text": q["text"], "speaker": q["speaker"], "timestamp": ts_str, "theme": q["theme_label"] or "Other"})

        participant_row = await db.fetch_one(
            "SELECT COUNT(DISTINCT speaker) AS cnt FROM quotes WHERE session_id = :sid AND speaker IS NOT NULL",
            {"sid": sid},
        )
        participant_count = int(participant_row["cnt"]) if participant_row else 0
        data = {"themes": themes, "findings": findings, "quotes": quotes, "participant_count": participant_count}

        from api.sessions import _build_markdown, _build_pdf, _push_to_notion

        # Notion export — push to Notion API and return page URL directly
        if fmt == "notion":
            try:
                notion_url = await _push_to_notion(row["name"] or "Research Session", data)
                return {"session_id": sid, "format": "notion", "download_url": notion_url, "expires_at": None}
            except Exception as e:
                return {"error": f"Notion export failed: {e}"}

        if fmt == "pdf":
            report_bytes, ext, content_type = _build_pdf(sid, row["name"] or "Research Session", data)
        else:
            md_text = _build_markdown(sid, row["name"] or "Research Session", data)
            report_bytes = md_text.encode("utf-8")
            ext, content_type = "md", "text/markdown"

        download_url = None
        expires_at = None
        try:
            from botocore.config import Config as _BotoConfig
            s3 = _boto3.client(
                "s3",
                endpoint_url=_os.environ["R2_ENDPOINT_URL"],
                aws_access_key_id=_os.environ["R2_ACCESS_KEY_ID"],
                aws_secret_access_key=_os.environ["R2_SECRET_ACCESS_KEY"],
                config=_BotoConfig(signature_version="s3v4"),
            )
            bucket = _os.environ["R2_BUCKET_NAME"]
            report_key = f"reports/{account_id}/{sid}.{ext}"
            s3.put_object(Bucket=bucket, Key=report_key, Body=report_bytes, ContentType=content_type)
            download_url = s3.generate_presigned_url(
                "get_object", Params={"Bucket": bucket, "Key": report_key}, ExpiresIn=3600
            )
            expires_at = (_dt.now(_tz.utc) + _td(hours=1)).isoformat()
        except Exception as e:
            print(f"[dispatch report] R2 upload failed: {e}")
            download_url = f"data:{content_type};base64," + _b64.b64encode(report_bytes).decode()

        return {"session_id": sid, "format": fmt, "download_url": download_url, "expires_at": expires_at}

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
        return f"data: {safe_json_dumps(data)}\n\n"

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
                "content": safe_json_dumps(result),
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
    from botocore.config import Config as BotoConfig
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        config=BotoConfig(signature_version="s3v4"),
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
        import modal
        process_session = modal.Function.from_name(
            "sensecritiq-pipeline", "process_session"
        )
        await process_session.spawn.aio(session_id, s3_key, filename)
        print(f"[upload] Modal job spawned for session {session_id}")
    except ImportError:
        print(f"[upload] ERROR: modal not installed — session {session_id} will stay queued")
        # Mark as failed so the user gets honest feedback
        await db.execute(
            "UPDATE sessions SET status = 'failed' WHERE id = :id",
            {"id": session_id},
        )
        return JSONResponse(
            {"session_id": session_id, "status": "failed",
             "error": "Pipeline unavailable — modal package not installed on server."},
            status_code=500,
        )
    except Exception as e:
        print(f"[upload] Modal spawn failed: {e}")
        await db.execute(
            "UPDATE sessions SET status = 'failed' WHERE id = :id",
            {"id": session_id},
        )

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


@router.get("/v1/conversations/{conversation_id}/messages")
async def get_conversation_messages(
    conversation_id: str,
    request: Request,
    account_id: str = Depends(get_account_id),
):
    db: Database = request.app.state.db
    conv = await db.fetch_one(
        "SELECT id FROM conversations WHERE id = :id AND account_id = :aid",
        {"id": conversation_id, "aid": account_id},
    )
    if not conv:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Conversation not found")

    rows = await db.fetch_all(
        """SELECT id, role, content, tool_name, created_at
           FROM messages
           WHERE conversation_id = :cid
           ORDER BY created_at ASC""",
        {"cid": conversation_id},
    )
    return [dict(r) for r in rows]
