"""
SenseCritiq — API endpoints (Week 6: real DB reads + Unkey auth).
"""

import os
import json
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Request, Depends
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from middleware.auth import verify_api_key

router = APIRouter()

DATABASE_URL = os.environ.get("DATABASE_URL", "")

def _get_engine():
    url = DATABASE_URL
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(url, connect_args={"ssl": "require"})


# ---------------------------------------------------------------------------
# POST /sessions  — upload_research_file  (auth added, account_id from key)
# ---------------------------------------------------------------------------

@router.post("/sessions")
async def create_session(request: Request, account_id: str = Depends(verify_api_key)):
    form = await request.form()
    session_name = form.get("session_name", "Unnamed session")
    return {
        "session_id": str(uuid.uuid4()),
        "session_name": session_name,
        "status": "queued",
        "estimated_processing_time": "3–5 minutes",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# GET /sessions  — list_sessions
# ---------------------------------------------------------------------------

@router.get("/sessions")
async def list_sessions(
    limit: int = 20,
    offset: int = 0,
    project_id: str = None,
    account_id: str = Depends(verify_api_key),
):
    engine = _get_engine()
    async with engine.connect() as conn:
        if project_id:
            result = await conn.execute(
                text("""
                    SELECT id, name, project, status, quote_count, created_at, completed_at
                    FROM sessions
                    WHERE account_id = :account_id AND project = :project_id
                    ORDER BY created_at DESC
                    LIMIT :limit OFFSET :offset
                """),
                {"account_id": account_id, "project_id": project_id, "limit": limit, "offset": offset}
            )
        else:
            result = await conn.execute(
                text("""
                    SELECT id, name, project, status, quote_count, created_at, completed_at
                    FROM sessions
                    WHERE account_id = :account_id
                    ORDER BY created_at DESC
                    LIMIT :limit OFFSET :offset
                """),
                {"account_id": account_id, "limit": limit, "offset": offset}
            )
        rows = result.fetchall()
        sessions = []
        for row in rows:
            sessions.append({
                "id": str(row[0]),
                "name": row[1],
                "project": row[2],
                "status": row[3],
                "quote_count": row[4] or 0,
                "created_at": row[5].isoformat() if row[5] else None,
                "completed_at": row[6].isoformat() if row[6] else None,
            })

        count_result = await conn.execute(
            text("SELECT COUNT(*) FROM sessions WHERE account_id = :account_id"),
            {"account_id": account_id}
        )
        total = count_result.scalar()

    await engine.dispose()
    return {"sessions": sessions, "total": total}


# ---------------------------------------------------------------------------
# GET /sessions/{session_id}/status
# ---------------------------------------------------------------------------

@router.get("/sessions/{session_id}/status")
async def get_status(session_id: str, account_id: str = Depends(verify_api_key)):
    engine = _get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT status, completed_at FROM sessions WHERE id = :id AND account_id = :account_id"),
            {"id": session_id, "account_id": account_id}
        )
        row = result.fetchone()

    await engine.dispose()
    if not row:
        return {"error": "Session not found", "session_id": session_id}

    return {
        "session_id": session_id,
        "status": row[0],
        "progress_pct": 100 if row[0] == "ready" else 50 if row[0] == "processing" else 0,
        "completed_at": row[1].isoformat() if row[1] else None,
    }


# ---------------------------------------------------------------------------
# GET /sessions/{session_id}/synthesis
# ---------------------------------------------------------------------------

@router.get("/sessions/{session_id}/synthesis")
async def get_synthesis(session_id: str, account_id: str = Depends(verify_api_key)):
    engine = _get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT name, status, themes, quote_count FROM sessions WHERE id = :id AND account_id = :account_id"),
            {"id": session_id, "account_id": account_id}
        )
        row = result.fetchone()

    await engine.dispose()
    if not row:
        return {"error": "Session not found", "session_id": session_id}

    name, status, themes_json, quote_count = row

    if status != "ready" or not themes_json:
        return {
            "session_id": session_id,
            "session_name": name,
            "status": status,
            "message": "Synthesis not yet available.",
        }

    data = themes_json if isinstance(themes_json, dict) else json.loads(themes_json)
    quotes = data.get("quotes", [])
    findings = data.get("findings", [])

    for finding in findings:
        sq = finding.get("supporting_quote", {})
        if sq:
            sq["session_id"] = session_id

    return {
        "session_id": session_id,
        "session_name": name,
        "participant_count": data.get("participant_count", 0),
        "quote_count": quote_count or len(quotes),
        "themes": data.get("themes", []),
        "key_findings": findings,
    }


# ---------------------------------------------------------------------------
# GET /sessions/{session_id}/quotes
# ---------------------------------------------------------------------------

@router.get("/sessions/{session_id}/quotes")
async def get_quotes(session_id: str, theme_id: str = None, account_id: str = Depends(verify_api_key)):
    engine = _get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT themes, status FROM sessions WHERE id = :id AND account_id = :account_id"),
            {"id": session_id, "account_id": account_id}
        )
        row = result.fetchone()

    await engine.dispose()
    if not row:
        return {"error": "Session not found", "session_id": session_id}

    themes_json, status = row
    if status != "ready" or not themes_json:
        return {"quotes": [], "total": 0, "message": "Session not ready yet."}

    data = themes_json if isinstance(themes_json, dict) else json.loads(themes_json)
    raw_quotes = data.get("quotes", [])
    themes = data.get("themes", [])
    label_to_id = {t.get("label", ""): f"theme_{str(i).zfill(3)}" for i, t in enumerate(themes)}

    quotes = []
    for i, q in enumerate(raw_quotes):
        t_label = q.get("theme", "")
        t_id = label_to_id.get(t_label, "theme_misc")
        quotes.append({
            "id": f"q_{str(i).zfill(3)}",
            "text": q.get("text", q.get("quote", "")),
            "speaker": q.get("speaker", "Unknown"),
            "timestamp": q.get("timestamp", ""),
            "theme_id": t_id,
            "theme_label": t_label,
        })

    if theme_id:
        quotes = [q for q in quotes if q["theme_id"] == theme_id]

    return {"quotes": quotes, "total": len(quotes)}


# ---------------------------------------------------------------------------
# POST /search
# ---------------------------------------------------------------------------

@router.post("/search")
async def search_research(request: Request, account_id: str = Depends(verify_api_key)):
    body = await request.json()
    query = body.get("query", "").lower()
    project_id = body.get("project_id")

    engine = _get_engine()
    async with engine.connect() as conn:
        if project_id:
            result = await conn.execute(
                text("""
                    SELECT id, name, themes, created_at FROM sessions
                    WHERE status = 'ready' AND themes IS NOT NULL
                      AND account_id = :account_id AND project = :project_id
                    ORDER BY created_at DESC
                """),
                {"account_id": account_id, "project_id": project_id}
            )
        else:
            result = await conn.execute(
                text("""
                    SELECT id, name, themes, created_at FROM sessions
                    WHERE status = 'ready' AND themes IS NOT NULL
                      AND account_id = :account_id
                    ORDER BY created_at DESC
                """),
                {"account_id": account_id}
            )
        rows = result.fetchall()

    await engine.dispose()

    results = []
    for row in rows:
        sess_id, sess_name, themes_json, created_at = row
        data = themes_json if isinstance(themes_json, dict) else json.loads(themes_json)
        for q in data.get("quotes", []):
            text_val = q.get("text", q.get("quote", ""))
            if query:
                words = query.split()
                if not any(w in text_val.lower() for w in words):
                    continue
            results.append({
                "relevance_score": 1.0,
                "quote": text_val,
                "speaker": q.get("speaker", "Unknown"),
                "timestamp": q.get("timestamp", ""),
                "session_id": str(sess_id),
                "session_name": sess_name,
                "date": created_at.date().isoformat() if created_at else None,
                "synthesis_summary": "",
            })

    return {"query": query, "results": results[:20], "total": len(results)}


# ---------------------------------------------------------------------------
# POST /sessions/{session_id}/report  (stub)
# ---------------------------------------------------------------------------

@router.post("/sessions/{session_id}/report")
async def generate_report(session_id: str, request: Request, account_id: str = Depends(verify_api_key)):
    from services.report import render_markdown, render_pdf, save_report
    from fastapi.responses import FileResponse

    body = await request.json()
    fmt = body.get("format", "markdown")

    engine = _get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT name, status, themes FROM sessions WHERE id = :id AND account_id = :account_id"),
            {"id": session_id, "account_id": account_id}
        )
        row = result.fetchone()
    await engine.dispose()

    if not row:
        return {"error": "Session not found"}

    name, status, themes_json = row
    if status != "ready" or not themes_json:
        return {"error": "Session not ready yet"}

    data = themes_json if isinstance(themes_json, dict) else json.loads(themes_json)

    if fmt == "pdf":
        path = render_pdf(session_id, name, data)
        return FileResponse(str(path), media_type="application/pdf", filename=f"{session_id}.pdf")
    else:
        content = render_markdown(session_id, name, data)
        path = save_report(session_id, content, "markdown")
        return {"session_id": session_id, "format": "markdown", "report": content, "saved_to": str(path)}
