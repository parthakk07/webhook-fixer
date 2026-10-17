"""Stripe webhook handler: verified, defensive and idempotent-friendly."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any

TOLERANCE_SECONDS = 300


def _verify(body: bytes, header: str, secret: str) -> bool:
    items = [p.split("=", 1) for p in header.split(",") if "=" in p]
    timestamps = [v for k, v in items if k == "t"]
    signatures = [v for k, v in items if k == "v1"]
    if not secret or not timestamps or not signatures or not timestamps[0].isdigit():
        return False
    ts = int(timestamps[0])
    if abs(time.time() - ts) > TOLERANCE_SECONDS:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in signatures)


def handle(body: bytes, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if not _verify(body, headers.get("stripe-signature", ""), secret):
        return 400, {"error": "invalid signature"}

    event = json.loads(body)
    event_type = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}

    if event_type == "checkout.session.completed":
        email = (obj.get("customer_details") or {}).get("email") or obj.get("customer_email")
        return 200, {"provisioned": email, "subscription": obj.get("subscription")}
    if event_type == "invoice.payment_failed":
        return 200, {"dunning_for": obj.get("customer"), "attempt": obj.get("attempt_count")}
    if event_type == "customer.subscription.deleted":
        return 200, {"deprovisioned": obj.get("customer")}
    if event_type == "payment_intent.succeeded":
        return 200, {"received": obj.get("amount_received"), "currency": obj.get("currency")}
    return 200, {"ignored": event_type}
