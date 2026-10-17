"""Where the handler runs: a real CreateOS sandbox (public ingress URL) or a local subprocess (dry run)."""

from __future__ import annotations

import json
import os
import secrets
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Protocol

import requests

HARNESS_SRC = (Path(__file__).parent / "harness" / "server.py").read_text()
PORT = 8080
APP_DIR = "/app"

# Runs inside the sandbox: POST a request to the harness over loopback (used when ingress is unreachable).
_INTERNAL_POST = (
    "import json,sys,urllib.request,urllib.error\n"
    "r=json.load(sys.stdin)\n"
    "q=urllib.request.Request('http://127.0.0.1:%d/webhook',data=r['body'].encode(),headers=r['headers'],method='POST')\n"
    "try:\n print(urllib.request.urlopen(q,timeout=15).status)\n"
    "except urllib.error.HTTPError as e:\n print(e.code)\n" % PORT
)


class Sandbox(Protocol):
    url: str

    def start(self) -> str: ...
    def deploy(self, handler_code: str, webhook_secret: str) -> None: ...
    def logs(self, max_chars: int = 6000) -> str: ...
    def request_log(self) -> list[dict]: ...
    def post_internal(self, body: bytes, headers: dict[str, str]) -> int: ...
    def close(self) -> None: ...


def _parse_jsonl(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


class LocalSandbox:
    """Runs the harness as a local subprocess. Same contract as CreateOS; used by --dry-run and tests."""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="webhook-fixer-"))
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.url = ""

    def start(self) -> str:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        (self.dir / "server.py").write_text(HARNESS_SRC)
        self.url = f"http://127.0.0.1:{self.port}"
        return self.url

    def deploy(self, handler_code: str, webhook_secret: str) -> None:
        self._stop()
        (self.dir / "handler.py").write_text(handler_code)
        for name in ("server.log", "requests.jsonl"):
            (self.dir / name).write_text("")
        env = {**os.environ, "PORT": str(self.port), "STRIPE_WEBHOOK_SECRET": webhook_secret}
        log = open(self.dir / "server.log", "a")  # noqa: SIM115 - owned by the child
        self.proc = subprocess.Popen([sys.executable, str(self.dir / "server.py")],
                                     env=env, stdout=log, stderr=subprocess.STDOUT, cwd=self.dir)
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                requests.get(self.url, timeout=1)
                return
            except requests.RequestException:
                time.sleep(0.1)
        raise TimeoutError("local harness did not start:\n" + self.logs())

    def logs(self, max_chars: int = 6000) -> str:
        return (self.dir / "server.log").read_text()[-max_chars:]

    def request_log(self) -> list[dict]:
        return _parse_jsonl((self.dir / "requests.jsonl").read_text())

    def post_internal(self, body: bytes, headers: dict[str, str]) -> int:
        return requests.post(self.url + "/webhook", data=body, headers=headers, timeout=15).status_code

    def _stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=5)

    def close(self) -> None:
        self._stop()


class CreateOSSandbox:
    """A CreateOS sandbox with public ingress. Requires CREATEOS_API_KEY."""

    def __init__(self, shape: str | None = None, rootfs: str | None = None) -> None:
        from createos import Client  # imported lazily so dry runs need no key

        if not os.environ.get("CREATEOS_API_KEY") and os.environ.get("CREATEOS_SANDBOX_API_KEY"):
            os.environ["CREATEOS_API_KEY"] = os.environ["CREATEOS_SANDBOX_API_KEY"]
        self.client = Client()
        self.shape = shape or os.environ.get("SANDBOX_SHAPE", "s-1vcpu-1gb")
        self.rootfs = rootfs or os.environ.get("SANDBOX_ROOTFS", "devbox:1")
        self.sb = None
        self.process_id = ""
        self.url = ""

    # -- helpers -----------------------------------------------------------------------------
    def _run(self, script: str, stdin: str = "", timeout: float = 60) -> tuple[int, str, str]:
        from createos import ExecOptions, RunCommandRequest

        resp = self.sb.run_command(  # type: ignore[union-attr]
            RunCommandRequest(command="sh", arguments=["-c", script], standard_input=stdin),
            ExecOptions(timeout=timeout),
        )
        r = resp.result
        return r.exit_code, r.standard_output, r.standard_error or r.error_message

    def _upload(self, path: str, text: str) -> None:
        try:
            self.sb.files.upload(path, text.encode())  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - fall back to stdin piping
            code, _, err = self._run(f"mkdir -p {APP_DIR} && cat > {shlex.quote(path)}", stdin=text)
            if code:
                raise RuntimeError(f"upload of {path} failed: {err}")

    # -- contract ----------------------------------------------------------------------------
    def start(self) -> str:
        from createos import CreateSandboxRequest

        self.sb = self.client.create_sandbox(CreateSandboxRequest(
            shape=self.shape, rootfs=self.rootfs, ingress_enabled=True,
            name=f"webhook-fixer-{secrets.token_hex(3)}"))
        try:
            self.sb.wait_until_running()
        except Exception:  # noqa: BLE001 - create already returns a running sandbox on most plans
            pass
        try:
            url = self.sb.preview_url(PORT)
        except ValueError:  # ingress template missing from create response: refresh, else enable
            self.sb.refresh()
            if not self.sb.data.ingress_enabled:
                self.sb.set_ingress(True)
            url = self.sb.preview_url(PORT)
        self.url = (url if "://" in url else "https://" + url).rstrip("/")
        self._run(f"mkdir -p {APP_DIR}")
        self._upload(f"{APP_DIR}/server.py", HARNESS_SRC)
        return self.url

    def deploy(self, handler_code: str, webhook_secret: str) -> None:
        from createos import ManagedProcessCreateRequest

        self._upload(f"{APP_DIR}/handler.py", handler_code)
        if self.process_id:
            try:
                self.sb.processes.delete(self.process_id)  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass
        self._run(f"pkill -f 'python3 {APP_DIR}/server[.]py'; sleep 0.3; : > {APP_DIR}/server.log; : > {APP_DIR}/requests.jsonl; true")
        env = {"PORT": str(PORT), "STRIPE_WEBHOOK_SECRET": webhook_secret, "PYTHONUNBUFFERED": "1"}
        start = f"exec python3 {APP_DIR}/server.py >> {APP_DIR}/server.log 2>&1"
        try:
            proc = self.sb.processes.create(ManagedProcessCreateRequest(  # type: ignore[union-attr]
                command="sh", arguments=["-c", start], working_directory=APP_DIR,
                environment_variables=env))
            self.process_id = proc.process_id
        except Exception:  # noqa: BLE001 - fall back to a daemonised shell process
            exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
            self._run(f"cd {APP_DIR} && {exports} nohup setsid sh -c {shlex.quote(start)} >/dev/null 2>&1 &")
            self.process_id = ""
        self.sb.wait_for_port(PORT, timeout=30)  # type: ignore[union-attr]

    def logs(self, max_chars: int = 6000) -> str:
        return self._run(f"tail -c {max_chars} {APP_DIR}/server.log")[1]

    def request_log(self) -> list[dict]:
        return _parse_jsonl(self._run(f"cat {APP_DIR}/requests.jsonl")[1])

    def post_internal(self, body: bytes, headers: dict[str, str]) -> int:
        payload = json.dumps({"body": body.decode(), "headers": headers})
        code, out, err = self._run(f"python3 -c {shlex.quote(_INTERNAL_POST)}", stdin=payload)
        try:
            return int(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise RuntimeError(f"internal POST failed: {err or out}") from None

    def close(self) -> None:
        if self.sb is not None:
            try:
                self.sb.destroy()
            finally:
                self.sb = None
        self.client.close() if hasattr(self.client, "close") else None
