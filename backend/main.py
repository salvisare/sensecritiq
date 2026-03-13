"""
SenseCritiq Backend — main.py
Includes Phase 1 routers + Phase 2 chat router.
"""

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from databases import Database
import os

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="SenseCritiq API",
    version="2.0.0",
    description="UX research synthesis backend — MCP + Web Chat",
)

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://sensecritiq-portal.vercel.app",
        "https://sensecritiq.com",
        "https://app.sensecritiq.com",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Database ──────────────────────────────────────────────────────────────────
DATABASE_URL = os.environ["DATABASE_URL"]
database = Database(DATABASE_URL)

@app.on_event("startup")
async def startup():
    await database.connect()
    app.state.db = database
    await _ensure_schema(database)


async def _ensure_schema(db: Database):
    """Create any missing tables that Phase 2 requires."""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS quotes (
            id               UUID        PRIMARY KEY,
            session_id       UUID        NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            account_id       UUID        NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            text             TEXT        NOT NULL,
            speaker          TEXT,
            timestamp_sec    INTEGER,
            theme_label      TEXT,
            embedding_model  TEXT,
            created_at       TIMESTAMP   DEFAULT NOW()
        )
    """)
    # Ensure project column exists on sessions (may have been named differently)
    try:
        await db.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS project TEXT")
    except Exception:
        pass  # Column already exists or DDL restricted

@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()

# ── Routers ───────────────────────────────────────────────────────────────────
from api.sessions import router as sessions_router
from api.stubs import router as stubs_router
from api.billing import router as billing_router
from api.portal import router as portal_router
from api.keys import router as keys_router        # API key management
from api.chat import router as chat_router        # Phase 2

app.include_router(sessions_router, prefix="/v1")
app.include_router(stubs_router, prefix="/v1")    # stub routes under /v1 to match dispatch_tool calls
app.include_router(billing_router)
app.include_router(portal_router)
app.include_router(keys_router)                   # /portal/keys CRUD
app.include_router(chat_router)                   # /v1/chat, /v1/conversations

# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "version": "2.0.0"}
