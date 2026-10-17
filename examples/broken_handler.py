"""Stripe webhook handler for our SaaS billing (BROKEN - this is what the agent must fix).

Contract used by the harness: handle(body: bytes, headers: dict[str, str]) -> (status, payload).
Header names are lower-cased. The signing secret is in env STRIPE_WEBHOOK_SECRET.
"""

import json
import os

WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")


def handle(body, headers):
    # BUG 1: only checks that the header exists. Never verifies the HMAC over the raw body,
    # so anyone can POST a forged event and we will happily fulfil it.
    if "stripe-signature" not in headers:
        return 400, {"error": "missing signature"}

    event = json.loads(body)
    event_type = event["type"]

    if event_type == "checkout.session.completed":
        # BUG 2: wrong JSON path - the session lives under data.object, and the email under
        # customer_details.email. This raises KeyError -> 500.
        email = event["data"]["customer_email"]
        return 200, {"provisioned": email}

    if event_type == "invoice.payment_failed":
        invoice = event["data"]["object"]
        # BUG 3: assumes fields that are not on an Invoice (and customer_email may be null).
        email = invoice["customer_email"].lower()
        reason = invoice["last_payment_error"]["message"]
        return 200, {"dunning_email": email, "reason": reason}

    if event_type == "payment_intent.succeeded":
        intent = event["data"]["object"]
        return 200, {"received": intent["amount_received"], "currency": intent["currency"]}

    # BUG 4: every other event type (e.g. customer.subscription.deleted) blows up -> 500,
    # so Stripe keeps retrying and eventually disables the endpoint.
    raise ValueError(f"Unhandled event type: {event_type}")
