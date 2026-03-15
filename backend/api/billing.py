"""
SenseCritiq — Stripe billing endpoints.
Handles subscription creation, customer portal, and webhook events.
"""

import os
import stripe
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

router = APIRouter(prefix="/billing", tags=["billing"])

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Maps Stripe price IDs → internal plan names and session quotas
PRICE_PLAN_MAP = {
    os.environ.get("STRIPE_PRICE_STARTER_MONTHLY"): ("starter", 10),
    os.environ.get("STRIPE_PRICE_STARTER_ANNUAL"):  ("starter", 10),
    os.environ.get("STRIPE_PRICE_GROWTH_MONTHLY"):  ("growth",  30),
    os.environ.get("STRIPE_PRICE_GROWTH_ANNUAL"):   ("growth",  30),
    os.environ.get("STRIPE_PRICE_TEAM_MONTHLY"):    ("team",   100),
    os.environ.get("STRIPE_PRICE_TEAM_ANNUAL"):     ("team",   100),
}


def _get_db():
    url = DATABASE_URL
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url, connect_args={"ssl": "require"})
    return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ---------------------------------------------------------------------------
# POST /billing/create-checkout  — start a Stripe Checkout session
# ---------------------------------------------------------------------------

@router.post("/create-checkout")
async def create_checkout(request: Request):
    """
    Creates a Stripe Checkout session for a given price.
    Call this from the portal when a user selects a plan.
    """
    body = await request.json()
    price_id = body.get("price_id")
    account_id = body.get("account_id")
    email = body.get("email", "")
    success_url = body.get("success_url", "https://sensecritiq.com/dashboard?upgraded=true")
    cancel_url = body.get("cancel_url", "https://sensecritiq.com/pricing")

    if not price_id or price_id not in PRICE_PLAN_MAP:
        raise HTTPException(status_code=400, detail="Invalid price_id")

    checkout = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        customer_email=email or None,
        metadata={"account_id": account_id},
        success_url=success_url,
        cancel_url=cancel_url,
    )

    return {"checkout_url": checkout.url, "session_id": checkout.id}


# ---------------------------------------------------------------------------
# POST /billing/portal  — open Stripe customer portal
# ---------------------------------------------------------------------------

@router.post("/portal")
async def customer_portal(request: Request):
    """
    Creates a Stripe Customer Portal session so users can manage
    their subscription, update payment method, or cancel.
    """
    body = await request.json()
    customer_id = body.get("stripe_customer_id")
    return_url = body.get("return_url", "https://sensecritiq.com/dashboard")

    if not customer_id:
        raise HTTPException(status_code=400, detail="Missing stripe_customer_id")

    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=return_url,
    )
    return {"portal_url": session.url}


# ---------------------------------------------------------------------------
# POST /billing/webhook  — Stripe webhook handler
# ---------------------------------------------------------------------------

@router.post("/webhook")
async def stripe_webhook(request: Request):
    """
    Receives Stripe webhook events and updates account plan in DB.

    Key events handled:
    - checkout.session.completed  → activate subscription
    - customer.subscription.updated → plan change / renewal
    - customer.subscription.deleted → downgrade to free
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    # Verify webhook signature
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    event_type = event["type"]
    data = event["data"]["object"]

    print(f"[stripe] event: {event_type}")

    if event_type == "checkout.session.completed":
        await _handle_checkout_completed(data)

    elif event_type in ("customer.subscription.updated", "customer.subscription.created"):
        await _handle_subscription_updated(data)

    elif event_type == "customer.subscription.deleted":
        await _handle_subscription_deleted(data)

    return JSONResponse({"received": True})


# ---------------------------------------------------------------------------
# Internal handlers
# ---------------------------------------------------------------------------

async def _handle_checkout_completed(session):
    account_id = session.get("metadata", {}).get("account_id")
    customer_id = session.get("customer")
    subscription_id = session.get("subscription")

    if not account_id:
        print("[stripe] checkout.session.completed missing account_id in metadata")
        return

    # Retrieve the subscription to get the price ID
    sub = stripe.Subscription.retrieve(subscription_id)
    price_id = sub["items"]["data"][0]["price"]["id"]
    plan, _ = PRICE_PLAN_MAP.get(price_id, ("free", 2))

    await _update_account_plan(account_id, plan, customer_id, subscription_id)
    print(f"[stripe] account {account_id} upgraded to {plan}")


async def _handle_subscription_updated(sub):
    customer_id = sub.get("customer")
    subscription_id = sub.get("id")
    price_id = sub["items"]["data"][0]["price"]["id"]
    plan, _ = PRICE_PLAN_MAP.get(price_id, ("free", 2))
    status = sub.get("status")

    if status not in ("active", "trialing"):
        print(f"[stripe] subscription {subscription_id} status={status}, skipping")
        return

    # Look up account by stripe_customer_id
    account_id = await _get_account_by_customer(customer_id)
    if account_id:
        await _update_account_plan(account_id, plan, customer_id, subscription_id)
        print(f"[stripe] account {account_id} plan updated to {plan}")


async def _handle_subscription_deleted(sub):
    customer_id = sub.get("customer")
    account_id = await _get_account_by_customer(customer_id)
    if account_id:
        await _update_account_plan(account_id, "free", customer_id, None)
        print(f"[stripe] account {account_id} downgraded to free")


async def _update_account_plan(account_id: str, plan: str, customer_id: str, subscription_id: str):
    AsyncSessionLocal = _get_db()
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("""
                UPDATE accounts
                SET plan = :plan,
                    stripe_customer_id = :customer_id,
                    stripe_subscription_id = :subscription_id
                WHERE id = :account_id
            """),
            {
                "plan": plan,
                "customer_id": customer_id,
                "subscription_id": subscription_id,
                "account_id": account_id,
            }
        )
        await db.commit()


async def _get_account_by_customer(customer_id: str):
    AsyncSessionLocal = _get_db()
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text("SELECT id FROM accounts WHERE stripe_customer_id = :customer_id"),
            {"customer_id": customer_id}
        )
        row = result.fetchone()
        return str(row[0]) if row else None
