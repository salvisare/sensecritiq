"""
SenseCritiq — Real session endpoints.

Handles all session lifecycle: upload, list, status, synthesis, quotes,
search, and report generation.  Registered before stubs.py in main.py
so these real routes take precedence.

Auth: Bearer scq_live_* API keys → SHA-256 verified against api_keys table.
      Bearer Clerk JWT → verified via portal._verify_clerk_token.
"""

import io
import json
import os
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import boto3
from databases import Database
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse

router = APIRouter()


# ── Auth (reuse from chat.py) ─────────────────────────────────────────────────
async def get_account_id(request: Request) -> str:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    token = auth_header[len("Bearer "):]
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
    from middleware.auth import verify_api_key
    return await verify_api_key(request)


# ── R2 helper ─────────────────────────────────────────────────────────────────

def _get_s3_client():
    """Return a boto3 S3 client pointed at R2, or None if not configured."""
    endpoint = os.environ.get("R2_ENDPOINT_URL")
    key_id   = os.environ.get("R2_ACCESS_KEY_ID")
    secret   = os.environ.get("R2_SECRET_ACCESS_KEY")
    if not (endpoint and key_id and secret):
        return None
    from botocore.config import Config as BotoConfig
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        config=BotoConfig(signature_version="s3v4"),
    )


# ── POST /v1/sessions ─────────────────────────────────────────────────────────

@router.post("/sessions")
async def create_session(
    request: Request,
    background_tasks: BackgroundTasks,
    account_id: str = Depends(get_account_id),
):
    """
    Upload a research artifact and create a new processing session.
    Accepts multipart/form-data with:
        file         — the research artifact (audio, video, text, pdf, docx)
        session_name — human-readable name
        project_id   — optional project grouping
        tags         — optional JSON array of strings
    """
    db: Database = request.app.state.db

    # ── Parse multipart form ──────────────────────────────────────────────────
    form = await request.form()
    session_name: str = form.get("session_name", "Unnamed session")
    project_id: Optional[str] = form.get("project_id") or None
    tags_raw: str = form.get("tags", "[]")
    file = form.get("file")

    if not file:
        raise HTTPException(status_code=400, detail="No file provided — include 'file' in multipart form")

    filename: str = getattr(file, "filename", "upload")
    file_bytes: bytes = await file.read() if hasattr(file, "read") else b""

    # ── Parse tags ────────────────────────────────────────────────────────────
    try:
        tags = json.loads(tags_raw) if tags_raw else []
    except Exception:
        tags = []

    # ── Create session record ─────────────────────────────────────────────────
    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    # Use only the core columns that definitely exist in the DB.
    # project and tags are set via a follow-up UPDATE (they may not exist yet).
    await db.execute(
        """INSERT INTO sessions
               (id, account_id, name, status, created_at)
           VALUES
               (:id, :account_id, :name, 'queued', :created_at)""",
        {
            "id":         session_id,
            "account_id": account_id,
            "name":       session_name,
            "created_at": now,
        },
    )

    # Best-effort optional fields (columns may not exist on older DB installs)
    if project_id:
        try:
            await db.execute(
                "UPDATE sessions SET project = :p WHERE id = :id",
                {"p": project_id, "id": session_id},
            )
        except Exception:
            pass

    # ── Upload file to R2 ─────────────────────────────────────────────────────
    s3_key = f"uploads/{account_id}/{session_id}/{filename}"
    bucket = os.environ.get("R2_BUCKET_NAME", "sensecritiq-uploads")
    s3     = _get_s3_client()

    if s3 and file_bytes:
        try:
            s3.put_object(Bucket=bucket, Key=s3_key, Body=file_bytes)
            await db.execute(
                "UPDATE sessions SET file_s3_key = :key WHERE id = :id",
                {"key": s3_key, "id": session_id},
            )
        except Exception as upload_err:
            print(f"[sessions] R2 upload failed for {session_id}: {upload_err}")
            # Don't abort — inline processing still possible

    # ── Trigger processing pipeline ───────────────────────────────────────────
    background_tasks.add_task(
        _trigger_pipeline,
        session_id=session_id,
        s3_key=s3_key,
        filename=filename,
        file_bytes=file_bytes,
        db_url=os.environ.get("DATABASE_URL", ""),
    )

    return {
        "session_id":               session_id,
        "session_name":             session_name,
        "status":                   "queued",
        "estimated_processing_time": "3–5 minutes",
        "created_at":               now.isoformat(),
    }


async def _trigger_pipeline(
    session_id: str,
    s3_key: str,
    filename: str,
    file_bytes: bytes,
    db_url: str,
):
    """
    Background task: try to spawn Modal pipeline if credentials are present;
    otherwise fall back to inline processing.
    The inline path handles text/transcript files directly (no AssemblyAI needed).
    """
    # ── Try Modal only if credentials are explicitly configured ───────────────
    if os.environ.get("MODAL_TOKEN_ID") and os.environ.get("MODAL_TOKEN_SECRET"):
        try:
            import modal
            fn = modal.Function.from_name("sensecritiq-pipeline", "process_session")
            fn.spawn(session_id, s3_key, filename)
            print(f"[sessions] Modal pipeline spawned for {session_id}")
            return
        except Exception as modal_err:
            print(f"[sessions] Modal spawn failed ({modal_err}), falling back to inline")
    else:
        print(f"[sessions] Modal not configured — running inline pipeline for {session_id}")

    # ── Inline pipeline ───────────────────────────────────────────────────────
    await _run_inline_pipeline(session_id, filename, file_bytes, db_url)


async def _run_inline_pipeline(
    session_id: str,
    filename: str,
    file_bytes: bytes,
    db_url: str,
):
    """
    Inline processing fallback for text-based research files.
    Skips AssemblyAI for .txt/.md files (already transcripts).
    For audio/video files, marks as failed with a helpful message.
    """
    from databases import Database as DB
    import anthropic

    database = DB(db_url)
    await database.connect()

    try:
        await database.execute(
            "UPDATE sessions SET status = 'processing' WHERE id = :id",
            {"id": session_id},
        )

        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        audio_extensions = {"mp3", "mp4", "wav", "m4a", "aac", "ogg", "flac", "webm"}

        if ext in audio_extensions:
            # ── Audio: try AssemblyAI ─────────────────────────────────────────
            aai_key = os.environ.get("ASSEMBLYAI_API_KEY")
            if not aai_key:
                raise RuntimeError(
                    "Audio/video files require AssemblyAI. "
                    "Set ASSEMBLYAI_API_KEY in Railway environment variables, "
                    "or upload a .txt transcript instead."
                )
            transcript_text = await _transcribe_assemblyai(file_bytes, filename, aai_key)
        else:
            # ── Text/PDF/DOCX: extract text directly ──────────────────────────
            transcript_text = _extract_text(file_bytes, ext)

        if not transcript_text.strip():
            raise RuntimeError("Could not extract any text from the uploaded file")

        print(f"[pipeline] {session_id} — {len(transcript_text):,} chars of transcript")

        # ── Filter with Claude Haiku ──────────────────────────────────────────
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

        FILTER_SYSTEM = (
            "You are a transcript cleaner for UX research. "
            "Remove: consent scripts, pleasantries, scheduling talk, facilitator instructions, "
            "technical setup issues, and any non-research meta-content. "
            "Keep: all participant statements about their experience, opinions, confusion, "
            "delight, or behaviour. Keep speaker labels and timestamps exactly as-is. "
            "Return ONLY the cleaned transcript text — nothing else."
        )

        MAX_FILTER_CHARS = 80_000
        chunks = [transcript_text[i:i + MAX_FILTER_CHARS] for i in range(0, len(transcript_text), MAX_FILTER_CHARS)]
        filtered_parts = []
        for i, chunk in enumerate(chunks):
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=4096,
                system=FILTER_SYSTEM,
                messages=[{"role": "user", "content": chunk}],
            )
            filtered_parts.append(resp.content[0].text)

        filtered_transcript = "\n".join(filtered_parts)
        print(f"[pipeline] {session_id} — filtered to {len(filtered_transcript):,} chars")

        # ── Synthesise with Claude Sonnet ─────────────────────────────────────
        SYNTHESIS_SYSTEM = """You are a UX research analyst. You receive a cleaned research transcript
and extract structured insights.

Output ONLY valid JSON — no markdown, no explanation, no wrapper text.

Schema:
{
  "themes": [
    {
      "id": "theme_001",
      "label": "Short descriptive label (4-8 words)",
      "description": "1-2 sentence summary of this theme",
      "severity": "high | medium | low | positive",
      "quote_count": <int>
    }
  ],
  "key_findings": [
    {
      "finding": "A single, specific, actionable finding (1 sentence)",
      "supporting_quote": {
        "text": "Verbatim quote from transcript",
        "speaker": "P1",
        "timestamp": "HH:MM:SS"
      }
    }
  ],
  "quotes": [
    {
      "text": "Verbatim quote",
      "speaker": "P1",
      "timestamp_sec": <int seconds>,
      "theme_label": "Label matching one of the themes above"
    }
  ]
}

Rules:
- Every finding MUST cite a verbatim quote with speaker + timestamp.
- Quotes must be exact words from the transcript — never paraphrased.
- Aim for 3-7 themes and 5-15 key findings depending on session length.
- Ignore pleasantries, filler, consent scripts, and facilitator meta-commentary."""

        synthesis_resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            system=SYNTHESIS_SYSTEM,
            messages=[{
                "role": "user",
                "content": (
                    f"Research session transcript:\n\n{filtered_transcript}\n\n"
                    "Extract themes, key findings, and notable quotes as JSON."
                ),
            }],
        )

        synthesis_text = synthesis_resp.content[0].text.strip()
        if synthesis_text.startswith("```"):
            lines = synthesis_text.split("\n")
            synthesis_text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
        synthesis = json.loads(synthesis_text)

        themes   = synthesis.get("themes", [])
        findings = synthesis.get("key_findings", [])
        quotes   = synthesis.get("quotes", [])
        print(f"[pipeline] {session_id} — {len(themes)} themes, {len(findings)} findings, {len(quotes)} quotes")

        # ── Fetch account_id ──────────────────────────────────────────────────
        row = await database.fetch_one(
            "SELECT account_id FROM sessions WHERE id = :id", {"id": session_id}
        )
        account_id = str(row["account_id"]) if row else None

        # ── Store quotes ──────────────────────────────────────────────────────
        for q in quotes:
            qid = str(uuid.uuid4())
            await database.execute(
                """INSERT INTO quotes
                       (id, session_id, account_id, text, speaker, timestamp_sec, theme_label, embedding_model)
                   VALUES
                       (:id, :sid, :aid, :text, :speaker, :ts, :theme_label, :model)
                   ON CONFLICT DO NOTHING""",
                {
                    "id":          qid,
                    "sid":         session_id,
                    "aid":         account_id,
                    "text":        q.get("text", ""),
                    "speaker":     q.get("speaker"),
                    "ts":          q.get("timestamp_sec"),
                    "theme_label": q.get("theme_label"),
                    "model":       "none",
                },
            )

        # ── Mark session ready ────────────────────────────────────────────────
        completed_at = datetime.now(timezone.utc)
        await database.execute(
            """UPDATE sessions
               SET status = 'ready', themes = :themes, findings = :findings,
                   quote_count = :qcount, completed_at = :completed_at
               WHERE id = :id""",
            {
                "themes":       json.dumps(themes),
                "findings":     json.dumps(findings),
                "qcount":       len(quotes),
                "completed_at": completed_at,
                "id":           session_id,
            },
        )
        print(f"[pipeline] {session_id} — status: ready ✓")

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[pipeline] {session_id} — FAILED: {e}\n{tb}")
        # Store the error in findings JSONB so it's inspectable via API
        try:
            await database.execute(
                """UPDATE sessions
                   SET status = 'failed',
                       findings = :err
                   WHERE id = :id""",
                {
                    "err": json.dumps([{"error": str(e), "traceback": tb}]),
                    "id": session_id,
                },
            )
        except Exception as db_err:
            print(f"[pipeline] could not write failure to DB: {db_err}")
        raise
    finally:
        await database.disconnect()


def _extract_text(file_bytes: bytes, ext: str) -> str:
    """Extract plain text from uploaded file bytes."""
    if ext in ("txt", "md", ""):
        # Detect encoding
        try:
            return file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return file_bytes.decode("latin-1", errors="replace")

    if ext == "pdf":
        try:
            import pdfminer.high_level
            return pdfminer.high_level.extract_text(io.BytesIO(file_bytes))
        except ImportError:
            pass
        # Fallback: raw text extraction
        text = file_bytes.decode("latin-1", errors="replace")
        # Filter printable ASCII
        return "".join(c if (32 <= ord(c) < 127 or c in "\n\r\t") else " " for c in text)

    if ext in ("docx",):
        try:
            import docx
            import zipfile
            doc = docx.Document(io.BytesIO(file_bytes))
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except ImportError:
            pass
        # Fallback: extract XML text from zip
        try:
            import zipfile, re
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                xml = z.read("word/document.xml").decode("utf-8")
            return re.sub(r"<[^>]+>", " ", xml)
        except Exception:
            return file_bytes.decode("latin-1", errors="replace")

    # Unknown format: try UTF-8 decode
    try:
        return file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return file_bytes.decode("latin-1", errors="replace")


async def _transcribe_assemblyai(file_bytes: bytes, filename: str, api_key: str) -> str:
    """Upload to AssemblyAI and poll until transcript is ready."""
    import httpx, time

    headers_auth = {"authorization": api_key}
    headers_json = {"authorization": api_key, "content-type": "application/json"}

    with httpx.Client(timeout=120) as http:
        # Upload audio bytes
        up = http.post(
            "https://api.assemblyai.com/v2/upload",
            headers=headers_auth,
            content=file_bytes,
        )
        up.raise_for_status()
        audio_url = up.json()["upload_url"]

        # Submit job
        job = http.post(
            "https://api.assemblyai.com/v2/transcript",
            headers=headers_json,
            json={
                "audio_url": audio_url,
                "speech_model": "universal",
                "speaker_labels": True,
                "punctuate": True,
                "format_text": True,
            },
        )
        job.raise_for_status()
        job_id = job.json()["id"]

        # Poll
        while True:
            poll = http.get(
                f"https://api.assemblyai.com/v2/transcript/{job_id}",
                headers=headers_auth,
            )
            poll.raise_for_status()
            result = poll.json()
            if result["status"] == "completed":
                break
            elif result["status"] == "error":
                raise RuntimeError(f"AssemblyAI error: {result.get('error')}")
            time.sleep(3)

    # Build diarised text
    lines = []
    utterances = result.get("utterances") or []
    if utterances:
        for utt in utterances:
            ts = utt.get("start", 0) // 1000
            h, r = divmod(ts, 3600)
            m, s = divmod(r, 60)
            lines.append(f"Speaker {utt['speaker']} [{h:02d}:{m:02d}:{s:02d}]: {utt['text']}")
    else:
        lines = [result.get("text", "")]

    return "\n".join(lines)


# ── GET /v1/sessions ──────────────────────────────────────────────────────────

@router.get("/sessions")
async def list_sessions(
    request: Request,
    limit: int = 20,
    offset: int = 0,
    project_id: Optional[str] = None,
    account_id: str = Depends(get_account_id),
):
    """List research sessions for the authenticated account."""
    db: Database = request.app.state.db

    q = """
        SELECT id, name, project AS project_id, status,
               quote_count, created_at, completed_at
        FROM sessions
        WHERE account_id = :aid
    """
    params: dict = {"aid": account_id}

    if project_id:
        q += " AND project = :pid"
        params["pid"] = project_id

    q += " ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
    params["limit"]  = limit
    params["offset"] = offset

    rows = await db.fetch_all(q, params)

    sessions = []
    for r in rows:
        sessions.append({
            "id":           str(r["id"]),
            "name":         r["name"] or "Unnamed session",
            "project":      r["project_id"],
            "status":       r["status"],
            "quote_count":  r["quote_count"] or 0,
            "created_at":   r["created_at"].isoformat() if r["created_at"] else None,
            "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
        })

    # Count total
    count_row = await db.fetch_one(
        "SELECT COUNT(*) as cnt FROM sessions WHERE account_id = :aid",
        {"aid": account_id},
    )
    total = count_row["cnt"] if count_row else len(sessions)

    return {"sessions": sessions, "total": total}


# ── GET /v1/sessions/{session_id}/status ──────────────────────────────────────

@router.get("/sessions/{session_id}/status")
async def get_session_status(
    session_id: str,
    request: Request,
    account_id: str = Depends(get_account_id),
):
    """Get the processing status of a session."""
    db: Database = request.app.state.db

    row = await db.fetch_one(
        """SELECT id, status, quote_count, created_at, completed_at
           FROM sessions
           WHERE id = :id AND account_id = :aid""",
        {"id": session_id, "aid": account_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")

    status_order = {"queued": 0, "processing": 50, "ready": 100, "failed": 0}
    progress = status_order.get(row["status"], 0)

    response: dict = {
        "session_id":   str(row["id"]),
        "status":       row["status"],
        "progress_pct": progress,
        "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
    }

    # If failed, surface the stored error so callers can see why
    if row["status"] == "failed":
        try:
            err_row = await db.fetch_one(
                "SELECT findings FROM sessions WHERE id = :id",
                {"id": session_id},
            )
            if err_row and err_row["findings"]:
                findings = err_row["findings"]
                if isinstance(findings, str):
                    findings = json.loads(findings)
                if isinstance(findings, list) and findings and "error" in findings[0]:
                    response["error"] = findings[0].get("error", "Unknown error")
        except Exception:
            pass

    return response


# ── GET /v1/sessions/{session_id}/synthesis ───────────────────────────────────

@router.get("/sessions/{session_id}/synthesis")
async def get_synthesis(
    session_id: str,
    request: Request,
    account_id: str = Depends(get_account_id),
):
    """Retrieve synthesised themes and key findings for a completed session."""
    db: Database = request.app.state.db

    row = await db.fetch_one(
        """SELECT id, name, status, themes, findings, quote_count
           FROM sessions
           WHERE id = :id AND account_id = :aid""",
        {"id": session_id, "aid": account_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    if row["status"] not in ("ready",):
        raise HTTPException(
            status_code=400,
            detail=f"Session not ready (status: {row['status']}). "
                   f"Use get_session_status to poll for completion.",
        )

    themes = row["themes"] or []
    findings = row["findings"] or []
    if isinstance(themes, str):
        try: themes = json.loads(themes)
        except: themes = []
    if isinstance(findings, str):
        try: findings = json.loads(findings)
        except: findings = []

    # Count distinct non-null speakers from quotes table
    speaker_row = await db.fetch_one(
        "SELECT COUNT(DISTINCT speaker) AS cnt FROM quotes WHERE session_id = :sid AND speaker IS NOT NULL",
        {"sid": session_id},
    )
    participant_count = int(speaker_row["cnt"]) if speaker_row else 0

    return {
        "session_id":        str(row["id"]),
        "session_name":      row["name"] or "Unnamed session",
        "participant_count": participant_count,
        "quote_count":       row["quote_count"] or 0,
        "themes":            themes,
        "key_findings":      findings,
    }


# ── GET /v1/sessions/{session_id}/quotes ──────────────────────────────────────

@router.get("/sessions/{session_id}/quotes")
async def get_quotes(
    session_id: str,
    request: Request,
    theme_id: Optional[str] = None,
    account_id: str = Depends(get_account_id),
):
    """Retrieve verbatim quotes from a session, optionally filtered by theme."""
    db: Database = request.app.state.db

    # Verify session belongs to this account
    session_row = await db.fetch_one(
        "SELECT id FROM sessions WHERE id = :id AND account_id = :aid",
        {"id": session_id, "aid": account_id},
    )
    if not session_row:
        raise HTTPException(status_code=404, detail="Session not found")

    q = """
        SELECT id, text, speaker, timestamp_sec, theme_label
        FROM quotes
        WHERE session_id = :sid
    """
    params: dict = {"sid": session_id}

    if theme_id:
        q += " AND theme_label ILIKE :theme"
        params["theme"] = f"%{theme_id}%"

    q += " ORDER BY timestamp_sec ASC NULLS LAST LIMIT 200"
    rows = await db.fetch_all(q, params)

    quotes = []
    for r in rows:
        ts = r["timestamp_sec"]
        ts_str = None
        if ts is not None:
            h, rem = divmod(ts, 3600)
            m, s   = divmod(rem, 60)
            ts_str = f"{h:02d}:{m:02d}:{s:02d}"
        quotes.append({
            "id":          str(r["id"]),
            "text":        r["text"],
            "speaker":     r["speaker"],
            "timestamp":   ts_str,
            "theme_id":    r["theme_label"],
            "theme_label": r["theme_label"],
        })

    return {"quotes": quotes, "total": len(quotes)}


# ── POST /v1/search ───────────────────────────────────────────────────────────
@router.post("/search")
async def search_research(
    request: Request,
    account_id: str = Depends(get_account_id),
):
    """
    Full-text ILIKE search across quotes and session names.
    Mounted at /v1/search via include_router(prefix='/v1').

    Falls back gracefully when pgvector is unavailable.
    Searches:
    1. quotes.text ILIKE '%query%'        — verbatim quote content
    2. sessions.name / findings ILIKE     — session-level match
    Returns deduplicated results ranked by match type.
    """
    body = await request.json()
    query: str = body.get("query", "").strip()
    project_id: Optional[str] = body.get("project_id")
    date_from: Optional[str] = body.get("date_from")
    date_to: Optional[str] = body.get("date_to")

    if not query:
        return {"query": query, "results": [], "total": 0}

    db: Database = request.app.state.db
    pattern = f"%{query}%"
    results = []

    # ── 1. Search quotes table ────────────────────────────────────────────
    try:
        q = """
            SELECT
                qt.id AS quote_id,
                qt.text,
                qt.speaker,
                qt.timestamp_sec,
                qt.theme_label,
                s.id   AS session_id,
                s.name AS session_name,
                s.created_at
            FROM quotes qt
            JOIN sessions s ON s.id = qt.session_id
            WHERE qt.account_id = :aid
              AND qt.text ILIKE :pattern
        """
        params: dict = {"aid": account_id, "pattern": pattern}

        if project_id:
            q += " AND s.project_id = :pid"
            params["pid"] = project_id
        if date_from:
            q += " AND s.created_at >= :dfrom"
            params["dfrom"] = date_from
        if date_to:
            q += " AND s.created_at <= :dto"
            params["dto"] = date_to

        q += " ORDER BY s.created_at DESC LIMIT 20"
        rows = await db.fetch_all(q, params)

        for r in rows:
            ts = r["timestamp_sec"]
            timestamp_str = None
            if ts is not None:
                h, remainder = divmod(ts, 3600)
                m, s_ = divmod(remainder, 60)
                timestamp_str = f"{h:02d}:{m:02d}:{s_:02d}"

            results.append({
                "relevance_score": 0.90,
                "quote": r["text"],
                "speaker": r["speaker"],
                "timestamp": timestamp_str,
                "session_id": str(r["session_id"]),
                "session_name": r["session_name"],
                "date": str(r["created_at"])[:10] if r["created_at"] else None,
                "theme": r["theme_label"],
                "synthesis_summary": None,
            })
    except Exception as e:
        print(f"[search] quotes query failed: {e}")

    # ── 2. Search session names (add sessions without quote hits) ─────────
    try:
        sq = """
            SELECT id, name, themes, findings, created_at
            FROM sessions
            WHERE account_id = :aid
              AND (name ILIKE :pattern
                   OR themes::text ILIKE :pattern
                   OR findings::text ILIKE :pattern)
        """
        sparams: dict = {"aid": account_id, "pattern": pattern}
        if date_from:
            sq += " AND created_at >= :dfrom"
            sparams["dfrom"] = date_from
        if date_to:
            sq += " AND created_at <= :dto"
            sparams["dto"] = date_to
        sq += " ORDER BY created_at DESC LIMIT 10"
        srows = await db.fetch_all(sq, sparams)

        seen_sessions = {r["session_id"] for r in results}
        for r in srows:
            sid = str(r["id"])
            if sid in seen_sessions:
                continue
            # Pull a representative quote from this session
            findings = r["findings"]
            if isinstance(findings, str):
                try:
                    findings = json.loads(findings)
                except Exception:
                    findings = []
            first_quote = ""
            if findings and isinstance(findings, list):
                sq_obj = findings[0].get("supporting_quote", {}) if findings else {}
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
            })
    except Exception as e:
        print(f"[search] sessions query failed: {e}")

    # Sort by relevance score desc
    results.sort(key=lambda x: x["relevance_score"], reverse=True)

    return {"query": query, "results": results, "total": len(results)}


# ── POST /v1/sessions/{session_id}/report ─────────────────────────────────────
@router.post("/sessions/{session_id}/report")
async def generate_report(
    session_id: str,
    request: Request,
    account_id: str = Depends(get_account_id),
):
    """
    Generate a Markdown or PDF report for a completed session.
    Uploads the report to R2 and returns a pre-signed URL (1 hour TTL).
    """
    body = await request.json()
    fmt: str = body.get("format", "markdown")

    db: Database = request.app.state.db

    # Fetch session
    row = await db.fetch_one(
        """SELECT id, name, status, themes, findings, quote_count
           FROM sessions WHERE id = :id AND account_id = :aid""",
        {"id": session_id, "aid": account_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    if row["status"] != "ready":
        raise HTTPException(
            status_code=400,
            detail=f"Session is not ready (status: {row['status']})"
        )

    # Parse stored JSON
    themes = row["themes"] or []
    findings = row["findings"] or []
    if isinstance(themes, str):
        try:
            themes = json.loads(themes)
        except Exception:
            themes = []
    if isinstance(findings, str):
        try:
            findings = json.loads(findings)
        except Exception:
            findings = []

    # Fetch quotes
    quote_rows = await db.fetch_all(
        """SELECT text, speaker, timestamp_sec, theme_label
           FROM quotes WHERE session_id = :sid ORDER BY timestamp_sec""",
        {"sid": session_id},
    )
    quotes = []
    for q in quote_rows:
        ts = q["timestamp_sec"]
        ts_str = None
        if ts is not None:
            h, remainder = divmod(ts, 3600)
            m, s_ = divmod(remainder, 60)
            ts_str = f"{h:02d}:{m:02d}:{s_:02d}"
        quotes.append({
            "text": q["text"],
            "speaker": q["speaker"],
            "timestamp": ts_str,
            "theme": q["theme_label"] or "Other",
        })

    # Count distinct speakers from quotes
    speaker_row = await db.fetch_one(
        "SELECT COUNT(DISTINCT speaker) AS cnt FROM quotes WHERE session_id = :sid AND speaker IS NOT NULL",
        {"sid": session_id},
    )
    participant_count = int(speaker_row["cnt"]) if speaker_row else 0

    data = {
        "themes": themes,
        "findings": findings,
        "quotes": quotes,
        "participant_count": participant_count,
    }

    session_name = row["name"] or "Research Session"

    # Notion export — create page via API and return URL directly
    if fmt == "notion":
        try:
            notion_url = await _push_to_notion(session_name, data)
            return {
                "session_id":   session_id,
                "format":       "notion",
                "download_url": notion_url,
                "expires_at":   None,
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Notion export failed: {e}")

    # Generate report content
    if fmt == "pdf":
        report_bytes, ext, content_type = _build_pdf(session_id, session_name, data)
    else:
        md_text = _build_markdown(session_id, session_name, data)
        report_bytes = md_text.encode("utf-8")
        ext = "md"
        content_type = "text/markdown"

    # Upload to R2
    report_key = f"reports/{account_id}/{session_id}.{ext}"
    try:
        from botocore.config import Config as BotoConfig
        s3 = boto3.client(
            "s3",
            endpoint_url=os.environ["R2_ENDPOINT_URL"],
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            config=BotoConfig(signature_version="s3v4"),
        )
        bucket = os.environ["R2_BUCKET_NAME"]
        s3.put_object(
            Bucket=bucket,
            Key=report_key,
            Body=report_bytes,
            ContentType=content_type,
        )
        download_url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": report_key},
            ExpiresIn=3600,
        )
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    except Exception as e:
        print(f"[report] R2 upload failed: {e}")
        # Fallback: return report content inline as base64 for markdown
        import base64
        download_url = f"data:{content_type};base64," + base64.b64encode(report_bytes).decode()
        expires_at = None

    return {
        "session_id": session_id,
        "format": fmt,
        "download_url": download_url,
        "expires_at": expires_at,
    }


# ── Report renderers ──────────────────────────────────────────────────────────

def _build_markdown(session_id: str, session_name: str, data: dict) -> str:
    """Render synthesis data as a Markdown report."""
    themes = data.get("themes", [])
    quotes = data.get("quotes", [])
    findings = data.get("findings", [])
    generated_at = datetime.now(timezone.utc).strftime("%B %d, %Y")

    lines = [
        f"# {session_name}",
        f"**Generated:** {generated_at}  ",
        f"**Session ID:** `{session_id}`  ",
        f"**Quotes captured:** {len(quotes)}",
        "",
        "---",
        "",
        "## Themes",
        "",
    ]

    for i, theme in enumerate(themes, 1):
        label = theme.get("label", f"Theme {i}")
        description = theme.get("description", "")
        severity = theme.get("severity", "")
        icon = {"high": "🔴", "medium": "🟡", "positive": "🟢", "low": "🟢"}.get(severity, "⚪")
        qcount = theme.get("quote_count", "")
        lines.append(f"### {icon} {label}" + (f"  _{qcount} quotes_" if qcount else ""))
        if description:
            lines.append(description)
        lines.append("")

    if findings:
        lines += ["## Key Findings", ""]
        for i, finding in enumerate(findings, 1):
            text = finding.get("finding", finding.get("text", ""))
            sq = finding.get("supporting_quote", {})
            lines.append(f"**{i}. {text}**")
            if sq:
                quote_text = sq.get("text", "")
                speaker = sq.get("speaker", "")
                timestamp = sq.get("timestamp", "")
                if quote_text:
                    lines.append("")
                    lines.append(f'> "{quote_text}"')
                    attr = " — ".join(filter(None, [speaker, timestamp]))
                    if attr:
                        lines.append(f"> *{attr}*")
            lines.append("")

    if quotes:
        lines += ["## Verbatim Quotes", ""]
        groups: dict = {}
        for q in quotes:
            t = q.get("theme", "Other")
            groups.setdefault(t, []).append(q)
        for theme_label, tq in groups.items():
            lines.append(f"### {theme_label}")
            lines.append("")
            for q in tq:
                text = q.get("text", "")
                speaker = q.get("speaker", "")
                timestamp = q.get("timestamp", "")
                lines.append(f'> "{text}"')
                attr = " — ".join(filter(None, [speaker, timestamp]))
                if attr:
                    lines.append(f"> *{attr}*")
                lines.append("")

    lines += ["---", "*Generated by SenseCritiq — sensecritiq.com*"]
    return "\n".join(lines)


def _build_pdf(session_id: str, session_name: str, data: dict):
    """Render synthesis data as a PDF using reportlab. Returns (bytes, ext, content_type)."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
        from reportlab.lib.enums import TA_CENTER
    except ImportError:
        # Reportlab not installed — fall back to markdown
        md = _build_markdown(session_id, session_name, data)
        return md.encode("utf-8"), "md", "text/markdown"

    themes = data.get("themes", [])
    quotes = data.get("quotes", [])
    findings = data.get("findings", [])
    generated_at = datetime.now(timezone.utc).strftime("%B %d, %Y")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=20*mm, rightMargin=20*mm,
        topMargin=20*mm, bottomMargin=20*mm,
    )
    base = getSampleStyleSheet()
    title_style = ParagraphStyle("T", parent=base["Title"],
        fontSize=20, textColor=colors.HexColor("#1A202C"), spaceAfter=4)
    meta_style = ParagraphStyle("M", parent=base["Normal"],
        fontSize=9, textColor=colors.HexColor("#718096"), spaceAfter=2)
    h2_style = ParagraphStyle("H2", parent=base["Heading2"],
        fontSize=14, textColor=colors.HexColor("#2D3748"), spaceBefore=14, spaceAfter=6)
    h3_style = ParagraphStyle("H3", parent=base["Heading3"],
        fontSize=11, textColor=colors.HexColor("#2D3748"), spaceBefore=8, spaceAfter=4)
    body_style = ParagraphStyle("B", parent=base["Normal"],
        fontSize=10, textColor=colors.HexColor("#4A5568"), leading=15, spaceAfter=6)
    quote_style = ParagraphStyle("Q", parent=base["Normal"],
        fontSize=10, textColor=colors.HexColor("#2D3748"),
        leftIndent=12, leading=15, borderColor=colors.HexColor("#E2E8F0"),
        borderWidth=2, borderPad=8, backColor=colors.HexColor("#F7FAFC"), spaceAfter=4)
    attribution_style = ParagraphStyle("A", parent=base["Normal"],
        fontSize=9, textColor=colors.HexColor("#718096"), leftIndent=12, spaceAfter=10)
    finding_style = ParagraphStyle("F", parent=base["Normal"],
        fontSize=10, textColor=colors.HexColor("#1A202C"), leading=15,
        spaceBefore=6, spaceAfter=4, fontName="Helvetica-Bold")
    footer_style = ParagraphStyle("FT", parent=base["Normal"],
        fontSize=8, textColor=colors.HexColor("#A0AEC0"), alignment=TA_CENTER)

    story = []
    story.append(Paragraph(session_name, title_style))
    story.append(Paragraph(
        f"Generated {generated_at} &nbsp;|&nbsp; {len(quotes)} quotes captured",
        meta_style,
    ))
    story.append(Spacer(1, 4*mm))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#E2E8F0")))
    story.append(Spacer(1, 4*mm))

    # Themes
    story.append(Paragraph("Themes", h2_style))
    for theme in themes:
        label = theme.get("label", "")
        description = theme.get("description", "")
        severity = theme.get("severity", "")
        cmap = {"high": "#E53E3E", "medium": "#D69E2E", "positive": "#38A169", "low": "#38A169"}
        dot = cmap.get(severity, "#A0AEC0")
        story.append(Paragraph(f'<font color="{dot}">&#9679;</font> <b>{label}</b>', h3_style))
        if description:
            story.append(Paragraph(description, body_style))

    story.append(Spacer(1, 4*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#E2E8F0")))

    # Key Findings
    if findings:
        story.append(Paragraph("Key Findings", h2_style))
        for i, finding in enumerate(findings, 1):
            text = finding.get("finding", finding.get("text", ""))
            sq = finding.get("supporting_quote", {})
            story.append(Paragraph(f"{i}. {text}", finding_style))
            if sq:
                qtext = sq.get("text", "")
                speaker = sq.get("speaker", "")
                ts = sq.get("timestamp", "")
                if qtext:
                    story.append(Paragraph(f'"{qtext}"', quote_style))
                    attr = " — ".join(filter(None, [speaker, ts]))
                    if attr:
                        story.append(Paragraph(attr, attribution_style))
        story.append(Spacer(1, 4*mm))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#E2E8F0")))

    # Quotes
    if quotes:
        story.append(Paragraph("Verbatim Quotes", h2_style))
        groups: dict = {}
        for q in quotes:
            t = q.get("theme", "Other")
            groups.setdefault(t, []).append(q)
        for theme_label, tq in groups.items():
            story.append(Paragraph(theme_label, h3_style))
            for q in tq:
                text = q.get("text", "")
                speaker = q.get("speaker", "")
                ts = q.get("timestamp", "")
                story.append(Paragraph(f'"{text}"', quote_style))
                attr = " — ".join(filter(None, [speaker, ts]))
                if attr:
                    story.append(Paragraph(attr, attribution_style))

    story.append(Spacer(1, 8*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#E2E8F0")))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph("Generated by SenseCritiq — sensecritiq.com", footer_style))

    doc.build(story)
    buf.seek(0)
    return buf.read(), "pdf", "application/pdf"


async def _push_to_notion(session_name: str, data: dict) -> str:
    """Create a Notion page from report data. Returns the Notion page URL."""
    import httpx
    from datetime import timezone

    notion_token = os.environ.get("NOTION_API_KEY", "")
    parent_page_id = os.environ.get("NOTION_PARENT_PAGE_ID", "")
    if not notion_token or not parent_page_id:
        raise ValueError("NOTION_API_KEY and NOTION_PARENT_PAGE_ID env vars are required")

    themes    = data.get("themes", [])
    findings  = data.get("findings", [])
    quotes    = data.get("quotes", [])
    generated = datetime.now(timezone.utc).strftime("%B %d, %Y")

    def txt(content: str, bold=False, color=None) -> dict:
        run: dict = {"type": "text", "text": {"content": content}}
        if bold or color:
            run["annotations"] = {}
            if bold:
                run["annotations"]["bold"] = True
            if color:
                run["annotations"]["color"] = color
        return run

    def heading2(content: str) -> dict:
        return {"object": "block", "type": "heading_2",
                "heading_2": {"rich_text": [txt(content)]}}

    def heading3(content: str) -> dict:
        return {"object": "block", "type": "heading_3",
                "heading_3": {"rich_text": [txt(content)]}}

    def para(rich_text: list) -> dict:
        return {"object": "block", "type": "paragraph",
                "paragraph": {"rich_text": rich_text}}

    def quote_block(content: str) -> dict:
        return {"object": "block", "type": "quote",
                "quote": {"rich_text": [txt(content)]}}

    def callout(content: str, emoji: str = "💡") -> dict:
        return {"object": "block", "type": "callout",
                "callout": {"rich_text": [txt(content)], "icon": {"type": "emoji", "emoji": emoji}}}

    def divider() -> dict:
        return {"object": "block", "type": "divider", "divider": {}}

    # Build blocks
    blocks: list = []

    # Meta
    blocks.append(para([txt(f"Generated: {generated}  |  Quotes: {len(quotes)}", color="gray")]))
    blocks.append(divider())

    # Themes
    if themes:
        blocks.append(heading2("Themes"))
        for i, theme in enumerate(themes, 1):
            label       = theme.get("label", f"Theme {i}")
            description = theme.get("description", "")
            severity    = theme.get("severity", "")
            emoji       = {"high": "🔴", "medium": "🟡", "positive": "🟢", "low": "🟢"}.get(severity, "⚪")
            qcount      = theme.get("quote_count", "")
            header      = f"{emoji} {label}" + (f"  ({qcount} quotes)" if qcount else "")
            blocks.append(heading3(header))
            if description:
                blocks.append(para([txt(description)]))

    # Key Findings
    if findings:
        blocks.append(divider())
        blocks.append(heading2("Key Findings"))
        for i, finding in enumerate(findings, 1):
            text = finding.get("finding", finding.get("text", ""))
            blocks.append(para([txt(f"{i}. {text}", bold=True)]))
            sq = finding.get("supporting_quote", {})
            if sq:
                qt  = sq.get("text", "")
                spk = sq.get("speaker", "")
                ts  = sq.get("timestamp", "")
                if qt:
                    blocks.append(quote_block(f'"{qt}"'))
                    attr = " — ".join(filter(None, [spk, ts]))
                    if attr:
                        blocks.append(para([txt(f"— {attr}", color="gray")]))

    # Quotes by theme
    if quotes:
        blocks.append(divider())
        blocks.append(heading2("Verbatim Quotes"))
        groups: dict = {}
        for q in quotes:
            groups.setdefault(q.get("theme", "Other"), []).append(q)
        for theme_label, tq in groups.items():
            blocks.append(heading3(theme_label))
            for q in tq:
                qtext = q.get("text", "")
                spk   = q.get("speaker", "")
                ts    = q.get("timestamp", "")
                if qtext:
                    blocks.append(quote_block(f'"{qtext}"'))
                    attr = " — ".join(filter(None, [spk, ts]))
                    if attr:
                        blocks.append(para([txt(f"— {attr}", color="gray")]))

    blocks.append(divider())
    blocks.append(para([txt("Generated by SenseCritiq — sensecritiq.com", color="gray")]))

    headers = {
        "Authorization": f"Bearer {notion_token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        # Create the page with first 100 blocks (Notion API limit per request)
        create_resp = await client.post(
            "https://api.notion.com/v1/pages",
            headers=headers,
            json={
                "parent": {"page_id": parent_page_id},
                "properties": {
                    "title": [{"type": "text", "text": {"content": session_name}}]
                },
                "children": blocks[:100],
            },
        )
        create_resp.raise_for_status()
        page_id  = create_resp.json()["id"]
        page_url = create_resp.json()["url"]

        # Append remaining blocks in batches of 100
        remaining = blocks[100:]
        while remaining:
            batch = remaining[:100]
            remaining = remaining[100:]
            append_resp = await client.patch(
                f"https://api.notion.com/v1/blocks/{page_id}/children",
                headers=headers,
                json={"children": batch},
            )
            append_resp.raise_for_status()

    return page_url
