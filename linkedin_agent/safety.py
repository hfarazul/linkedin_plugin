from __future__ import annotations

import random
import time

from . import db
from .config import Config


class RateLimitExceeded(RuntimeError):
    """Raised when a daily cap would be exceeded."""


CAP_FIELD = {
    "react": "daily_max_reactions",
    "connect": "daily_max_connections",
    "dm": "daily_max_dms",
    "search": "daily_max_searches",
}


def check_cap(cfg: Config, kind: str) -> None:
    field = CAP_FIELD.get(kind)
    if not field:
        return
    cap = getattr(cfg, field)
    used = db.count_actions_last_24h(kind)
    if used >= cap:
        raise RateLimitExceeded(
            f"daily cap reached for {kind}: {used}/{cap} in last 24h"
        )


def human_delay(cfg: Config) -> None:
    """Sleep a randomized delay between actions. Skipped in dry-run."""
    if cfg.dry_run:
        return
    lo, hi = cfg.action_delay_min, cfg.action_delay_max
    if hi <= lo:
        time.sleep(lo)
        return
    time.sleep(random.uniform(lo, hi))
