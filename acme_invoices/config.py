"""Runtime configuration (12-factor: config from env, not code)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent

DEFAULT_DATA_DIR = PROJECT_ROOT / "data" / "invoices"
DEFAULT_DB_PATH = PROJECT_ROOT / "inventory.db"

# Business thresholds (mirrors the notebook engine).
HIGH_VALUE_THRESHOLD = 10_000.0
PRICE_TOLERANCE = 0.05           # 5% variance from catalog price is allowed
SUPPORTED_EXTS = {".json", ".txt", ".csv", ".xml", ".pdf"}


@dataclass
class Settings:
    """Everything the pipeline needs to run, resolved once at startup."""

    db_path: Path = DEFAULT_DB_PATH
    data_dir: Path = DEFAULT_DATA_DIR
    offline: bool = False
    api_key: Optional[str] = None
    model: str = "grok-3"

    @property
    def use_grok(self) -> bool:
        """True only when Grok should actually be called."""
        return bool(self.api_key) and not self.offline


def load_settings(
    *,
    db_path: Optional[Path] = None,
    data_dir: Optional[Path] = None,
    offline: bool = False,
    model: Optional[str] = None,
) -> Settings:
    """Build Settings, loading XAI_API_KEY from the environment / a local .env."""
    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_ROOT / ".env")
    except ImportError:
        pass

    return Settings(
        db_path=Path(db_path) if db_path else DEFAULT_DB_PATH,
        data_dir=Path(data_dir) if data_dir else DEFAULT_DATA_DIR,
        offline=offline,
        api_key=None if offline else os.getenv("XAI_API_KEY"),
        model=model or os.getenv("XAI_MODEL", "grok-3"),
    )
