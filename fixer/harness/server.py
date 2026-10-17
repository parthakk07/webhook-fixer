"""Fixed HTTP harness that runs INSIDE the sandbox (stdlib only, never edited by the LLM).

It loads handler.py (which must define ``handle(body: bytes, headers: dict) -> (status, payload)``),
serves it on 0.0.0.0:$PORT, turns uncaught exceptions into 500s with the traceback printed to
stdout (-> server.log), and appends one JSON line per request to $REQUEST_LOG.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
HANDLER_PATH = os.environ.get("HANDLER_PATH", os.path.join(HERE, "handler.py"))
REQUEST_LOG = os.environ.get("REQUEST_LOG", os.path.join(HERE, "requests.jsonl"))
PORT = int(os.environ.get("PORT", "8080"))

handler_module = None
import_error = ""
try:
    spec = importlib.util.spec_from_file_location("handler", HANDLER_PATH)
    handler_module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(handler_module)  # type: ignore[union-attr]
except Exception:  # noqa: BLE001 - surface any import failure to the agent
    import_error = traceback.format_exc()
    print("HANDLER IMPORT FAILED\n" + import_error, flush=True)


class Harness(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        print("%s %s" % (self.address_string(), fmt % args), flush=True)

    def _reply(self, status: int, payload: object) -> None:
        data = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
        raw = data.encode() if isinstance(data, str) else data
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # health check used by the agent
        self._reply(200, {"harness": "webhook-fixer", "handler_loaded": handler_module is not None})

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        headers = {k.lower(): v for k, v in self.headers.items()}
        event_id, event_type = "", ""
        try:
            parsed = json.loads(body or b"{}")
            event_id, event_type = str(parsed.get("id", "")), str(parsed.get("type", ""))
        except Exception:  # noqa: BLE001
            pass
        error = ""
        if handler_module is None:
            status, payload, error = 500, {"error": "handler failed to import"}, import_error
        else:
            try:
                status, payload = handler_module.handle(body, headers)
                status = int(status)
            except Exception:  # noqa: BLE001
                error = traceback.format_exc()
                print(f"Unhandled exception for {event_type} {event_id}\n{error}", flush=True)
                status, payload = 500, {"error": "internal error"}
        self._reply(status, payload)
        record = {"ts": time.time(), "event_id": event_id, "event_type": event_type,
                  "status": status, "error": error[-2000:]}
        with open(REQUEST_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")


if __name__ == "__main__":
    print(f"harness listening on 0.0.0.0:{PORT} handler={HANDLER_PATH}", flush=True)
    sys.stdout.flush()
    ThreadingHTTPServer(("0.0.0.0", PORT), Harness).serve_forever()
