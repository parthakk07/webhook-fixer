"""The agent loop: deploy -> replay events -> collect logs -> LLM patch -> redeploy, until all green."""

from __future__ import annotations

import ast
import difflib
from collections.abc import Iterator
from dataclasses import dataclass, field

from .input_check import check_handler
from .delivery import Delivery, EventResult
from .llm import LLM
from .sandbox import Sandbox


@dataclass
class Iteration:
    number: int
    code: str
    results: list[EventResult]
    logs: str
    rejected_patch: str = ""  # why the LLM's previous patch was refused, if it was

    @property
    def all_ok(self) -> bool:
        return all(r.ok for r in self.results)


@dataclass
class RunResult:
    original: str
    final: str
    iterations: list[Iteration] = field(default_factory=list)
    url: str = ""

    @property
    def success(self) -> bool:
        return bool(self.iterations) and self.iterations[-1].all_ok

    @property
    def diff(self) -> str:
        return "".join(difflib.unified_diff(self.original.splitlines(True), self.final.splitlines(True),
                                            "broken_handler.py", "fixed_handler.py"))


@dataclass
class Progress:
    kind: str  # "started" | "deploying" | "iteration" | "patching" | "done"
    message: str = ""
    iteration: Iteration | None = None
    result: RunResult | None = None


def validate_patch(code: str) -> str:
    """Return "" if the patch is acceptable, else the reason it is refused."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"SyntaxError: {exc}"
    if not any(isinstance(n, ast.FunctionDef) and n.name == "handle" for n in tree.body):
        return "missing top-level def handle(body, headers)"
    if "compare_digest" not in code or "hmac" not in code:
        return "signature verification removed (must use hmac + compare_digest on the raw body)"
    banned = [m for m in ("flask", "stripe", "requests") if f"import {m}" in code or f"from {m}" in code]
    if banned:
        return f"non-stdlib import not available in sandbox: {banned}"
    problems = check_handler(code)
    return "; ".join(problems)


def build_feedback(it: Iteration) -> str:
    rows = "\n".join(f"- {r.event}: got {r.status}, expected {r.expected}{'  OK' if r.ok else '  FAIL'}"
                     + (f"\n  traceback: {r.error.strip()[-1200:]}" if r.error and not r.ok else "")
                     for r in it.results)
    note = ("\nNOTE: 'forged-signature' is a payment_intent.succeeded event signed with the WRONG "
            "secret; a correct handler must reject it with 400.")
    refused = f"\nYour previous patch was REFUSED: {it.rejected_patch}" if it.rejected_patch else ""
    return (f"Iteration {it.number} replay results:\n{rows}{note}{refused}\n\n"
            f"Sandbox server log (tail):\n{it.logs[-3000:]}")


MAX_ITERS = 5  # hard cap: the agent never tries more than 5 rounds


def fix_session(handler_code: str, events: list[str], sandbox: Sandbox, delivery: Delivery,
                llm: LLM, max_iters: int = 5) -> Iterator[Progress]:
    """Generator driving one full repair session. Always tears down the sandbox and Stripe endpoint."""
    max_iters = max(1, min(max_iters, MAX_ITERS))
    result = RunResult(original=handler_code, final=handler_code)
    try:
        result.url = sandbox.start()
        secret = delivery.setup(result.url)
        yield Progress("started", f"sandbox up at {result.url} (delivery: {delivery.name}, llm: {llm.name})")
        code, rejected = handler_code, ""
        for n in range(1, max_iters + 1):
            yield Progress("deploying", f"iteration {n}: deploying handler")
            sandbox.deploy(code, secret)
            it = Iteration(n, code, delivery.send(events, sandbox), sandbox.logs(), rejected)
            result.iterations.append(it)
            result.final = code
            yield Progress("iteration", f"iteration {n}: {sum(r.ok for r in it.results)}/{len(it.results)} passing", it)
            if it.all_ok or n == max_iters:
                break
            yield Progress("patching", f"iteration {n}: asking {llm.name} for a patch")
            candidate = llm.patch(code, build_feedback(it))
            rejected = validate_patch(candidate)
            if not rejected:
                code = candidate
    finally:
        for closer in (delivery.teardown, sandbox.close):
            try:
                closer()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
    yield Progress("done", "all events green" if result.success else "stopped before all green", result=result)
