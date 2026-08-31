"""LLM integration (xAI Grok) with a hermetic offline fallback.

The pipeline never *requires* the network: every Grok-backed agent works with a
deterministic fallback when no client is supplied. Tests inject a fake client
that satisfies :class:`LLMClient`.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Quiet the native gRPC/absl C-library logging that xai_sdk pulls in; without
# this, proxy/TLS handshake failures spew raw lines straight to stderr.
os.environ.setdefault("GRPC_VERBOSITY", "NONE")
os.environ.setdefault("GLOG_minloglevel", "3")


@runtime_checkable
class LLMClient(Protocol):
    """Minimal surface the pipeline needs from a chat model."""

    def complete(self, prompt: str) -> str:
        ...


class GrokClient:
    """Thin wrapper over xAI's ``xai_sdk`` chat API."""

    def __init__(self, api_key: str, model: str = "grok-3") -> None:
        self.api_key = api_key
        self.model = model

    def complete(self, prompt: str) -> str:
        from xai_sdk import Client
        from xai_sdk.chat import user

        client = Client(api_key=self.api_key)
        chat = client.chat.create(model=self.model)
        chat.append(user(prompt))
        return chat.sample().content


def get_llm(
    *, offline: bool = False, api_key: Optional[str] = None, model: str = "grok-3"
) -> Optional[LLMClient]:
    """Return a live Grok client, or ``None`` to force deterministic fallbacks."""
    if offline or not api_key:
        return None
    return GrokClient(api_key, model)


def summarize_error(exc: BaseException, limit: int = 140) -> str:
    """Collapse an exception (e.g. a multi-line gRPC error) to one short line."""
    first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    text = f"{type(exc).__name__}: {first_line}" if first_line else type(exc).__name__
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_json_response(content: str) -> dict:
    """Parse a model reply as JSON, tolerating ```json fences."""
    clean = content.strip()
    if clean.startswith("```"):
        clean = clean.split("```", 2)[1]
        if clean.lstrip().lower().startswith("json"):
            clean = clean.lstrip()[4:]
    clean = clean.strip("` \n")
    return json.loads(clean)
