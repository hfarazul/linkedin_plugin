from .base import LinkedInAdapter, ProspectHit, Post
from ..config import Config


def get_adapter(cfg: Config) -> LinkedInAdapter:
    if cfg.backend == "playwright":
        from .playwright_adapter import PlaywrightAdapter
        return PlaywrightAdapter(cfg)
    if cfg.backend == "unipile":
        from .unipile_adapter import UnipileAdapter
        return UnipileAdapter(cfg)
    if cfg.backend == "fake":
        from .fake_adapter import FakeAdapter
        return FakeAdapter(cfg)
    raise ValueError(f"unknown backend {cfg.backend!r}; expected 'playwright', 'unipile', or 'fake'")


__all__ = ["LinkedInAdapter", "ProspectHit", "Post", "get_adapter"]
