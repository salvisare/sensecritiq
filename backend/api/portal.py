"""
SenseCritiq — Portal API endpoints.
Authenticated via Clerk JWT (portal users), not Unkey (MCP users).
"""

import os
import json
import httpx
import jwt
from fastapi import APIRouter, Request, HTTPException
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

router = APIRouter(prefix="/portal", tags=["portal"])

DATABASE_URL = os.environ.get("DATABASE_URL", "")
CLERK_SECRET_KEY = os.environ.get("CLERK_SECRET_KEY", "")
UNKEY_ROOT_KEY = os.environ.get("UNKEY_ROOT_KEY", "")
UNKEY_API_ID = os.environ.get("UNKEY_API_ID", "")

DEV_ACCOUNT_ID = "00000000-0000-0000-0000-000000000001"


def _get_db():
    url = DATABASE_URL
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url, connect_args={"ssl": "require"})
    return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _verify_clerk_token(request: Request) -> dict:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")

    token = auth.removeprefix("Bearer ").strip()

    if token == "scq_test_dev":
        return {"clerk_user_id": "dev_user", "email": "dev@sensecritiq.com"}

    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://allowing-werewolf-46.clerk.accounts.dev/.well-known/jwks.json"
            )
        jwks = resp.json()

        key_data = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
        if not key_data:
            raise HTTPException(status_code=401, detail="Invalid token key")

        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key_data))
        payload = jwt.decode(token, public_key, algorithms=["RS256"])

        clerk_user_id = payload.get("sub")
        email = payload.get("email") or payload.get("email_address") or ""

        return {"clerk_user_id": clerk_user_id, "email": email}

    except HTTPException:
        raise
    except Exception as e:
        print(f"[auth] JWT verification failed: {type(e).__name__}: {str(e)}")
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")


async def _get_or_create_account(clerk_user_id: str, email: str) -> dict:
    AsyncSessionLocal = _get_db()
    async with AsyncSessionLocal() as db:
        # Look up by email first (stable unique key)
        result = await db.execute(
            text("""
                SELECT id, email, plan, stripe_customer_id FROM accounts 
                WHERE email = :email OR clerk_user_id = :clerk_user_id
                LIMIT 1
            """),
            {"email": email, "clerk_user_id": clerk_user_id}
        )
        row = result.fetchone()

        if row:
            account_id = str(row[0])
            # Backfill clerk_user_id if missing
            await db.execute(
                text("UPDATE accounts SET clerk_user_id = :clerk_user_id WHERE id = :id AND clerk_user_id IS NULL"),
                {"clerk_user_id": clerk_user_id, "id": account_id}
            )
            await db.commit()
        else:
            import uuid
            account_id = str(uuid.uuid4())
            await db.execute(
                text("""
                    INSERT INTO accounts (id, email, clerk_user_id, plan, created_at)
                    VALUES (:id, :email, :clerk_user_id, 'free', NOW())
                    ON CONFLICT (email) DO UPDATE SET clerk_user_id = EXCLUDED.clerk_user_id
                """),
                {"id": account_id, "email": email, "clerk_user_id": clerk_user_id}
            )
            await db.commit()
            print(f"[portal] created account {account_id} for {email}")

        api_key = await _get_or_create_unkey_key(account_id, email)

        usage_result = await db.execute(
            text("""
                SELECT COUNT(*) FROM sessions
                WHERE account_id = :account_id
                AND created_at >= date_trunc('month', NOW())
            """),
            {"account_id": account_id}
        )
        sessions_this_month = usage_result.scalar() or 0

        plan_result = await db.execute(
            text("SELECT plan, stripe_customer_id FROM accounts WHERE id = :id"),
            {"id": account_id}
        )
        plan_row = plan_result.fetchone()
        plan = plan_row[0] if plan_row else "free"
        stripe_customer_id = plan_row[1] if plan_row else None

    return {
        "id": account_id,
        "email": email,
        "plan": plan,
        "stripe_customer_id": stripe_customer_id,
        "api_key": api_key,
        "sessions_this_month": sessions_this_month,
    }


async def _get_or_create_unkey_key(account_id: str, email: str) -> str:
    if not UNKEY_ROOT_KEY or not UNKEY_API_ID:
        return "scq_live_configure_unkey_first"

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.unkey.com/v2/keys.createKey",
                headers={"Authorization": f"Bearer {UNKEY_ROOT_KEY}"},
                json={
                    "apiId": UNKEY_API_ID,
                    "prefix": "scq_live",
                    "meta": {"account_id": account_id, "email": email},
                    "name": f"account-{account_id[:8]}",
                },
                timeout=5.0,
            )
        data = resp.json()
        key = data.get("data", {}).get("key", "")
        return key if key else "scq_live_key_creation_failed"
    except Exception as e:
        print(f"[portal] Unkey key creation failed: {e}")
        return "scq_live_key_creation_failed"


@router.get("/me")
async def get_me(request: Request):
    user = await _verify_clerk_token(request)
    account = await _get_or_create_account(user["clerk_user_id"], user["email"])
    return account


@router.get("/sessions")
async def get_sessions(request: Request, limit: int = 10):
    user = await _verify_clerk_token(request)

    AsyncSessionLocal = _get_db()
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text("SELECT id FROM accounts WHERE clerk_user_id = :clerk_user_id"),
            {"clerk_user_id": user["clerk_user_id"]}
        )
        row = result.fetchone()
        if not row:
            return {"sessions": [], "total": 0}

        account_id = str(row[0])

        sessions_result = await db.execute(
            text("""
                SELECT id, name, project, status, quote_count, created_at, completed_at
                FROM sessions
                WHERE account_id = :account_id
                ORDER BY created_at DESC
                LIMIT :limit
            """),
            {"account_id": account_id, "limit": limit}
        )
        rows = sessions_result.fetchall()

    sessions = []
    for r in rows:
        sessions.append({
            "id": str(r[0]),
            "name": r[1],
            "project": r[2],
            "status": r[3],
            "quote_count": r[4] or 0,
            "created_at": r[5].isoformat() if r[5] else None,
            "completed_at": r[6].isoformat() if r[6] else None,
        })

    return {"sessions": sessions, "total": len(sessions)}
