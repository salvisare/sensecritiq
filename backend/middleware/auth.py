"""
SenseCritiq — API key authentication middleware.

verify_api_key:  checks Bearer scq_live_* / scq_test_* tokens against
                 SHA-256 hashes in the api_keys table.
Returns the account_id for the key owner, or raises HTTP 401.
"""

import hashlib
from fastapi import Request, HTTPException
from databases import Database


async def verify_api_key(request: Request) -> str:
    """
    Authenticate a scq_live_ / scq_test_ API key.

    Computes SHA-256(token) and compares against api_keys.key_hash.
    Updates last_used_at on success.
    Returns account_id or raises 401.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    token = auth_header[len("Bearer "):].strip()
    if not token.startswith("scq_"):
        raise HTTPException(status_code=401, detail="Invalid API key format — expected scq_live_... or scq_test_...")

    key_hash = hashlib.sha256(token.encode()).hexdigest()

    db: Database = request.app.state.db
    row = await db.fetch_one(
        """SELECT id, account_id FROM api_keys
           WHERE key_hash = :hash AND revoked = false
           LIMIT 1""",
        {"hash": key_hash},
    )

    if not row:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")

    # Update last_used_at asynchronously (fire and don't block)
    try:
        await db.execute(
            "UPDATE api_keys SET last_used_at = NOW() WHERE key_hash = :hash",
            {"hash": key_hash},
        )
    except Exception:
        pass  # Non-fatal

    return str(row["account_id"])
