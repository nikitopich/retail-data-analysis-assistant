"""Arize Phoenix tracing init (spec §7.3) — now opt-in.

Enabled via the ``--trace`` flag or ``TRACING=1`` env. Must be called BEFORE the
graph is built. Failures here are non-fatal: tracing is infrastructure, not a
closed requirement, so the CLI keeps working without it.

If ``PHOENIX_COLLECTOR_ENDPOINT`` is set, traces are sent to that already-running
collector (so they persist across runs) instead of launching an embedded UI.
"""
from __future__ import annotations

import logging
from typing import Optional

PHOENIX_URL = "http://localhost:6006"


def init_tracing(
    enabled: bool,
    endpoint: Optional[str] = None,
    debug: bool = False,
) -> Optional[str]:
    """Instrument LangChain/LangGraph for Phoenix when ``enabled``.

    Returns a URL/endpoint string on success, or ``None`` when tracing is
    disabled or could not be initialized.
    """
    if not enabled:
        return None
    try:
        from openinference.instrumentation.langchain import LangChainInstrumentor
        from phoenix.otel import register

        if endpoint:
            # Send to an external collector (`phoenix serve`).
            tracer_provider = register(endpoint=endpoint)
            url = endpoint
        else:
            # Launch a local, embedded Phoenix UI.
            import phoenix as px

            px.launch_app()
            tracer_provider = register()
            url = PHOENIX_URL

        LangChainInstrumentor().instrument(tracer_provider=tracer_provider)
        # Silence all OTel noise (encode errors for surrogates, batch exporter
        # failures, ClientDisconnect) and google-auth warnings — tracing
        # failures must never surface as noise in the user-facing CLI output.
        for _name in ("opentelemetry", "google.auth"):
            logging.getLogger(_name).setLevel(logging.CRITICAL)
        return url
    except Exception as e:  # pragma: no cover - depends on local env
        logging.warning(f"Phoenix tracing disabled: {e}")
        if debug:
            import traceback

            traceback.print_exc()
        return None
