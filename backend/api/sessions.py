"""
SenseCritiq — /sessions endpoint.
Accepts file uploads, creates a session record in PostgreSQL,
and spawns the Modal pipeline worker asynchronously.
"""

import uuid
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text
import os
from middleware.auth import verify_api_key
from fastapi import Depends

router = APIRouter()

UPLOADS_DIR = Path(__file__).parent.parent / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {".mp3", ".mp4", ".wav", ".m4a", ".txt", ".pdf", ".docx"}


def _get_db_session():
    engine = create_async_engine(os.environ["DATABASE_URL"], echo=False)
    return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@router.post("/sessions")
async def create_session(
    file: UploadFile = File(...),
    session_name: str = Form(...),
    project_id: str = Form(default=""),
    tags: str = Form(default="[]"),
    account_id: str = Depends(verify_api_key),
):
    # ── Validate file type ──────────────────────────────────────────────────
    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{suffix}'. Allowed: {', '.join(ALLOWED_EXTENSIONS)}",
        )

    # ── Read file bytes ─────────────────────────────────────────────────────
    file_bytes = await file.read()
    if len(file_bytes) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    # ── Generate session ID and upload to R2 ────────────────────────────────
    session_id = str(uuid.uuid4())
    r2_key = f"uploads/{session_id}{suffix}"
    try:
        from services.storage import upload_file, content_type_for
        upload_file(file_bytes, r2_key, content_type_for(file.filename))
        print(f"[{session_id}] uploaded to R2: {r2_key}")
    except Exception as e:
        print(f"[{session_id}] R2 upload failed, falling back to local: {e}")
        local_path = UPLOADS_DIR / f"{session_id}{suffix}"
        local_path.write_bytes(file_bytes)
        r2_key = str(local_path)

    # ── Parse tags ──────────────────────────────────────────────────────────
    try:
        parsed_tags = json.loads(tags) if tags else []
    except json.JSONDecodeError:
        parsed_tags = []

    # ── Create session record in DB ─────────────────────────────────────────
    AsyncSessionLocal = _get_db_session()
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("""
                INSERT INTO sessions (id, account_id, name, project, tags, status, file_s3_key, created_at)
                VALUES (:id, :account_id, :name, :project, :tags, 'queued', :file_key, :created_at)
            """),
            {
                "id": session_id,
                "account_id": account_id,
                "name": session_name,
                "project": project_id or None,
                "tags": parsed_tags,
                "file_key": r2_key,
                "created_at": datetime.now(timezone.utc),
            },
        )
        await db.commit()

    # ── Spawn Modal pipeline worker ─────────────────────────────────────────
    try:
        import modal
        process_session = modal.Function.from_name("sensecritiq-pipeline", "process_session")
        await process_session.spawn.aio(session_id, file_bytes, file.filename, session_name)
        print(f"[{session_id}] pipeline spawned via Modal")
    except Exception as e:
        # Don't fail the request if Modal spawn fails — session is queued in DB
        print(f"[{session_id}] Modal spawn failed (will need manual retry): {e}")

    return {
        "session_id": session_id,
        "session_name": session_name,
        "status": "queued",
        "estimated_processing_time": "3-5 minutes",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
