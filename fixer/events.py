"""Realistic Stripe event fixtures and Stripe-compatible signing (t=..,v1=HMAC-SHA256)."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from typing import Any

DEFAULT_EVENTS = [
    "checkout.session.completed",
    "invoice.payment_failed",
    "customer.subscription.deleted",
    "payment_intent.succeeded",
]
FORGED_PROBE = "forged-signature"  # security probe: must be rejected with 4xx
API_VERSION = "2025-09-30.clover"


def _id(prefix: str) -> str:
    return f"{prefix}_test_{secrets.token_hex(8)}"


def _object(event_type: str) -> dict[str, Any]:
    customer, now = _id("cus"), int(time.time())
    if event_type == "checkout.session.completed":
        return {"id": _id("cs"), "object": "checkout.session", "mode": "subscription",
                "amount_total": 2900, "currency": "usd", "customer": customer,
                "customer_details": {"email": "jenny.rosen@example.com", "name": "Jenny Rosen"},
                "customer_email": None, "payment_status": "paid", "status": "complete",
                "subscription": _id("sub"), "created": now}
    if event_type == "invoice.payment_failed":
        return {"id": _id("in"), "object": "invoice", "customer": customer,
                "customer_email": None, "amount_due": 2900, "amount_paid": 0,
                "attempt_count": 2, "billing_reason": "subscription_cycle", "currency": "usd",
                "next_payment_attempt": now + 3 * 86400, "status": "open", "created": now}
    if event_type == "customer.subscription.deleted":
        return {"id": _id("sub"), "object": "subscription", "customer": customer,
                "status": "canceled", "canceled_at": now, "cancel_at_period_end": False,
                "items": {"object": "list", "data": [
                    {"id": _id("si"), "price": {"id": _id("price"), "unit_amount": 2900}}]},
                "created": now - 30 * 86400}
    if event_type == "payment_intent.succeeded":
        return {"id": _id("pi"), "object": "payment_intent", "amount": 2900,
                "amount_received": 2900, "currency": "usd", "customer": customer,
                "status": "succeeded", "created": now}
    return {"id": _id("obj"), "object": event_type.split(".")[0]}


def make_event(event_type: str) -> dict[str, Any]:
    """Build a Stripe Event envelope shaped like the real API (test mode)."""
    return {"id": _id("evt"), "object": "event", "api_version": API_VERSION,
            "created": int(time.time()), "livemode": False, "pending_webhooks": 1,
            "request": {"id": None, "idempotency_key": None}, "type": event_type,
            "data": {"object": _object(event_type)}}


def new_secret() -> str:
    return "whsec_" + secrets.token_hex(24)


def sign(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """Return a Stripe-Signature header value exactly as Stripe computes it."""
    ts = int(time.time()) if timestamp is None else timestamp
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify(payload: bytes, header: str, secret: str, tolerance: int = 300) -> bool:
    """Reference verifier (used in tests)."""
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    try:
        ts = int(parts["t"])
    except (KeyError, ValueError):
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    sigs = [v for k, v in (p.split("=", 1) for p in header.split(",") if "=" in p) if k == "v1"]
    fresh = abs(time.time() - ts) <= tolerance
    return fresh and any(hmac.compare_digest(expected, s) for s in sigs)


def signed_request(event_type: str, secret: str) -> tuple[str, bytes, dict[str, str]]:
    """(event_id, raw body, headers) for a correctly signed event."""
    event = make_event(event_type)
    body = json.dumps(event, separators=(",", ":")).encode()
    return event["id"], body, {"Content-Type": "application/json", "Stripe-Signature": sign(body, secret)}


def forged_request() -> tuple[str, bytes, dict[str, str]]:
    """A payment_intent.succeeded event signed with the WRONG secret (an attacker faking a payment)."""
    return signed_request("payment_intent.succeeded", new_secret())
