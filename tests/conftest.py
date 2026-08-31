"""Shared fixtures: a seeded temp DB, offline settings, and a fake LLM."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Union

import pytest

from acme_invoices.config import DEFAULT_DATA_DIR, Settings
from acme_invoices.persistence import setup_inventory

DATA_DIR = DEFAULT_DATA_DIR


@pytest.fixture
def db_path(tmp_path) -> Path:
    """A freshly seeded inventory database in a temp dir."""
    p = tmp_path / "inventory.db"
    setup_inventory(p, reset=True)
    return p


@pytest.fixture
def settings(db_path) -> Settings:
    return Settings(db_path=db_path, data_dir=DATA_DIR, offline=True, api_key=None)


class FakeLLM:
    """Deterministic stand-in for a Grok client. ``reply`` is a fixed string or a
    callable(prompt) -> str. Records prompts for assertions."""

    def __init__(self, reply: Union[str, Callable[[str], str]]) -> None:
        self._reply = reply
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._reply(prompt) if callable(self._reply) else self._reply


def invoice(name: str) -> Path:
    return DATA_DIR / name
