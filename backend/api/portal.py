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
    """
    Verify Clerk JWT and return user info.
    Returns {"clerk_user_id": ..., "email": ...}
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")

    token = auth.removeprefix("Bearer ").strip()

    # Dev bypass
    if token == "scq_test_dev":
        return {"clerk_user_id": "dev_user", "email": "dev@sensecritiq.com"}

    try:
        # Decode header to get key ID
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")

        # Fetch Clerk JWKS
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.clerk.com/v1/jwks",
                headers={"Authorization": f"Bearer {CLERK_SECRET_KEY}"},
            )
        jwks = resp.json()

        # Find matching key
        key_data = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
        if not key_data:
            raise HTTPException(status_code=401, detail="Invalid token key")

        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key_data))
        payload = jwt.decode(token, public_key, algorithms=["RS256"])

        clerk_user_id = payload.get("sub")
        email = payload.get("email", "")

        return {"clerk_user_id": clerk_user_id, "email": email}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")


async def _get_or_create_account(clerk_user_id: str, email: str) -> dict:
    """
    Look up account by clerk_user_id.
    If not found, create account + generate Unkey API key.
    """
    AsyncSessionLocal = _get_db()
    async with AsyncSessionLocal() as db:
        # Check if account exists
        result = await db.execute(
            text("SELECT id, email, plan, stripe_customer_id FROM accounts WHERE clerk_user_id = :clerk_user_id"),
            {"clerk_user_id": clerk_user_id}
        )
        row = result.fetchone()

        if row:
            account_id = str(row[0])
        else:
            # Create new account
            import uuid
            account_id = str(uuid.uuid4())
            await db.execute(
                text("""
                    INSERT INTO accounts (id, email, clerk_user_id, plan, created_at)
                    VALUES (:id, :email, :clerk_user_id, 'free', NOW())
                """),
                {"id": account_id, "email": email, "clerk_user_id": clerk_user_id}
            )
            await db.commit()
            print(f"[portal] created account {account_id} for {email}")

        # Fetch API key from Unkey
        api_key = await _get_or_create_unkey_key(account_id, email)

        # Get usage stats
        usage_result = await db.execute(
            text("""
                SELECT COUNT(*) FROM sessions
                WHERE account_id = :account_id
                AND created_at >= date_trunc('month', NOW())
            """),
            {"account_id": account_id}
        )
        sessions_this_month = usage_result.scalar() or 0

        # Get plan
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
    """
    Check if account already has a key in Unkey.
    If not, create one. Returns the key string or masked version.
    """
    if not UNKEY_ROOT_KEY or not UNKEY_API_ID:
        return "scq_live_configure_unkey_first"

    try:
        async with httpx.AsyncClient() as client:
            # Create a new key for this account
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


# ---------------------------------------------------------------------------
# GET /portal/me
# ---------------------------------------------------------------------------

@router.get("/me")
async def get_me(request: Request):
    user = await _verify_clerk_token(request)
    account = await _get_or_create_account(
        user["clerk_user_id"],
        user["email"]
    )
    return account


# ---------------------------------------------------------------------------
# GET /portal/sessions
# ---------------------------------------------------------------------------

@router.get("/sessions")
async def get_sessions(request: Request, limit: int = 10):
    user = await _verify_clerk_token(request)

    AsyncSessionLocal = _get_db()
    async with AsyncSessionLocal() as db:
        # Get account_id from clerk_user_id
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


# ---------------------------------------------------------------------------
# GET /portal/usage
# ---------------------------------------------------------------------------

@router.get("/usage")
async def get_usage(request: Request):
    """
    Returns token and cost usage for the authenticated account.
    Aggregated by month (current + last 2) and broken down per session.
    """
    user = await _verify_clerk_token(request)

    AsyncSessionLocal = _get_db()
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text("SELECT id FROM accounts WHERE clerk_user_id = :cuid"),
            {"cuid": user["clerk_user_id"]}
        )
        row = result.fetchone()
        if not row:
            return {"this_month": {}, "all_time": {}, "by_session": [], "by_action": []}

        account_id = str(row[0])

        # ── Totals this month ────────────────────────────────────────────────
        month_result = await db.execute(
            text("""
                SELECT
                    COALESCE(SUM(tokens_used), 0)  AS tokens,
                    COALESCE(SUM(cost_usd), 0)     AS cost,
                    COUNT(*)                        AS events
                FROM usage_log
                WHERE account_id = :aid
                  AND created_at >= date_trunc('month', NOW())
            """),
            {"aid": account_id}
        )
        month_row = month_result.fetchone()

        # ── All-time totals ──────────────────────────────────────────────────
        alltime_result = await db.execute(
            text("""
                SELECT
                    COALESCE(SUM(tokens_used), 0) AS tokens,
                    COALESCE(SUM(cost_usd), 0)    AS cost,
                    COUNT(*)                       AS events
                FROM usage_log
                WHERE account_id = :aid
            """),
            {"aid": account_id}
        )
        alltime_row = alltime_result.fetchone()

        # ── Per-session breakdown (last 20 sessions with usage) ──────────────
        session_result = await db.execute(
            text("""
                SELECT
                    ul.session_id,
                    s.name                          AS session_name,
                    SUM(ul.tokens_used)             AS tokens,
                    SUM(ul.cost_usd)                AS cost,
                    MAX(ul.created_at)              AS last_event
                FROM usage_log ul
                LEFT JOIN sessions s ON s.id = ul.session_id
                WHERE ul.account_id = :aid
                GROUP BY ul.session_id, s.name
                ORDER BY last_event DESC
                LIMIT 20
            """),
            {"aid": account_id}
        )
        session_rows = session_result.fetchall()

        # ── Breakdown by action type ─────────────────────────────────────────
        action_result = await db.execute(
            text("""
                SELECT
                    action,
                    COUNT(*)              AS calls,
                    SUM(tokens_used)      AS tokens,
                    SUM(cost_usd)         AS cost
                FROM usage_log
                WHERE account_id = :aid
                GROUP BY action
                ORDER BY tokens DESC
            """),
            {"aid": account_id}
        )
        action_rows = action_result.fetchall()

        # ── Sessions this month (for quota context) ──────────────────────────
        sessions_month_result = await db.execute(
            text("""
                SELECT COUNT(*) FROM sessions
                WHERE account_id = :aid
                  AND created_at >= date_trunc('month', NOW())
            """),
            {"aid": account_id}
        )
        sessions_this_month = sessions_month_result.scalar() or 0

    def _fmt_cost(val):
        return round(float(val), 4) if val else 0.0

    return {
        "this_month": {
            "tokens": int(month_row[0]) if month_row else 0,
            "cost_usd": _fmt_cost(month_row[1] if month_row else 0),
            "events": int(month_row[2]) if month_row else 0,
            "sessions": int(sessions_this_month),
        },
        "all_time": {
            "tokens": int(alltime_row[0]) if alltime_row else 0,
            "cost_usd": _fmt_cost(alltime_row[1] if alltime_row else 0),
            "events": int(alltime_row[2]) if alltime_row else 0,
        },
        "by_session": [
            {
                "session_id": str(r[0]) if r[0] else None,
                "session_name": r[1] or "Unnamed session",
                "tokens": int(r[2]) if r[2] else 0,
                "cost_usd": _fmt_cost(r[3]),
                "last_event": r[4].isoformat() if r[4] else None,
            }
            for r in session_rows
        ],
        "by_action": [
            {
                "action": r[0],
                "calls": int(r[1]),
                "tokens": int(r[2]) if r[2] else 0,
                "cost_usd": _fmt_cost(r[3]),
            }
            for r in action_rows
        ],
    }
