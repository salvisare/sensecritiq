"""
Internal request middleware.
Allows the chat endpoint to call backend routes directly without going
through Unkey API key validation — it uses a shared internal secret
header instead, and resolves the account_id from the header.

Usage in route dependencies:
    account_id: str = Depends(verify_internal_or_api_key)
"""

import os
from fastapi import Request, HTTPException


INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")


def is_internal_request(request: Request) -> bool:
    """Returns True if this is an internal call from the chat orchestrator."""
    if not INTERNAL_SECRET:
        return False
    return request.headers.get("X-Internal-Secret") == INTERNAL_SECRET


async def get_internal_account_id(request: Request) -> str | None:
    """Returns the account_id from the internal header, or None."""
    if is_internal_request(request):
        account_id = request.headers.get("X-Internal-Account-Id")
        if account_id:
            return account_id
    return None
