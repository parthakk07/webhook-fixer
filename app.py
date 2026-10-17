from __future__ import annotations

import os
"""Streamlit dashboard: watch the agent turn a red webhook grid green. Run: streamlit run app.py"""

import html
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from fixer.delivery import LocalSignedDelivery, StripeDelivery
from fixer.events import DEFAULT_EVENTS
from fixer.llm import MockLLM, OpenAICompatLLM
from fixer.loop import Iteration, fix_session
from fixer.sandbox import CreateOSSandbox, LocalSandbox

load_dotenv()
st.set_page_config(page_title="Stripe Webhook Fixer", page_icon="🪝", layout="wide")
st.title("🪝 Stripe Webhook Fixer")
st.caption("An agent that runs your broken Stripe webhook handler in a CreateOS sandbox, fires signed Stripe "
           "events at its public URL, reads the tracebacks and patches the code until every event is 200.")

EXAMPLES = {p.name: p for p in sorted(Path("examples").glob("*.py"))}

with st.sidebar:
    st.header("Setup")
    choice = st.selectbox("Handler", list(EXAMPLES))
    code = st.text_area("handler.py", EXAMPLES[choice].read_text() if choice else "", height=320)
    events = st.multiselect("Stripe events", DEFAULT_EVENTS, default=DEFAULT_EVENTS)
    delivery_mode = st.radio("Delivery", ["Signed replay (robust)", "Real Stripe endpoint + stripe trigger"])
    public_demo = os.environ.get("FIXER_PUBLIC_DEMO") == "1"  # hosted copy: no secrets, offline only
    dry_run = st.toggle("Offline dry run (local sandbox + mock LLM)", value=public_demo, disabled=public_demo)
    if public_demo:
        st.caption("Hosted demo runs offline. Clone the repo and add keys to run on a real CreateOS sandbox.")
    max_iters = st.slider("Max iterations", 1, 5, 5)
    go = st.button(" Run fixer", type="primary", use_container_width=True)


def grid(it: Iteration) -> str:
    cells = []
    for r in it.results:
        color = "#16a34a" if r.ok else "#dc2626"
        label = "🔒 forged sig" if r.event == "forged-signature" else r.event
        cells.append(f"<div style='flex:1;background:{color};color:white;border-radius:8px;padding:10px;"
                     f"text-align:center;font-family:monospace'><b>{html.escape(label)}</b><br>"
                     f"{r.status if r.status is not None else '—'} <small>(want {r.expected})</small></div>")
    return (f"<div style='display:flex;gap:8px;align-items:stretch;margin:6px 0'>"
            f"<div style='width:90px;padding-top:14px'><b>Iter {it.number}</b></div>{''.join(cells)}</div>")


if go:
    if not events:
        st.error("Pick at least one event.")
        st.stop()
    try:
        sandbox = LocalSandbox() if dry_run else CreateOSSandbox()
        llm = MockLLM() if dry_run else OpenAICompatLLM()
        delivery = (StripeDelivery(events) if delivery_mode.startswith("Real") and not dry_run
                    else LocalSignedDelivery())
    except Exception as exc:  # noqa: BLE001
        st.error(f"Setup failed: {exc}")
        st.stop()

    status = st.status("Starting sandbox…", expanded=True)
    st.subheader("Event grid")
    grid_box = st.container()
    col_logs, col_code = st.columns(2)
    result = None
    try:
        for prog in fix_session(code, events, sandbox, delivery, llm, max_iters):
            status.update(label=prog.message)
            status.write(prog.message)
            if prog.iteration:
                it = prog.iteration
                grid_box.markdown(grid(it), unsafe_allow_html=True)
                with col_logs.expander(f"Iteration {it.number}: sandbox logs", expanded=not it.all_ok):
                    st.code(it.logs or "(empty)", language="text")
                    if it.rejected_patch:
                        st.warning(f"Previous LLM patch refused: {it.rejected_patch}")
                with col_code.expander(f"Iteration {it.number}: handler.py"):
                    st.code(it.code, language="python")
            result = prog.result or result
    except Exception as exc:  # noqa: BLE001
        status.update(label=f"Failed: {exc}", state="error")
        st.exception(exc)

    if result:
        status.update(label="All events green " if result.success else "Stopped before all green",
                      state="complete" if result.success else "error")
        if result.success:
            st.balloons()
        st.subheader("Diff")
        st.code(result.diff or "(no changes)", language="diff")
        st.download_button("Download fixed_handler.py", result.final, "fixed_handler.py")
        st.caption(f"Sandbox URL used: {result.url} (sandbox destroyed after the run)")
else:
    st.info("Pick a handler and press **Run fixer**. Toggle *Offline dry run* to demo without any keys.")
