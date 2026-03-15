"""
SenseCritiq — Modal async processing pipeline.

Triggered by POST /v1/upload after a file lands in R2.
Runs: download → transcribe (AssemblyAI) → filter (Claude Haiku) → synthesise (Claude Sonnet)
      → embed (OpenAI) → store (PostgreSQL + pgvector) → mark session ready.

Deploy:
    modal deploy backend/workers/pipeline.py

Required Modal secrets (set at modal.com/secrets):
    sensecritiq-secrets:
        DATABASE_URL
        ASSEMBLYAI_API_KEY
        ANTHROPIC_API_KEY
        OPENAI_API_KEY
        R2_ENDPOINT_URL
        R2_ACCESS_KEY_ID
        R2_SECRET_ACCESS_KEY
        R2_BUCKET_NAME
"""

import modal

# ── App & image ───────────────────────────────────────────────────────────────

app = modal.App("sensecritiq-pipeline")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "httpx==0.27.0",
        "anthropic==0.25.0",
        "openai==1.25.0",
        "boto3==1.34.0",
        "asyncpg==0.29.0",
        "databases[asyncpg]==0.9.0",
        "pgvector==0.2.5",
        "psycopg2-binary==2.9.9",
        "sqlalchemy==2.0.29",
    )
)

secrets = [modal.Secret.from_name("sensecritiq-secrets")]


# ── Synthesis prompt ──────────────────────────────────────────────────────────

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
- Ignore pleasantries, filler, consent scripts, and facilitator meta-commentary.
"""

FILTER_SYSTEM = """You are a transcript cleaner for UX research.
Remove: consent scripts, pleasantries, scheduling talk, facilitator instructions,
        technical setup issues, and any non-research meta-content.
Keep: all participant statements about their experience, opinions, confusion,
      delight, or behaviour. Keep speaker labels and timestamps exactly as-is.
Return ONLY the cleaned transcript text — nothing else."""


# ── Helper: update session status ─────────────────────────────────────────────

async def _update_session(db, session_id: str, **fields):
    set_clause = ", ".join(f"{k} = :{k}" for k in fields)
    await db.execute(
        f"UPDATE sessions SET {set_clause} WHERE id = :session_id",
        {"session_id": session_id, **fields},
    )


# ── Main pipeline function ────────────────────────────────────────────────────

@app.function(
    image=image,
    secrets=secrets,
    timeout=900,           # 15 min max — long recordings need time
    retries=modal.Retries(max_retries=2, backoff_coefficient=2),
)
async def process_session(session_id: str, s3_key: str, filename: str):
    """
    Full processing pipeline for one research artifact.
    Called via: await process_session.spawn.aio(session_id, s3_key, filename)
    """
    import os, json, io, uuid, time
    import boto3
    import anthropic
    import openai
    from databases import Database

    import asyncio as _asyncio
    db_url = os.environ["DATABASE_URL"]
    database = Database(db_url)

    # Retry DB connect with exponential backoff — Railway PostgreSQL can time
    # out on cold Modal container SSL handshake.
    _max_attempts = 4
    for _attempt in range(1, _max_attempts + 1):
        try:
            await database.connect()
            break
        except Exception as _conn_err:
            if _attempt == _max_attempts:
                raise
            _wait = 2 ** _attempt          # 2s, 4s, 8s
            print(f"[pipeline] {session_id} — DB connect attempt {_attempt} failed "
                  f"({_conn_err!r}), retrying in {_wait}s…")
            await _asyncio.sleep(_wait)

    try:
        # ── 1. Mark as processing ────────────────────────────────────────────
        await _update_session(database, session_id, status="processing")
        print(f"[pipeline] {session_id} — status: processing")

        # Fetch account_id early — needed for usage_log inserts throughout pipeline
        _acct_row = await database.fetch_one(
            "SELECT account_id FROM sessions WHERE id = :id",
            {"id": session_id},
        )
        account_id = str(_acct_row["account_id"]) if _acct_row else None

        # ── 2. Download file from R2 ─────────────────────────────────────────
        s3 = boto3.client(
            "s3",
            endpoint_url=os.environ["R2_ENDPOINT_URL"],
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        )
        bucket = os.environ["R2_BUCKET_NAME"]
        buf = io.BytesIO()
        s3.download_fileobj(bucket, s3_key, buf)
        buf.seek(0)
        file_bytes = buf.read()
        print(f"[pipeline] {session_id} — downloaded {len(file_bytes):,} bytes from R2")

        # ── 3. Get transcript text ────────────────────────────────────────────
        # Route by file type: text files extract directly, audio/video use AssemblyAI
        import httpx, time as _time

        TEXT_EXTENSIONS = {"txt", "md", "vtt", "srt", "csv"}
        DOC_EXTENSIONS  = {"pdf", "docx", "doc"}
        AUDIO_EXTENSIONS = {"mp3", "mp4", "wav", "m4a", "aac", "ogg", "flac", "webm", "mov"}

        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

        if ext in TEXT_EXTENSIONS:
            # ── Plain text — decode directly ──────────────────────────────────
            try:
                raw_transcript = file_bytes.decode("utf-8")
            except UnicodeDecodeError:
                raw_transcript = file_bytes.decode("latin-1", errors="replace")
            print(f"[pipeline] {session_id} — text file, {len(raw_transcript):,} chars")

        elif ext in DOC_EXTENSIONS:
            # ── PDF / DOCX — extract text ─────────────────────────────────────
            raw_transcript = _extract_doc_text(file_bytes, ext)
            print(f"[pipeline] {session_id} — doc file ({ext}), {len(raw_transcript):,} chars")

        else:
            # ── Audio / Video — transcribe with AssemblyAI ────────────────────
            aai_key = os.environ["ASSEMBLYAI_API_KEY"]
            aai_headers = {"authorization": aai_key, "content-type": "application/json"}

            with httpx.Client(timeout=120) as http:
                up = http.post(
                    "https://api.assemblyai.com/v2/upload",
                    headers={"authorization": aai_key},
                    content=file_bytes,
                )
                up.raise_for_status()
                audio_url = up.json()["upload_url"]
                print(f"[pipeline] {session_id} — uploaded to AssemblyAI: {audio_url}")

                job = http.post(
                    "https://api.assemblyai.com/v2/transcript",
                    headers=aai_headers,
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
                print(f"[pipeline] {session_id} — transcription job {job_id} submitted")

                while True:
                    poll = http.get(
                        f"https://api.assemblyai.com/v2/transcript/{job_id}",
                        headers=aai_headers,
                    )
                    poll.raise_for_status()
                    result = poll.json()
                    status = result["status"]
                    if status == "completed":
                        break
                    elif status == "error":
                        raise RuntimeError(f"AssemblyAI error: {result.get('error')}")
                    _time.sleep(3)

            utterances = result.get("utterances") or []
            if utterances:
                raw_lines = [
                    f"Speaker {utt['speaker']} [{_fmt_timestamp((utt['start'] or 0) // 1000)}]: {utt['text']}"
                    for utt in utterances
                ]
            else:
                raw_lines = [result.get("text") or ""]
            raw_transcript = "\n".join(raw_lines)
            print(f"[pipeline] {session_id} — transcribed {len(raw_transcript):,} chars")

        if not raw_transcript.strip():
            raise RuntimeError(f"No text could be extracted from '{filename}'")

        # Save transcript key (optional — for future retrieval)
        transcript_key = f"transcripts/{session_id}/transcript.txt"
        s3.put_object(
            Bucket=bucket,
            Key=transcript_key,
            Body=raw_transcript.encode("utf-8"),
        )
        await _update_session(database, session_id, transcript_s3_key=transcript_key)

        # ── 4. Filter with Claude Haiku ──────────────────────────────────────
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

        # Split into chunks if very long (Haiku has 200k context but keep costs low)
        MAX_FILTER_CHARS = 80_000
        if len(raw_transcript) > MAX_FILTER_CHARS:
            chunks = _chunk_text(raw_transcript, MAX_FILTER_CHARS)
        else:
            chunks = [raw_transcript]

        # Token cost constants (USD per token)
        HAIKU_IN   = 0.80  / 1_000_000   # $0.80 per 1M input tokens
        HAIKU_OUT  = 4.00  / 1_000_000   # $4.00 per 1M output tokens
        SONNET_IN  = 3.00  / 1_000_000   # $3.00 per 1M input tokens
        SONNET_OUT = 15.00 / 1_000_000   # $15.00 per 1M output tokens

        haiku_input_tokens  = 0
        haiku_output_tokens = 0

        filtered_parts = []
        for i, chunk in enumerate(chunks):
            print(f"[pipeline] {session_id} — filtering chunk {i+1}/{len(chunks)}")
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=4096,
                system=FILTER_SYSTEM,
                messages=[{"role": "user", "content": chunk}],
            )
            filtered_parts.append(resp.content[0].text)
            haiku_input_tokens  += resp.usage.input_tokens
            haiku_output_tokens += resp.usage.output_tokens

        # Log Haiku usage
        haiku_tokens = haiku_input_tokens + haiku_output_tokens
        haiku_cost   = haiku_input_tokens * HAIKU_IN + haiku_output_tokens * HAIKU_OUT
        await database.execute(
            """INSERT INTO usage_log (id, account_id, session_id, action, tokens_used, cost_usd, created_at)
               VALUES (:id, :aid, :sid, :action, :tokens, :cost, NOW())""",
            {"id": str(uuid.uuid4()), "aid": account_id, "sid": session_id,
             "action": "haiku_filter", "tokens": haiku_tokens, "cost": round(haiku_cost, 6)},
        )

        filtered_transcript = "\n".join(filtered_parts)
        print(f"[pipeline] {session_id} — filtered to {len(filtered_transcript):,} chars "
              f"({100*len(filtered_transcript)//max(len(raw_transcript),1)}% of original)")

        # ── 5. Synthesise with Claude Sonnet ─────────────────────────────────
        print(f"[pipeline] {session_id} — synthesising with Claude Sonnet")
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

        # Log Sonnet usage
        sonnet_tokens = synthesis_resp.usage.input_tokens + synthesis_resp.usage.output_tokens
        sonnet_cost   = synthesis_resp.usage.input_tokens * SONNET_IN + synthesis_resp.usage.output_tokens * SONNET_OUT
        await database.execute(
            """INSERT INTO usage_log (id, account_id, session_id, action, tokens_used, cost_usd, created_at)
               VALUES (:id, :aid, :sid, :action, :tokens, :cost, NOW())""",
            {"id": str(uuid.uuid4()), "aid": account_id, "sid": session_id,
             "action": "sonnet_synthesis", "tokens": sonnet_tokens, "cost": round(sonnet_cost, 6)},
        )

        synthesis_text = synthesis_resp.content[0].text.strip()

        # Strip markdown code fences if present
        if synthesis_text.startswith("```"):
            synthesis_text = synthesis_text.split("```")[1]
            if synthesis_text.startswith("json"):
                synthesis_text = synthesis_text[4:]
        synthesis_text = synthesis_text.strip()

        synthesis = json.loads(synthesis_text)
        themes    = synthesis.get("themes", [])
        findings  = synthesis.get("key_findings", [])
        quotes    = synthesis.get("quotes", [])
        print(f"[pipeline] {session_id} — {len(themes)} themes, {len(findings)} findings, "
              f"{len(quotes)} quotes")

        # ── 6. Generate embeddings (OpenAI text-embedding-3-small) ───────────
        oa = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        quote_texts = [q["text"] for q in quotes if q.get("text")]
        embeddings: list[list[float] | None] = [None] * len(quotes)

        if quote_texts:
            try:
                BATCH = 100  # OpenAI allows up to 2048 inputs; keep batches small
                all_embeddings: list[list[float]] = []
                for i in range(0, len(quote_texts), BATCH):
                    batch = quote_texts[i : i + BATCH]
                    resp = oa.embeddings.create(
                        model="text-embedding-3-small",
                        input=batch,
                    )
                    all_embeddings.extend([e.embedding for e in resp.data])

                # Map back to original quotes list (quotes without text stay None)
                text_idx = 0
                for i, q in enumerate(quotes):
                    if q.get("text"):
                        embeddings[i] = all_embeddings[text_idx]
                        text_idx += 1

                print(f"[pipeline] {session_id} — generated {len(all_embeddings)} embeddings")
            except Exception as emb_err:
                print(f"[pipeline] {session_id} — embeddings failed (non-fatal): {emb_err}")

        # ── 7. Store quotes in DB ─────────────────────────────────────────────
        import json as _json
        for i, q in enumerate(quotes):
            qid = str(uuid.uuid4())
            emb = embeddings[i]
            # Serialize embedding as pgvector string format: "[x1,x2,...]"
            emb_str = _json.dumps(emb) if emb is not None else None

            if emb_str is not None:
                await database.execute(
                    """INSERT INTO quotes
                       (id, session_id, account_id, text, speaker, timestamp_sec,
                        theme_label, embedding_model, embedding)
                       VALUES (:id, :sid, :aid, :text, :speaker, :ts,
                               :theme_label, :model, CAST(:emb AS vector))
                       ON CONFLICT DO NOTHING""",
                    {
                        "id": qid,
                        "sid": session_id,
                        "aid": account_id,
                        "text": q.get("text", ""),
                        "speaker": q.get("speaker"),
                        "ts": q.get("timestamp_sec"),
                        "theme_label": q.get("theme_label"),
                        "model": "text-embedding-3-small",
                        "emb": emb_str,
                    },
                )
            else:
                await database.execute(
                    """INSERT INTO quotes
                       (id, session_id, account_id, text, speaker, timestamp_sec,
                        theme_label, embedding_model)
                       VALUES (:id, :sid, :aid, :text, :speaker, :ts,
                               :theme_label, :model)
                       ON CONFLICT DO NOTHING""",
                    {
                        "id": qid,
                        "sid": session_id,
                        "aid": account_id,
                        "text": q.get("text", ""),
                        "speaker": q.get("speaker"),
                        "ts": q.get("timestamp_sec"),
                        "theme_label": q.get("theme_label"),
                        "model": "text-embedding-3-small",
                    },
                )

        # ── 8. Update session → ready ─────────────────────────────────────────
        import datetime
        await _update_session(
            database,
            session_id,
            status="ready",
            themes=json.dumps(themes),
            findings=json.dumps(findings),
            quote_count=len(quotes),
            completed_at=datetime.datetime.utcnow(),
        )
        print(f"[pipeline] {session_id} — status: ready ✓")

    except Exception as e:
        print(f"[pipeline] {session_id} — FAILED: {e}")
        import traceback
        traceback.print_exc()
        try:
            await _update_session(database, session_id, status="failed")
        except Exception:
            pass
        raise

    finally:
        await database.disconnect()


# ── Utilities ─────────────────────────────────────────────────────────────────

def _fmt_timestamp(seconds: int) -> str:
    """Convert seconds to HH:MM:SS string."""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _extract_doc_text(file_bytes: bytes, ext: str) -> str:
    """Extract plain text from PDF or DOCX bytes."""
    import io
    if ext == "pdf":
        try:
            import pdfminer.high_level
            return pdfminer.high_level.extract_text(io.BytesIO(file_bytes))
        except Exception:
            pass
        # Fallback: raw decode
        return file_bytes.decode("latin-1", errors="replace")

    if ext in ("docx", "doc"):
        try:
            import docx
            doc = docx.Document(io.BytesIO(file_bytes))
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except Exception:
            pass
        # Fallback: extract XML from zip
        try:
            import zipfile, re
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                xml = z.read("word/document.xml").decode("utf-8")
            return re.sub(r"<[^>]+>", " ", xml)
        except Exception:
            pass

    return file_bytes.decode("utf-8", errors="replace")


def _chunk_text(text: str, max_chars: int) -> list[str]:
    """Split text into chunks at newline boundaries."""
    chunks, current = [], []
    current_len = 0
    for line in text.split("\n"):
        if current_len + len(line) > max_chars and current:
            chunks.append("\n".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += len(line)
    if current:
        chunks.append("\n".join(current))
    return chunks
