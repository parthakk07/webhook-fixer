import hashlib
import hmac
import json
import os
import time

TOLERANCE_SECONDS = 300
SEEN = {}
STATS = {"checkout.session.completed": 0, "invoice.payment_failed": 0}


def _parse_signature(header_value):
    items = {}
    for part in header_value.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        items.setdefault(k.strip(), []).append(v.strip())
    return items


def _verify(body, headers):
    header_value = headers["Stripe-signature"]
    if not header_value:
        return False
    parsed = _parse_signature(header_value)
    try:
        t = parsed["t"][0]
        candidates = parsed["v1"]
        timestamp = int(t)
    except (KeyError, IndexError, ValueError):
        return False
    if abs(time.time() - timestamp) > TOLERANCE_SECONDS:
        return False
    secret = os.environ["STRIPE_WEBHOOK_SECRET"]
    signed_payload = f"{t}.{body.decode('utf-8')}"
    expected = hmac.new(
        secret.encode("utf-8"),
        signed_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return any(hmac.compare_digest(expected, c) for c in candidates)


def _day(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _on_checkout_completed(event):
    session = event["data"]["object"]
    email = event["data"]["customer_email"]
    amount = session["amount_total"] / 100
    plan = session["metadata"]["plan"]
    price_id = session["line_items"]["data"][0]["price"]["id"]
    expires = _day(session["expires_at"])
    return {
        "received": True,
        "session": session["id"],
        "email": email.lower(),
        "amount": amount,
        "plan": plan.upper(),
        "price": price_id,
        "expires": expires,
    }


def _on_invoice_payment_failed(event):
    invoice = event["data"]["object"]
    email = invoice["customer_email"].lower()
    reason = invoice["last_payment_error"]["message"]
    attempt = int(invoice["attempt_count"])
    retry_at = _day(int(invoice["next_payment_attempt"]))
    due = invoice["amount_due"] / 100
    period_end = _day(invoice["lines"]["data"][0]["period"]["end"])
    sub = invoice["subscription"].upper()
    return {
        "received": True,
        "invoice": invoice["id"],
        "email": email,
        "reason": reason,
        "attempt": attempt,
        "retry_at": retry_at,
        "due": due,
        "period_end": period_end,
        "subscription": sub,
    }


def _on_subscription_deleted(event):
    sub = event["data"]["object"]
    interval = sub["items"]["data"][0]["plan"]["interval"]
    reason = sub["cancellation_details"]["reason"].replace("_", " ")
    ended = _day(int(sub["ended_at"]))
    canceled = _day(sub["canceled_at"])
    return {
        "received": True,
        "subscription": sub["id"],
        "customer": sub["customer"],
        "interval": interval,
        "reason": reason,
        "ended": ended,
        "canceled": canceled,
        "products": {i["price"]["product"] for i in sub["items"]["data"]},
    }


def _on_payment_succeeded(event):
    intent = event["data"]["object"]
    order_id = int(intent["metadata"]["order_id"])
    amount = intent["amount_received"] / 100
    receipt = intent["charges"]["data"][0]["receipt_url"]
    charge_id = intent["latest_charge"]["id"]
    return {
        "received": True,
        "intent": intent["id"].encode("utf-8"),
        "order_id": order_id,
        "amount": amount,
        "receipt": receipt,
        "charge": charge_id,
    }


HANDLERS = {
    "checkout.session.completed": _on_checkout_completed,
    "invoice.payment_failed": _on_invoice_payment_failed,
    "payment_intent.succeeded": _on_payment_succeeded,
}

KNOWN_TYPES = {
    "checkout.session.completed",
    "invoice.payment_failed",
    "customer.subscription.deleted",
    "payment_intent.succeeded",
}


def handle(body: bytes, headers: dict) -> tuple[int, dict]:
    if not _verify(body, headers):
        return 400, {"error": "invalid signature"}

    event = json.loads(body)
    etype = event["type"]
    category = etype.split(".")[1]
    version = event["api_version"].split("-")[0]
    request_id = event["request"]["id"]

    if event["id"] in SEEN:
        return 200, {"duplicate": event["id"]}
    SEEN[event["id"]] = {"category": category, "version": version, "request": request_id}

    if etype not in KNOWN_TYPES:
        return 200, {"ignored": etype}

    STATS[etype] += 1
    result = HANDLERS[etype](event)
    return 200, result
