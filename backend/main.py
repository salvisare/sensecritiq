from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api.sessions import router as sessions_router
from api.stubs import router as stubs_router
from api.billing import router as billing_router
from api.portal import router as portal_router
from api.chat import router as chat_router

app = FastAPI(
    title="SenseCritiq API",
    description="UX research synthesis backend — ingests research artifacts, produces structured insights.",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(sessions_router, prefix="/v1")
app.include_router(stubs_router)
app.include_router(billing_router)
app.include_router(portal_router)
app.include_router(chat_router)

@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok", "service": "sensecritiq-api"}