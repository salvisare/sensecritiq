"""
SenseCritiq — Modal async pipeline worker.

Flow:
  1. Receive file bytes + session metadata
  2. AssemblyAI transcription with speaker diarisation
  3. Claude Haiku  — filter meta content (~70% removed)
  4. Claude Sonnet — extract themes, quotes, findings (structured JSON)
  5. Update session record in PostgreSQL → status: ready

Local dev:  modal run workers/pipeline.py --session-id xxx (after deploying stubs)
Cloud:      modal deploy workers/pipeline.py
"""

import os
import sys
import json
import tempfile
from sqlalchemy.ext.asyncio import create_async_engine, AsyncConnection
from sqlalchemy import text
import assemblyai as aai
import modal
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Modal app definition
# ---------------------------------------------------------------------------

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install([
        "assemblyai>=0.40.0",
        "anthropic==0.40.0",
        "sqlalchemy==2.0.36",
        "asyncpg==0.30.0",
        "python-dotenv==1.0.1",
    ])
    .add_local_python_source("services")
)

app = modal.App("sensecritiq-pipeline", image=image)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_transcript(utterances) -> str:
    """Format AssemblyAI utterances into Speaker A [MM:SS]: text lines."""
    lines = []
    for u in utterances:
        ms = u.start
        total_seconds = ms // 1000
        ts = f"{total_seconds // 60:02d}:{total_seconds % 60:02d}"
        lines.append(f"Speaker {u.speaker} [{ts}]: {u.text}")
    return "\n".join(lines)


async def _update_session(conn, session_id: str, status: str, **fields):
    """Update a session record in PostgreSQL."""
    params = {"session_id": session_id, "status": status}
    set_clauses = ["status = :status"]
    for key, val in fields.items():
        set_clauses.append(f"{key} = :{key}")
        params[key] = val
    if status == "ready":
        set_clauses.append("completed_at = :completed_at")
        params["completed_at"] = datetime.now(timezone.utc)
    query = f"UPDATE sessions SET {', '.join(set_clauses)} WHERE id = :session_id"
    await conn.execute(text(query), params)
    await conn.commit()


# ---------------------------------------------------------------------------
# Main pipeline function
# ---------------------------------------------------------------------------

@app.function(
    secrets=[modal.Secret.from_name("sensecritiq-secrets")],
    timeout=600,   # 10 min — covers long recordings
    retries=1,
)
async def process_session(
    session_id: str,
    file_bytes: bytes,
    filename: str,
    session_name: str,
):
    """
    Full processing pipeline for one research session.
    Called by the FastAPI sessions endpoint after upload.
    """
    # Import here so Modal image has the packages
    from services.synthesis import filter_transcript, synthesise

    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.connect() as conn:
        await _run_pipeline(conn, session_id, file_bytes, filename, session_name)
    await engine.dispose()
    return

async def _run_pipeline(conn, session_id, file_bytes, filename, session_name):
    from services.synthesis import filter_transcript, synthesise
    try:
        # ── Mark as processing ──────────────────────────────────────────────
        await _update_session(conn, session_id, "processing")
        print(f"[{session_id}] status → processing")

        # ── Write file to temp disk ─────────────────────────────────────────
        suffix = Path(filename).suffix or ".audio"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name
        print(f"[{session_id}] saved to {tmp_path} ({len(file_bytes) / 1024:.0f} KB)")

        # ── Transcription or direct text load ──────────────────────────────
        text_extensions = {".txt", ".md"}
        if Path(filename).suffix.lower() in text_extensions:
            # Already a transcript — skip AssemblyAI
            raw_transcript = Path(tmp_path).read_text(encoding="utf-8")
            print(f"[{session_id}] loaded text file — {len(raw_transcript)} chars")
        else:
            aai.settings.api_key = os.environ["ASSEMBLYAI_API_KEY"]
            config = aai.TranscriptionConfig(speaker_labels=True, speech_models=["universal-2"])
            transcriber = aai.Transcriber()
            print(f"[{session_id}] sending to AssemblyAI...")
            transcript = transcriber.transcribe(tmp_path, config=config)
            if transcript.status == aai.TranscriptStatus.error:
                raise RuntimeError(f"AssemblyAI error: {transcript.error}")
            if not transcript.utterances:
                raw_transcript = transcript.text or ""
            else:
                raw_transcript = _format_transcript(transcript.utterances)
            print(f"[{session_id}] transcription done — {len(raw_transcript)} chars")

        # ── Claude Haiku filtering ──────────────────────────────────────────
        print(f"[{session_id}] filtering with Haiku...")
        filtered = filter_transcript(raw_transcript)
        reduction = (1 - len(filtered) / max(len(raw_transcript), 1)) * 100
        print(f"[{session_id}] filtered — {reduction:.0f}% removed")

        # ── Claude Sonnet synthesis ─────────────────────────────────────────
        print(f"[{session_id}] synthesising with Sonnet...")
        synthesis = synthesise(filtered, session_name)

        themes = synthesis.get("themes", [])
        quotes = synthesis.get("quotes", [])
        findings = synthesis.get("findings", [])
        participant_count = synthesis.get("participant_count", 0)
        quote_count = len(quotes)

        print(f"[{session_id}] synthesis done — {len(themes)} themes, {quote_count} quotes")

        # ── Store results in DB ─────────────────────────────────────────────
        await _update_session(
            conn,
            session_id,
            "ready",
            themes=json.dumps({"themes": themes, "quotes": quotes, "findings": findings}),
            quote_count=quote_count,
        )
        print(f"[{session_id}] status → ready ✓")

    except Exception as e:
        print(f"[{session_id}] pipeline failed: {e}", file=sys.stderr)
        await _update_session(conn, session_id, "failed")
        raise

    finally:
        pass

# ---------------------------------------------------------------------------
# Local test entrypoint
# modal run workers/pipeline.py --file-path /path/to/test.mp3
# ---------------------------------------------------------------------------

@app.local_entrypoint()
def main(file_path: str, session_name: str = "Test session", session_id: str = ""):
    import uuid
    if not session_id:
        session_id = str(uuid.uuid4())
    print(f"Running pipeline locally — session_id: {session_id}")
    file_bytes = Path(file_path).read_bytes()
    filename = Path(file_path).name
    from dotenv import load_dotenv
    load_dotenv()
    import asyncio
    asyncio.run(process_session.local(session_id, file_bytes, filename, session_name))
    print("Done.")
