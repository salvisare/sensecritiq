# SenseCritiq — Claude Code Context

> UX research synthesis agent. Ingests raw research artifacts (audio, video, transcripts,
> surveys) and produces structured insights: themes, affinity clusters, findings, reports.
> Queryable research memory across an organisation's entire history.
>
> **URL:** sensecritiq.com | **Status:** Building MVP | **Stack:** Python + FastAPI + PostgreSQL

---

## Repo Structure

```
sensecritiq/
├── CLAUDE.md              ← you are here
├── backend/               ← FastAPI app (the core system)
│   ├── main.py
│   ├── api/               ← route handlers
│   ├── models/            ← SQLAlchemy models
│   ├── services/          ← business logic (transcription, synthesis, search)
│   ├── workers/           ← Modal async processing functions
│   └── tests/
├── mcp-server/            ← thin MCP layer (~300 lines), open source MIT
│   ├── server.py
│   └── tools/
└── portal/                ← minimal React app (sensecritiq.com)
    ← signup, API key, usage dashboard ONLY
```

---

## Architecture: Six Layers

| Layer | What it does |
|---|---|
| **0 — MCP Server** | Translates Claude's tool calls → authenticated REST requests to backend. Thin. ~300 lines. The primary user-facing interface. |
| **1 — Backend API** | FastAPI. Validates API keys (Unkey.dev), meters quota, routes to internal services. Only external entry point. |
| **2 — Ingestion** | Accepts files, SHA-256 dedup, stores in S3/R2, queues for processing. |
| **3 — Processing** | AssemblyAI transcription → spaCy NLP → Claude synthesis. Async via Modal. |
| **4 — Storage** | PostgreSQL + pgvector. Research memory lives here. |
| **5 — Output** | Report generation (Markdown/PDF). Pre-signed S3 URLs. |

**Key principle:** MCP server is stateless and thin. All logic lives in the backend. The web portal (sensecritiq.com) does only 5 things: signup, API key generation, subscription management, usage dashboard, session history.

---

## Tech Stack

| Component | Choice | Why |
|---|---|---|
| Backend | Python 3.11 + FastAPI | Async, fast, auto OpenAPI docs |
| Database | PostgreSQL + pgvector (Railway) | Single DB for structured + vector search |
| API key auth | Unkey.dev | Handles hashing, rotation, rate limiting out of the box |
| Billing | Stripe | Subscriptions + webhooks |
| Async processing | Modal | Python decorators, no infra, great logs. Use instead of Celery. |
| Transcription | AssemblyAI | Managed, diarisation built in. Revisit self-hosted Whisper at 50+ customers. |
| LLM (synthesis) | Claude Sonnet | Applied to cleaned ~30% of transcript content only |
| LLM (filtering) | Claude Haiku | Pre-screening and classification to reduce cost |
| Embeddings | text-embedding-3-small (OpenAI) | Store model_id alongside every vector |
| File storage | Cloudflare R2 (or AWS S3) | All uploads: audio, video, transcripts |
| MCP framework | anthropic-mcp (Python SDK) | Official SDK, PyPI distributed |
| Frontend | React + Tailwind | Portal only. Phase 2: app.sensecritiq.com chat UI |

---

## Database Schema (starter — 4 tables)

```sql
-- accounts
id UUID PK | email TEXT UNIQUE | created_at TIMESTAMP | plan TEXT

-- api_keys
id UUID PK | account_id UUID FK | key_hash TEXT | unkey_key_id TEXT
prefix TEXT | name TEXT | created_at TIMESTAMP | last_used_at TIMESTAMP | revoked BOOL

-- sessions
id UUID PK | account_id UUID FK | name TEXT | project TEXT | tags TEXT[]
status TEXT  -- queued | processing | ready | failed
file_s3_key TEXT | transcript_s3_key TEXT
themes JSONB | findings JSONB | quote_count INT
created_at TIMESTAMP | completed_at TIMESTAMP

-- usage_log
id UUID PK | account_id UUID FK | session_id UUID FK
action TEXT | tokens_used INT | cost_usd DECIMAL | created_at TIMESTAMP
```

**Phase 2 additions:** `projects`, `quotes`, `embeddings` (via pgvector), `conversations`, `messages`

---

## MCP Tool Catalogue (7 tools)

All tools authenticate via `SCQ_API_KEY` env var → `Authorization: Bearer` header on every backend call.

| Tool | Parameters | Returns |
|---|---|---|
| `upload_research_file` | file_path, session_name, project_id, tags[] | session_id, status, estimated_processing_time |
| `get_session_status` | session_id | status, progress_pct |
| `get_synthesis` | session_id | themes[], key_findings[], quote_count, participant_count |
| `get_quotes` | session_id, theme_id? | quotes[] with text, speaker, timestamp, theme_label |
| `search_research` | query, project_id?, date_from?, date_to? | results[] with quote, session_name, date, relevance_score, synthesis_summary |
| `list_sessions` | project_id?, limit, offset | sessions[] with id, name, date, status |
| `generate_report` | session_id, format (markdown\|pdf\|notion) | download_url, expires_at |

**Tool description quality matters as much as the code.** These descriptions are what Claude reads to decide which tool to call. Treat them as UX copy.

---

## API Key Conventions

- **Production:** `scq_live_` prefix (e.g. `scq_live_a1b2c3...`)
- **Sandbox:** `scq_test_` prefix
- **Env var in MCP config:** `SCQ_API_KEY`
- **Header:** `Authorization: Bearer scq_live_...`
- Prefixed keys allow GitHub + Claude secret scanners to flag accidental commits automatically

---

## Processing Pipeline (Modal async workers)

```
upload_research_file (MCP call)
  → POST /sessions (backend API)
    → store file in R2
    → enqueue Modal job
      → AssemblyAI transcription + diarisation
      → spaCy NLP extraction + filtering
      → Claude Haiku: pre-screen, remove meta-content (~70% filtered)
      → Claude Sonnet: extract themes, quotes, findings (JSON output)
      → generate embeddings (text-embedding-3-small)
      → store in PostgreSQL + pgvector
      → update session status → ready
```

**Cost target:** < $8 processing cost per Growth customer/month (30 sessions)

---

## Synthesis Prompt Principles

1. **Always output structured JSON** — themes, quotes, findings with explicit schema
2. **Every finding must cite a verbatim quote** — session_id + timestamp + speaker
3. **Never present a finding without a source** — post-processing step validates this
4. **Quote fidelity over synthesis quality** — researcher trust depends on accuracy

---

## Subscription Tiers

| Tier | Sessions/month | Price |
|---|---|---|
| Free | 2 | $0 |
| Starter | 10 | $29 |
| Growth | 30 | $79 |
| Team | Unlimited | $199 |
| Enterprise (Phase 2) | Unlimited | $499 |

1 session = 1 artifact through full pipeline. Quota enforced at `POST /sessions`.

---

## Build Order

1. **Week 1–2:** PostgreSQL schema + FastAPI skeleton + Unkey.dev auth middleware + Stripe webhook
2. **Week 3:** MCP server — all 7 tools against stub endpoints (mock data). Test in Claude Desktop.
3. **Week 4–5:** Modal workers — AssemblyAI → NLP → synthesis. Wire to real MCP tools.
4. **Week 6:** End-to-end test with 3–5 real researchers via sensecritiq.com
5. **Week 7–8:** Minimal React portal + Markdown/PDF reports + MCP registry submission
6. **Week 9+:** Distribution — ResearchOps Slack, UX communities, sensecritiq.com SEO

---

## Key Constraints & Guard-rails

- **MCP server must stay thin** — if logic creeps in, move it to the backend
- **Web portal does 5 things only** — resist scope creep into a research UI
- **Modal over Celery** — simpler ops, better observability, no Redis to manage
- **AssemblyAI over self-hosted Whisper** — until 800+ min/month processed
- **Unkey.dev over custom auth** — saves ~1 week of backend work
- **pgvector over Pinecone** — single DB until 100k+ vectors
- **Re-embed on model upgrade** — store `embedding_model_id` alongside every vector
- **CLAUDE.md is the single source of truth** — update it when architectural decisions change

---

## Useful Commands

```bash
# Backend dev
cd backend && uvicorn main:app --reload

# Run a Modal worker locally
modal run workers/transcription.py

# Deploy Modal workers
modal deploy workers/

# Database migrations
alembic upgrade head

# MCP server (local test)
cd mcp-server && python server.py

# Run tests
pytest backend/tests/
```

---

## Environment Variables

```bash
# Backend (.env)
DATABASE_URL=postgresql://...
UNKEY_API_ID=...
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
ASSEMBLYAI_API_KEY=...
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=...          # for text-embedding-3-small
R2_BUCKET_NAME=sensecritiq-uploads
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_ENDPOINT_URL=https://....r2.cloudflarestorage.com

# MCP server (.env or claude_desktop_config.json)
SCQ_API_KEY=scq_live_...
SCQ_API_BASE_URL=https://api.sensecritiq.com
```

---

*Keep this file updated as decisions are made. It is the source of truth for every Claude Code session.*
