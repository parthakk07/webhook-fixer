"""How Stripe events reach the sandbox.

* LocalSignedDelivery (default, robust): builds realistic Stripe event payloads, signs them with the
  endpoint secret exactly like Stripe (t=..,v1=HMAC-SHA256) and POSTs them to the public sandbox URL.
  If the public URL is unreachable it falls back to POSTing over loopback inside the sandbox.
* StripeDelivery: registers a real test-mode webhook endpoint pointing at the sandbox URL via the
  Stripe API, then fires real events with `stripe trigger <event>`; Stripe itself delivers them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Protocol

import requests

from .events import FORGED_PROBE, forged_request, new_secret, signed_request
from .sandbox import Sandbox

STRIPE_API = "https://api.stripe.com/v1"


@dataclass
class EventResult:
    event: str
    status: int | None  # None = never delivered
    expected: str  # "2xx" or "4xx"
    event_id: str = ""
    error: str = ""
    via: str = ""

    @property
    def ok(self) -> bool:
        if self.status is None:
            return False
        return 200 <= self.status < 300 if self.expected == "2xx" else 400 <= self.status < 500


class Delivery(Protocol):
    name: str

    def setup(self, url: str) -> str: ...  # returns the webhook signing secret
    def send(self, events: list[str], sandbox: Sandbox) -> list[EventResult]: ...
    def teardown(self) -> None: ...


def _errors_by_id(sandbox: Sandbox) -> dict[str, str]:
    return {r.get("event_id", ""): r.get("error", "") for r in sandbox.request_log()}


class LocalSignedDelivery:
    name = "local-signed"

    def __init__(self) -> None:
        self.secret = ""
        self.url = ""
        self.use_public: bool | None = None  # decided lazily by a health check

    def setup(self, url: str) -> str:
        self.url, self.secret = url, new_secret()
        return self.secret

    def _public_reachable(self) -> bool:
        try:
            r = requests.get(self.url + "/healthz", timeout=10)
            return r.ok and "webhook-fixer" in r.text
        except requests.RequestException:
            return False

    def _post(self, body: bytes, headers: dict[str, str], sandbox: Sandbox) -> tuple[int, str]:
        if self.use_public is None:
            self.use_public = self._public_reachable()
        if self.use_public:
            try:
                return requests.post(self.url + "/webhook", data=body, headers=headers, timeout=20).status_code, "public-url"
            except requests.RequestException:
                self.use_public = False
        return sandbox.post_internal(body, headers), "in-sandbox"

    def send(self, events: list[str], sandbox: Sandbox) -> list[EventResult]:
        results = []
        for ev in events:
            eid, body, headers = signed_request(ev, self.secret)
            status, via = self._post(body, headers, sandbox)
            results.append(EventResult(ev, status, "2xx", eid, via=via))
        eid, body, headers = forged_request()
        status, via = self._post(body, headers, sandbox)
        results.append(EventResult(FORGED_PROBE, status, "4xx", eid, via=via))
        errors = _errors_by_id(sandbox)
        for r in results:
            r.error = errors.get(r.event_id, "")
        return results

    def teardown(self) -> None:
        pass


class StripeDelivery:
    """Real Stripe test-mode delivery. Needs STRIPE_SECRET_KEY (sk_test_...) and the `stripe` CLI."""

    name = "stripe"

    def __init__(self, events: list[str], wait_seconds: float = 60) -> None:
        key = os.environ.get("STRIPE_SECRET_KEY", "")
        if not key.startswith(("sk_test_", "rk_test_")):
            raise RuntimeError("StripeDelivery needs a TEST mode STRIPE_SECRET_KEY (sk_test_...)")
        if not shutil.which("stripe"):
            raise RuntimeError("`stripe` CLI not found on PATH")
        self.key, self.events, self.wait = key, events, wait_seconds
        self.endpoint_id = ""
        self.local = LocalSignedDelivery()  # used for the forged-signature probe

    def setup(self, url: str) -> str:
        data = [("url", url + "/webhook"), ("description", "webhook-fixer sandbox (auto-deleted)")]
        data += [("enabled_events[]", e) for e in self.events]
        r = requests.post(f"{STRIPE_API}/webhook_endpoints", auth=(self.key, ""), data=data, timeout=30)
        if not r.ok:
            raise RuntimeError(f"Stripe webhook endpoint creation failed: {r.status_code} {r.text}")
        body = r.json()
        self.endpoint_id = body["id"]
        self.local.setup(url)
        return body["secret"]

    def send(self, events: list[str], sandbox: Sandbox) -> list[EventResult]:
        t0 = time.time()
        for ev in events:
            subprocess.run(["stripe", "trigger", ev, "--api-key", self.key],
                           capture_output=True, text=True, timeout=120, check=False)
        latest: dict[str, dict] = {}
        deadline = time.time() + self.wait
        while time.time() < deadline:
            for rec in sandbox.request_log():
                if rec.get("ts", 0) >= t0 - 5 and rec.get("event_type") in events:
                    latest[rec["event_type"]] = rec
            if all(e in latest for e in events):
                break
            time.sleep(2)
        results = [EventResult(e, latest[e]["status"], "2xx", latest[e].get("event_id", ""),
                               latest[e].get("error", ""), "stripe")
                   if e in latest else EventResult(e, None, "2xx", error="not delivered by Stripe in time", via="stripe")
                   for e in events]
        probe = self.local.send([], sandbox)  # only the forged probe
        return results + probe

    def teardown(self) -> None:
        if self.endpoint_id:
            requests.delete(f"{STRIPE_API}/webhook_endpoints/{self.endpoint_id}", auth=(self.key, ""), timeout=30)
            self.endpoint_id = ""
