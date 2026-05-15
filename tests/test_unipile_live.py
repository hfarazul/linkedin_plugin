"""Live integration tests against the real Unipile API.

Skipped by default. Run with:
    pytest -m live                    # safe reads + self-react only
    pytest -m live --invite-target=<linkedin_url>   # also test connection request
    pytest -m live --dm-target=<linkedin_url>       # also test DM send

Requires .env with valid UNIPILE_API_KEY / UNIPILE_ACCOUNT_ID / UNIPILE_DSN.
The invite/dm tests target real people on LinkedIn — provide URLs only for
profiles you control (e.g., a secondary account or a colleague who has agreed).
"""

from __future__ import annotations

import os

import pytest

from linkedin_agent.adapters import get_adapter
from linkedin_agent.adapters.unipile_adapter import UnipileAdapter
from linkedin_agent.config import load as load_config


# --------------------------------------------------------------------- pytest

def pytest_addoption(parser):  # noqa: PT004
    """Forwarded to conftest if present — keeps these tests self-contained.
    Tests read the values from os.environ as a fallback."""


# Marker registered in pyproject.toml's [tool.pytest.ini_options]
pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def cfg():
    c = load_config()
    if c.backend != "unipile":
        pytest.skip(f"LINKEDIN_BACKEND={c.backend!r}; this test requires 'unipile'")
    if not (c.unipile_api_key and c.unipile_account_id and c.unipile_dsn):
        pytest.skip("Unipile credentials not set in .env")
    return c


@pytest.fixture(scope="module")
def adapter(cfg):
    a = get_adapter(cfg)
    yield a
    a.close()


# --------------------------------------------------------------------- reads

def test_search_returns_real_profiles(adapter: UnipileAdapter):
    hits = adapter.search("fintech founder", limit=3)
    assert len(hits) >= 1, "search returned nothing"
    h = hits[0]
    assert h.linkedin_url.startswith("https://www.linkedin.com/in/"), f"unexpected url: {h.linkedin_url}"
    assert h.provider_id and h.provider_id.startswith("ACo"), f"missing provider_id: {h.provider_id}"
    assert h.full_name, "missing full_name"


def test_get_recent_posts_on_self(adapter: UnipileAdapter):
    # We assume the account owner has at least one post. If not, skip rather
    # than fail — that's an account state issue, not an adapter issue.
    posts = adapter.get_recent_posts("https://www.linkedin.com/in/haquefarazul", limit=3)
    if not posts:
        pytest.skip("account has no recent posts to test against")
    p = posts[0]
    assert p.post_id and p.post_id.isdigit(), f"post_id should be numeric: {p.post_id}"
    assert p.url.startswith("https://www.linkedin.com/"), f"odd post url: {p.url}"


# --------------------------------------------------------------------- write

def test_react_to_own_post(adapter: UnipileAdapter):
    """Reacting to your own LinkedIn post is a safe write — no one else is affected.
    Verifies the /posts/reaction endpoint round-trips."""
    posts = adapter.get_recent_posts("https://www.linkedin.com/in/haquefarazul", limit=1)
    if not posts:
        pytest.skip("no posts to react to")
    result = adapter.react(posts[0], reaction="LIKE")
    assert result == posts[0].post_id


# Invite + DM are dangerous against random people — they only run when the
# user passes a target URL via env vars. Document this loudly so nobody
# accidentally sends a connection request during dev.

INVITE_TARGET = os.getenv("LINKEDIN_INVITE_TARGET")
DM_TARGET = os.getenv("LINKEDIN_DM_TARGET")


@pytest.mark.skipif(not INVITE_TARGET, reason="set LINKEDIN_INVITE_TARGET=<url> to enable")
def test_send_connection_request(adapter: UnipileAdapter):
    """Sends an actual connection request to LINKEDIN_INVITE_TARGET.
    Only set this env var for a URL you control."""
    result = adapter.send_connection(
        INVITE_TARGET,
        note="hey — testing my outreach pipeline. ignore.",
    )
    assert result and result != "sent" or result == "sent"  # accept either response shape


@pytest.mark.skipif(not DM_TARGET, reason="set LINKEDIN_DM_TARGET=<url> to enable")
def test_send_dm(adapter: UnipileAdapter):
    """Sends a real DM to LINKEDIN_DM_TARGET. Only set for a URL you control."""
    result = adapter.send_dm(
        DM_TARGET,
        body="hey — testing the outreach DM path. ignore this.",
    )
    assert result, f"expected non-empty result, got {result!r}"
