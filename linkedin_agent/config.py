from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _int(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(v) if v else default


def _bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    backend: str
    unipile_api_key: str | None
    unipile_account_id: str | None
    unipile_dsn: str | None

    telegram_bot_token: str | None
    telegram_chat_id: int | None

    daily_max_reactions: int
    daily_max_connections: int
    daily_max_dms: int
    daily_max_searches: int

    action_delay_min: int
    action_delay_max: int

    dry_run: bool

    playwright_state_path: Path


def load() -> Config:
    chat_id_raw = os.getenv("TELEGRAM_CHAT_ID")
    return Config(
        backend=os.getenv("LINKEDIN_BACKEND", "playwright").lower(),
        unipile_api_key=os.getenv("UNIPILE_API_KEY") or None,
        unipile_account_id=os.getenv("UNIPILE_ACCOUNT_ID") or None,
        unipile_dsn=os.getenv("UNIPILE_DSN") or None,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=int(chat_id_raw) if chat_id_raw and chat_id_raw.strip() else None,
        daily_max_reactions=_int("DAILY_MAX_REACTIONS", 30),
        daily_max_connections=_int("DAILY_MAX_CONNECTIONS", 20),
        daily_max_dms=_int("DAILY_MAX_DMS", 10),
        daily_max_searches=_int("DAILY_MAX_SEARCHES", 50),
        action_delay_min=_int("ACTION_DELAY_MIN", 30),
        action_delay_max=_int("ACTION_DELAY_MAX", 90),
        dry_run=_bool("DRY_RUN", False),
        playwright_state_path=ROOT / "playwright_state" / "state.json",
    )
