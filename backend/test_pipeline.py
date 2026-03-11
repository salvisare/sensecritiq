"""
Direct pipeline test — no Modal, no async.
Runs AssemblyAI → Haiku filter → Sonnet synthesis → writes to DB.
Run from backend/: python3 test_pipeline.py /path/to/audio.m4a [session_id]
"""
import os, sys, json, asyncio
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent))

import assemblyai as aai
from services.synthesis import filter_transcript, synthesise
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from datetime import datetime, timezone

def format_transcript(utterances):
    lines = []
    for u in utterances:
        ts = f"{(u.start//1000)//60:02d}:{(u.start//1000)%60:02d}"
        lines.append(f"Speaker {u.speaker} [{ts}]: {u.text}")
    return "\n".join(lines)

async def write_to_db(session_id, synthesis):
    engine = create_async_engine(os.environ["DATABASE_URL"])
    themes = synthesis.get("themes", [])
    quotes = synthesis.get("quotes", [])
    findings = synthesis.get("findings", [])
    async with engine.connect() as conn:
        await conn.execute(text("""
            UPDATE sessions SET
                status = 'ready',
                themes = :themes,
                quote_count = :quote_count,
                completed_at = :completed_at
            WHERE id = :session_id
        """), {
            "session_id": session_id,
            "themes": json.dumps({"themes": themes, "quotes": quotes, "findings": findings}),
            "quote_count": len(quotes),
            "completed_at": datetime.now(timezone.utc),
        })
        await conn.commit()
    await engine.dispose()
    print(f"✓ Written to DB — session {session_id}")

def main(file_path, session_id=None):
    print(f"\n▶ File: {file_path}")
    if session_id:
        print(f"▶ Session ID: {session_id}")

    aai.settings.api_key = os.environ["ASSEMBLYAI_API_KEY"]
    config = aai.TranscriptionConfig(speaker_labels=True, speech_models=["universal-2"])
    print("⏳ Transcribing...")
    transcript = aai.Transcriber().transcribe(file_path, config=config)
    if transcript.status.value == "error":
        print(f"AssemblyAI error: {transcript.error}"); return
    raw = format_transcript(transcript.utterances) if transcript.utterances else (transcript.text or "")
    print(f"✓ Transcription: {len(raw)} chars")

    print("⏳ Filtering with Haiku...")
    filtered = filter_transcript(raw)
    reduction = (1 - len(filtered)/max(len(raw),1)) * 100
    print(f"✓ Filtered: {reduction:.0f}% removed")

    print("⏳ Synthesising with Sonnet...")
    synthesis = synthesise(filtered, "Test session — March 2026")
    print(f"✓ Done — {len(synthesis.get('themes',[]))} themes, {len(synthesis.get('quotes',[]))} quotes")
    print("\n--- SYNTHESIS OUTPUT ---")
    print(json.dumps(synthesis, indent=2))

    if session_id:
        print("\n⏳ Writing to DB...")
        asyncio.run(write_to_db(session_id, synthesis))

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 test_pipeline.py /path/to/audio.m4a [session_id]")
        sys.exit(1)
    session_id = sys.argv[2] if len(sys.argv) > 2 else None
    main(sys.argv[1], session_id)
