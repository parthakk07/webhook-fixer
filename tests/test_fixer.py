"""Offline tests: signing, patch validation, the full loop (local sandbox + mock LLM), CreateOS wiring."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import createos
import pytest

from fixer import sandbox as sandbox_mod
from fixer.delivery import LocalSignedDelivery
from fixer.events import DEFAULT_EVENTS, FORGED_PROBE, forged_request, make_event, new_secret, sign, verify
from fixer.llm import MockLLM, extract_code
from fixer.loop import fix_session, validate_patch

ROOT = Path(__file__).resolve().parents[1]
BROKEN = (ROOT / "examples" / "broken_handler.py").read_text()
FULL_FIX = (ROOT / "fixer" / "mock_patches" / "step2_full.py").read_text()


def test_sign_and_verify_roundtrip() -> None:
    secret, body = new_secret(), json.dumps(make_event("invoice.payment_failed")).encode()
    header = sign(body, secret)
    assert verify(body, header, secret)
    assert not verify(body + b" ", header, secret)
    assert not verify(body, header, new_secret())
    assert not verify(body, sign(body, secret, timestamp=1), secret)  # stale


def test_validate_patch_guards() -> None:
    assert validate_patch(FULL_FIX) == ""
    assert "SyntaxError" in validate_patch("def handle(:\n")
    assert "missing" in validate_patch("import hmac\nhmac.compare_digest\n")
    assert "signature verification removed" in validate_patch(BROKEN)
    assert "non-stdlib" in validate_patch(FULL_FIX + "\nimport stripe\n")


def test_extract_code() -> None:
    assert extract_code("Sure!\n```python\nx = 1\n```\n") == "x = 1\n"


def run(llm: MockLLM, max_iters: int = 5):
    progress = list(fix_session(BROKEN, DEFAULT_EVENTS, sandbox_mod.LocalSandbox(), LocalSignedDelivery(), llm, max_iters))
    return progress[-1].result


def test_full_loop_goes_green() -> None:
    llm = MockLLM()
    result = run(llm)
    assert result.success and len(result.iterations) == 3
    first = {r.event: r for r in result.iterations[0].results}
    assert first["checkout.session.completed"].status == 500 and "KeyError" in first["checkout.session.completed"].error
    assert first["customer.subscription.deleted"].status == 500
    assert first["payment_intent.succeeded"].status == 200
    assert first[FORGED_PROBE].status == 200 and not first[FORGED_PROBE].ok  # broken handler accepts forgeries
    assert all(r.ok for r in result.iterations[-1].results)
    assert "compare_digest" in result.final and "+" in result.diff
    assert "KeyError" in llm.calls[0]  # tracebacks reach the LLM


def test_refused_patch_is_not_deployed() -> None:
    result = run(MockLLM([BROKEN.replace("BROKEN", "still broken")]), max_iters=2)
    assert not result.success
    assert result.iterations[1].code == BROKEN
    assert "signature verification removed" in result.iterations[1].rejected_patch


def test_harness_survives_import_error() -> None:
    sb = sandbox_mod.LocalSandbox()
    try:
        sb.start()
        sb.deploy("this is not python", new_secret())
        _, body, headers = forged_request()
        assert sb.post_internal(body, headers) == 500
        assert "HANDLER IMPORT FAILED" in sb.logs()
    finally:
        sb.close()


class _FakeSDKSandbox:
    """Stands in for createos.SandboxInstance; asserts we pass real SDK request models."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.data = SimpleNamespace(ingress_enabled=True)
        self.files = SimpleNamespace(upload=lambda path, data: self.calls.append(("upload", path)))
        self.processes = SimpleNamespace(create=self._create, delete=lambda pid: self.calls.append(("delete", pid)))

    def _create(self, req):
        assert isinstance(req, createos.ManagedProcessCreateRequest)
        assert req.environment_variables["PORT"] == "8080"
        self.calls.append(("process", req.arguments[-1]))
        return SimpleNamespace(process_id="proc-1")

    def wait_until_running(self):
        return self

    def preview_url(self, port: int) -> str:
        return f"https://sb-abc-{port}.ingress.example"

    def run_command(self, req, opts):
        assert isinstance(req, createos.RunCommandRequest) and isinstance(opts, createos.ExecOptions)
        self.calls.append(("exec", req.arguments[-1]))
        return SimpleNamespace(result=SimpleNamespace(exit_code=0, standard_output="200\n", standard_error="", error_message=""))

    def wait_for_port(self, port: int, timeout: float) -> None:
        self.calls.append(("port", port))

    def destroy(self) -> None:
        self.calls.append(("destroy",))


def test_createos_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSDKSandbox()

    class FakeClient:
        def create_sandbox(self, req):
            assert isinstance(req, createos.CreateSandboxRequest) and req.ingress_enabled
            assert req.shape == "s-1vcpu-1gb" and req.rootfs == "devbox:1"
            return fake

    monkeypatch.setattr(createos, "Client", FakeClient)
    sb = sandbox_mod.CreateOSSandbox()
    assert sb.start() == "https://sb-abc-8080.ingress.example"
    sb.deploy(FULL_FIX, "whsec_x")
    sb.deploy(FULL_FIX, "whsec_x")
    assert sb.post_internal(b"{}", {}) == 200
    sb.close()
    kinds = [c[0] for c in fake.calls]
    assert kinds.count("process") == 2 and ("delete", "proc-1") in fake.calls and kinds[-1] == "destroy"


def test_never_more_than_five_rounds():
    result = run(MockLLM([BROKEN.replace("BROKEN", "still broken")] * 20), max_iters=50)
    assert len(result.iterations) <= 5


def test_non_json_response_is_500_not_a_crash():
    bad = BROKEN.replace("def handle(", "def _orig_handle(", 1) + (
        "\n\ndef handle(body, headers):\n    s, p = _orig_handle(body, headers)\n    return s, {'x': {1, 2}}\n")
    sb = sandbox_mod.LocalSandbox(); sb.start()
    try:
        sb.deploy(bad, "whsec_test")
        assert sb.post_internal(b'{"id":"e","type":"x"}', {"Content-Type": "application/json"}) in (400, 500)
    finally:
        sb.close()
