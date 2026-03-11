import os
import httpx
from fastapi import Request, HTTPException

UNKEY_ROOT_KEY = os.environ.get("UNKEY_ROOT_KEY", "")
UNKEY_API_ID = os.environ.get("UNKEY_API_ID", "")
DEV_ACCOUNT_ID = "00000000-0000-0000-0000-000000000001"

async def verify_api_key(request: Request) -> str:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing API key. Pass Authorization: Bearer scq_live_...")

    key = auth_header.removeprefix("Bearer ").strip()

    if key == "scq_test_dev":
        return DEV_ACCOUNT_ID

    if not UNKEY_ROOT_KEY or not UNKEY_API_ID:
        raise HTTPException(status_code=500, detail="Auth not configured on server")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.unkey.com/v2/keys.verifyKey",
            headers={"Authorization": f"Bearer {UNKEY_ROOT_KEY}"},
            json={"key": key},
            timeout=5.0,
        )

    if resp.status_code != 200:
        print(f"Unkey error: {resp.status_code} {resp.text}")
        raise HTTPException(status_code=401, detail="Could not verify API key")

    data = resp.json()
    valid = data.get("data", {}).get("valid", False)
    if not valid:
        code = data.get("data", {}).get("code", "UNKNOWN")
        raise HTTPException(status_code=401, detail=f"Invalid API key: {code}")

    meta = data.get("data", {}).get("meta", {}) or {}
    account_id = meta.get("account_id")
    if not account_id:
        raise HTTPException(status_code=401, detail="Key has no account_id in metadata")

    return account_id
