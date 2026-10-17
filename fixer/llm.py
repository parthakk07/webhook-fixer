"""The patching brain: an OpenAI-compatible chat model (OpenRouter or OpenAI) or a scripted mock."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Protocol

SYSTEM_PROMPT = """You are a senior payments engineer fixing a Stripe webhook handler.
It runs inside a sandbox behind a fixed harness that calls:
    handle(body: bytes, headers: dict[str, str]) -> tuple[int, dict]
Header names are lower-cased. The signing secret is in os.environ["STRIPE_WEBHOOK_SECRET"].

Hard requirements:
1. Verify the Stripe-Signature header against the RAW body bytes before parsing JSON:
   parse t=<ts> and every v1=<sig>, compute HMAC-SHA256(secret, f"{t}." + raw_body), compare with
   hmac.compare_digest, reject timestamps older than 300s. Invalid/missing signature -> return 400.
   Never remove or weaken verification.
2. Use only the Python standard library (no stripe package, no flask).
3. Access Stripe objects via event["data"]["object"]; use .get() for optional/nullable fields.
4. Return 200 for every correctly signed event, including event types you do not act on
   (acknowledge them) so Stripe stops retrying. Never raise for a valid event.
5. Keep existing business intent (provisioning, dunning, etc.) and keep the code small and typed.
Reply with the COMPLETE new handler.py in a single ```python code block and nothing else."""


class LLM(Protocol):
    name: str

    def patch(self, code: str, feedback: str) -> str: ...


def extract_code(text: str) -> str:
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    return (max(blocks, key=len) if blocks else text).strip() + "\n"


class OpenAICompatLLM:
    """OPENROUTER_API_KEY (preferred) or OPENAI_API_KEY. Model via FIXER_MODEL."""

    def __init__(self, model: str | None = None) -> None:
        from openai import OpenAI

        if os.environ.get("OPENROUTER_API_KEY"):
            self.client = OpenAI(api_key=os.environ["OPENROUTER_API_KEY"],
                                 base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"))
            default = "anthropic/claude-sonnet-4"
        elif os.environ.get("OPENAI_API_KEY"):
            self.client = OpenAI()
            default = "gpt-4o-mini"
        else:
            raise RuntimeError("Set OPENROUTER_API_KEY or OPENAI_API_KEY (or use --dry-run)")
        self.model = model or os.environ.get("FIXER_MODEL") or default
        self.name = self.model

    def patch(self, code: str, feedback: str) -> str:
        resp = self.client.chat.completions.create(
            model=self.model, temperature=0.1,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": f"Current handler.py:\n```python\n{code}```\n\n{feedback}"}])
        return extract_code(resp.choices[0].message.content or "")


MOCK_DIR = Path(__file__).parent / "mock_patches"


class MockLLM:
    """Replays scripted patches (step1 = partial fix, step2 = full fix) for offline runs and tests."""

    name = "mock"

    def __init__(self, patches: list[str] | None = None) -> None:
        self.patches = patches or [p.read_text() for p in sorted(MOCK_DIR.glob("step*.py"))]
        self.calls: list[str] = []

    def patch(self, code: str, feedback: str) -> str:
        self.calls.append(feedback)
        return self.patches[min(len(self.calls), len(self.patches)) - 1]
