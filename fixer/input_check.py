"""Gatekeeper for user-supplied code: only accept something that is really a Stripe webhook handler.

Runs before anything is sent to the sandbox or the LLM, so random scripts, empty input, or code that
tries to touch the machine (shell, network, files) are refused with a clear list of reasons.
"""

from __future__ import annotations

import ast
import re

MAX_BYTES = 30_000
ALLOWED_IMPORTS = {
    "__future__", "json", "hmac", "hashlib", "time", "os", "datetime", "logging", "typing", "base64",
    "re", "collections", "dataclasses", "decimal", "math", "functools", "uuid", "enum", "secrets",
}
BANNED_CALLS = {"eval", "exec", "compile", "__import__", "open", "input", "breakpoint", "globals", "setattr"}
BANNED_ATTRS = {"system", "popen", "spawnl", "spawnv", "execv", "execl", "fork", "kill", "remove", "unlink",
                "rmdir", "removedirs", "rename", "chmod", "chown", "putenv", "_exit"}
STRIPE_EVENT_RE = re.compile(
    r"""["'](checkout\.session|invoice|payment_intent|customer|charge|subscription_schedule|price|product|"""
    r"""payout|refund|setup_intent|account|balance|issuing|radar|review)\.[a-z_.]+["']""")


def check_handler(code: str) -> list[str]:
    """Return a list of problems. Empty list means: this looks like a Stripe webhook handler."""
    problems: list[str] = []
    if not code.strip():
        return ["The box is empty. Paste your Stripe webhook handler (a Python file with def handle(body, headers))."]
    if len(code.encode()) > MAX_BYTES:
        return [f"File is too large ({len(code.encode())//1000} KB). A webhook handler should be under 30 KB."]
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"This isn't valid Python (line {exc.lineno}: {exc.msg}). Paste a Python webhook handler."]

    handle = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "handle"), None)
    if handle is None:
        problems.append("Missing a top-level function `def handle(body, headers)` that returns (status, payload).")
    elif len(handle.args.args) != 2:
        problems.append("`handle` must take exactly two arguments: (body, headers).")

    low = code.lower()
    if "stripe-signature" not in low and "stripe_signature" not in low:
        problems.append("No `Stripe-Signature` header handling found. A Stripe webhook must read that header.")
    # A missing HMAC check is a *bug the agent fixes*, not a reason to refuse input.
    if not STRIPE_EVENT_RE.search(code):
        problems.append("No Stripe event types found (e.g. 'checkout.session.completed', 'invoice.payment_failed').")

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for name in names:
                root = name.split(".")[0]
                if root not in ALLOWED_IMPORTS:
                    problems.append(f"Import `{name}` is not allowed. Only standard-library basics "
                                    f"(json, hmac, hashlib, time, os, datetime…) can run in the sandbox.")
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in BANNED_CALLS:
                problems.append(f"`{fn.id}()` is not allowed in a webhook handler.")
            if isinstance(fn, ast.Attribute) and fn.attr in BANNED_ATTRS:
                problems.append(f"`.{fn.attr}()` is not allowed (no shell, process or file operations).")
    return list(dict.fromkeys(problems))  # dedupe, keep order
