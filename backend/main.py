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

@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()

# ── Routers ───────────────────────────────────────────────────────────────────
from api.sessions import router as sessions_router
from api.stubs import router as stubs_router
from api.billing import router as billing_router
from api.portal import router as portal_router
from api.chat import router as chat_router        # Phase 2

app.include_router(sessions_router, prefix="/v1")
app.include_router(stubs_router, prefix="/v1")    # stub routes under /v1 to match dispatch_tool calls
app.include_router(billing_router)
app.include_router(portal_router)
app.include_router(chat_router)                   # /v1/chat, /v1/conversations

# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "version": "2.0.0"}
