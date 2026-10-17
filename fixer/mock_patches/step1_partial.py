"""Stripe webhook handler (mock patch 1: verifies signatures and fixes JSON access,
but still crashes on event types it does not know)."""

import hashlib
import hmac
import json
import os
import time


def _verify(body, header, secret):
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    if not secret or not parts.get("t", "").isdigit() or "v1" not in parts:
        return False
    if abs(time.time() - int(parts["t"])) > 300:
        return False
    expected = hmac.new(secret.encode(), f"{parts['t']}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts["v1"])


def handle(body, headers):
    if not _verify(body, headers.get("stripe-signature", ""), os.environ.get("STRIPE_WEBHOOK_SECRET", "")):
        return 400, {"error": "invalid signature"}

    event = json.loads(body)
    event_type = event["type"]
    obj = event["data"]["object"]

    if event_type == "checkout.session.completed":
        email = (obj.get("customer_details") or {}).get("email") or obj.get("customer_email")
        return 200, {"provisioned": email}

    if event_type == "invoice.payment_failed":
        return 200, {"dunning_for": obj.get("customer"), "attempt": obj.get("attempt_count")}

    if event_type == "payment_intent.succeeded":
        return 200, {"received": obj.get("amount_received"), "currency": obj.get("currency")}

    raise ValueError(f"Unhandled event type: {event_type}")
