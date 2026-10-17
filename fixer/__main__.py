"""CLI: python -m fixer run examples/broken_handler.py --events a,b,c [--dry-run]"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from .delivery import LocalSignedDelivery, StripeDelivery
from .events import DEFAULT_EVENTS
from .llm import MockLLM, OpenAICompatLLM
from .loop import RunResult, fix_session
from .sandbox import CreateOSSandbox, LocalSandbox


def build(args: argparse.Namespace, events: list[str]):
    sandbox = LocalSandbox() if args.dry_run or args.sandbox == "local" else CreateOSSandbox()
    llm = MockLLM() if args.dry_run or args.llm == "mock" else OpenAICompatLLM(args.model)
    delivery = StripeDelivery(events) if args.delivery == "stripe" and not args.dry_run else LocalSignedDelivery()
    return sandbox, delivery, llm


def grid_line(it) -> str:
    return "  ".join(f"{'🟢' if r.ok else '🔴'} {r.event}={r.status}" for r in it.results)


def save(result: RunResult, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "fixed_handler.py").write_text(result.final)
    (out / "fix.diff").write_text(result.diff)
    report = {"success": result.success, "url": result.url, "iterations": [
        {"n": it.number, "results": [vars(r) | {"ok": r.ok} for r in it.results]} for it in result.iterations]}
    (out / "report.json").write_text(json.dumps(report, indent=2))


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(prog="fixer", description="Fix a broken Stripe webhook handler in a CreateOS sandbox")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("handler", type=Path)
    r.add_argument("--events", default=",".join(DEFAULT_EVENTS))
    r.add_argument("--max-iters", type=int, default=5)
    r.add_argument("--delivery", choices=["local", "stripe"], default="local",
                   help="local = sign fixtures + POST (default); stripe = real endpoint + `stripe trigger`")
    r.add_argument("--sandbox", choices=["createos", "local"], default="createos")
    r.add_argument("--llm", choices=["real", "mock"], default="real")
    r.add_argument("--model", default=None, help="override FIXER_MODEL")
    r.add_argument("--dry-run", action="store_true", help="offline: local sandbox + mock LLM + local delivery")
    r.add_argument("--out", type=Path, default=Path("out"))
    args = p.parse_args(argv)

    events = [e.strip() for e in args.events.split(",") if e.strip()]
    try:
        sandbox, delivery, llm = build(args, events)
    except Exception as exc:  # noqa: BLE001 - missing keys, missing stripe CLI, ...
        print(f"setup error: {exc}\nTip: fill .env (see .env.example) or use --dry-run", file=sys.stderr)
        return 2
    result: RunResult | None = None
    for prog in fix_session(args.handler.read_text(), events, sandbox, delivery, llm, args.max_iters):
        print(f"[{prog.kind}] {prog.message}", flush=True)
        if prog.iteration:
            print("   " + grid_line(prog.iteration), flush=True)
        result = prog.result or result
    if result is None:
        return 2
    save(result, args.out)
    print("\n" + result.diff)
    print(f"\n{'✅ fixed' if result.success else '❌ not fixed'} in {len(result.iterations)} iteration(s). "
          f"Outputs in {args.out}/ (fixed_handler.py, fix.diff, report.json)")
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
