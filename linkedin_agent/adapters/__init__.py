from .base import LinkedInAdapter, ProspectHit, Post
from ..config import Config


def get_adapter(cfg: Config) -> LinkedInAdapter:
    """Return the adapter for the configured backend.

    'unipile' and 'phantombuster' both resolve to the capability router, which
    is now the only production path — provider choice is a routing question,
    not an adapter question, so the legacy backend name is honoured but does
    not select an implementation. 'fake' and 'playwright' still bypass it:
    the fake adapter is what keeps the offline test suite hermetic.
    """
    if cfg.backend == "fake":
        from .fake_adapter import FakeAdapter
        return FakeAdapter(cfg)
    if cfg.backend == "playwright":
        from .playwright_adapter import PlaywrightAdapter
        return PlaywrightAdapter(cfg)
    if cfg.backend in ("unipile", "phantombuster", "router"):
        from ..providers import RouterAdapter, build_router
        return RouterAdapter(build_router(cfg))
    raise ValueError(
        f"unknown backend {cfg.backend!r}; expected 'phantombuster', 'fake', "
        f"or 'playwright'")


__all__ = ["LinkedInAdapter", "ProspectHit", "Post", "get_adapter"]
