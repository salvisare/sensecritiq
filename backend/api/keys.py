"""
SenseCritiq — API Key management endpoints.

Used by the portal to list, create, and revoke scq_live_ API keys.
All routes are mounted under /portal/keys (no prefix needed — registered directly).

Auth: Clerk JWT via _verify_clerk_token (same as portal.py routes).
Storage: api_keys table in PostgreSQL.
Key generation: local (secrets module) — no external dependency.

Key format: scq_live_<32 random hex chars>
Verification: SHA-256 hash stored in DB, compared on each MCP request.
"""

import hashlib
import os
import secrets
import uuid
from datetime import datetime, timezone

from databases import Database
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()


# ── Auth helper ────────────────────────────────────────────────────────────────

async def _get_portal_account_id(request: Request) -> str:
    """Verify Clerk JWT and return the account_id from our DB."""
    from api.portal import _verify_clerk_token
    try:
        user_info = await _verify_clerk_token(request)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

    clerk_user_id = user_info.get("clerk_user_id", "")
    email = user_info.get("email", "")
    db: Database = request.app.state.db
    row = await db.fetch_one(
        "SELECT id FROM accounts WHERE clerk_user_id = :cuid OR email = :email LIMIT 1",
        {"cuid": clerk_user_id, "email": email},
    )
    if not row:
        raise HTTPException(status_code=401, detail="Account not found")
    return str(row["id"])


# ── GET /portal/keys ──────────────────────────────────────────────────────────

@router.get("/portal/keys")
async def list_keys(
    request: Request,
    account_id: str = Depends(_get_portal_account_id),
):
    """List all non-revoked API keys for the account. Key values are masked."""
    db: Database = request.app.state.db
    rows = await db.fetch_all(
        """SELECT id, name, prefix, created_at, last_used_at
           FROM api_keys
           WHERE account_id = :aid AND revoked = false
           ORDER BY created_at DESC""",
        {"aid": account_id},
    )
    keys = []
    for r in rows:
        prefix = r["prefix"] or "scq_live"
        keys.append({
            "id": str(r["id"]),
            "name": r["name"] or "Unnamed key",
            "key_preview": f"{prefix}_{'•' * 8}",
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "last_used_at": r["last_used_at"].isoformat() if r["last_used_at"] else None,
        })
    return {"keys": keys}


# ── POST /portal/keys ─────────────────────────────────────────────────────────

class CreateKeyRequest(BaseModel):
    name: str = "My API key"


@router.post("/portal/keys")
async def create_key(
    body: CreateKeyRequest,
    request: Request,
    account_id: str = Depends(_get_portal_account_id),
):
    """
    Generate a new scq_live_ API key locally using secrets module.
    Returns the full key ONCE — only the SHA-256 hash is stored in our DB.
    Format: scq_live_<32 random hex chars>
    """
    db: Database = request.app.state.db

    # Enforce 5-key limit per account
    count_row = await db.fetch_one(
        "SELECT COUNT(*) as cnt FROM api_keys WHERE account_id = :aid AND revoked = false",
        {"aid": account_id},
    )
    if count_row and count_row["cnt"] >= 5:
        raise HTTPException(
            status_code=400,
            detail="Maximum of 5 active keys allowed. Revoke an existing key first."
        )

    # Generate key locally — no external calls needed
    raw_secret = secrets.token_hex(32)          # 64 hex chars = 256 bits of entropy
    full_key   = f"scq_live_{raw_secret}"
    prefix     = "scq_live"
    key_hash   = hashlib.sha256(full_key.encode()).hexdigest()
    key_id     = str(uuid.uuid4())
    now        = datetime.now(timezone.utc)

    await db.execute(
        """INSERT INTO api_keys
               (id, account_id, key_hash, unkey_key_id, prefix, name, created_at, revoked)
           VALUES
               (:id, :account_id, :key_hash, :unkey_key_id, :prefix, :name, :created_at, false)""",
        {
            "id": key_id,
            "account_id": account_id,
            "key_hash": key_hash,
            "unkey_key_id": None,       # not using Unkey.dev
            "prefix": prefix,
            "name": body.name,
            "created_at": now,
        },
    )

    return {
        "id": key_id,
        "name": body.name,
        "key": full_key,                        # Only shown once — researcher must save it
        "key_preview": f"scq_live_{'•' * 8}",
        "created_at": now.isoformat(),
        "warning": "Save this key now — it will not be shown again.",
    }


# ── DELETE /portal/keys/{key_id} ──────────────────────────────────────────────

@router.delete("/portal/keys/{key_id}")
async def revoke_key(
    key_id: str,
    request: Request,
    account_id: str = Depends(_get_portal_account_id),
):
    """Revoke an API key — marks it as revoked in our DB."""
    db: Database = request.app.state.db

    row = await db.fetch_one(
        """SELECT id FROM api_keys
           WHERE id = :id AND account_id = :aid AND revoked = false""",
        {"id": key_id, "aid": account_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Key not found")

    await db.execute(
        "UPDATE api_keys SET revoked = true WHERE id = :id",
        {"id": key_id},
    )

    return {"id": key_id, "revoked": True}
