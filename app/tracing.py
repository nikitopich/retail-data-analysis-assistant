"""Arize Phoenix tracing init (spec §7.3).

Must be called BEFORE the graph is built. Failures here are non-fatal: tracing
is infrastructure, not a closed requirement, so the CLI keeps working without it.
"""
from __future__ import annotations

import logging
from typing import Optional

PHOENIX_URL = "http://localhost:6006"


def init_tracing(debug: bool = False) -> Optional[str]:
    """Launch a local Phoenix app and instrument LangChain/LangGraph.

    Returns the Phoenix UI URL on success, or ``None`` if tracing could not be
    initialized.
    """
    try:
        import phoenix as px
        from openinference.instrumentation.langchain import LangChainInstrumentor
        from phoenix.otel import register

        px.launch_app()                  # local UI at http://localhost:6006
        tracer_provider = register()     # OTEL provider -> Phoenix collector
        LangChainInstrumentor().instrument(tracer_provider=tracer_provider)
        return PHOENIX_URL
    except Exception as e:  # pragma: no cover - depends on local env
        logging.warning(f"Phoenix tracing disabled: {e}")
        if debug:
            import traceback

            traceback.print_exc()
        return None
