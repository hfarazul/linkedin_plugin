from __future__ import annotations

# Unipile LinkedIn adapter. Endpoint paths and body shapes verified against the
# Unipile Node SDK source (github.com/unipile/unipile-node-sdk, develop branch)
# and live API probes on 2026-05-16. Web docs were stale on some paths — the
# SDK source is authoritative.
#
# Verified endpoints used by this adapter:
#   POST  /linkedin/search                         (search people)
#   GET   /users/{public_identifier_or_provider_id} (resolve profile)
#   GET   /users/{provider_id}/posts               (recent activity)
#   POST  /posts/reaction                          (react to a post)
#   POST  /users/invite                            (send connection request)
#   POST  /chats                                   (start a new chat / DM, multipart)

import re
from urllib.parse import urlparse

import httpx

from ..config import Config
from .base import LinkedInAdapter, Post, PostHit, ProspectHit


_PROVIDER_ID_RE = re.compile(r"^ACo[A-Za-z0-9_\-]+$")


def _slug_from_url(linkedin_url: str) -> str:
    """Extract the public identifier from a LinkedIn profile URL.
    Accepts: https://www.linkedin.com/in/<slug>[/[?...]]"""
    path = urlparse(linkedin_url).path
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "in":
        return parts[1]
    raise ValueError(f"unrecognized LinkedIn profile URL: {linkedin_url!r}")


class UnipileAdapter(LinkedInAdapter):
    def __init__(self, cfg: Config) -> None:
        if not cfg.unipile_api_key or not cfg.unipile_account_id or not cfg.unipile_dsn:
            raise RuntimeError(
                "Unipile backend selected but UNIPILE_API_KEY / UNIPILE_ACCOUNT_ID / UNIPILE_DSN not set"
            )
        self.cfg = cfg
        self._client = httpx.Client(
            base_url=f"https://{cfg.unipile_dsn}/api/v1",
            headers={"X-API-KEY": cfg.unipile_api_key, "accept": "application/json"},
            timeout=60.0,
        )

    def close(self) -> None:
        self._client.close()

    # ----------------------------------------------------- provider_id resolve

    def _resolve_provider_id(self, linkedin_url: str) -> str:
        """Look up the LinkedIn-internal provider id from a profile URL.
        Returns the ACoXXX-style id used by all action endpoints."""
        slug = _slug_from_url(linkedin_url)
        # Allow callers to pass a provider id directly via a /in/<provider_id>/ URL
        if _PROVIDER_ID_RE.match(slug):
            return slug
        r = self._client.get(f"/users/{slug}", params={"account_id": self.cfg.unipile_account_id})
        r.raise_for_status()
        return r.json()["provider_id"]

    # ------------------------------------------------------------------- search

    def search(self, query: str, limit: int = 20) -> list[ProspectHit]:
        r = self._client.post(
            "/linkedin/search",
            params={"account_id": self.cfg.unipile_account_id, "limit": limit},
            json={"api": "classic", "category": "people", "keywords": query},
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        out: list[ProspectHit] = []
        for it in items[:limit]:
            slug = it.get("public_identifier")
            url = f"https://www.linkedin.com/in/{slug}" if slug else it.get("public_profile_url", "")
            if not url:
                continue
            out.append(ProspectHit(
                linkedin_url=url,
                full_name=it.get("name"),
                headline=it.get("headline"),
                location=it.get("location"),
                provider_id=it.get("id"),
            ))
        return out

    def search_posts(
        self, keywords: str, *, limit: int = 20,
        date_posted: str = "past_month",
        author_keywords: str | None = None,
    ) -> list[PostHit]:
        """Search LinkedIn post content (not profile headlines). Returns posts
        with their author profiles so we can import the right person and stash
        the post text as pitch_context. Same endpoint as `search()` — just a
        different `category` and filter set."""
        body: dict = {
            "api": "classic",
            "category": "posts",
            "keywords": keywords,
            "sort_by": "date",
            "date_posted": date_posted,
        }
        if author_keywords:
            body["author"] = {"keywords": author_keywords}
        r = self._client.post(
            "/linkedin/search",
            params={"account_id": self.cfg.unipile_account_id, "limit": limit},
            json=body,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        out: list[PostHit] = []
        for it in items[:limit]:
            author = it.get("author") or {}
            # Author shape varies — try a few fields. public_identifier is
            # most reliable when present; fall back to a profile URL.
            slug = author.get("public_identifier")
            author_url = (
                f"https://www.linkedin.com/in/{slug}" if slug
                else author.get("public_profile_url") or author.get("profile_url") or ""
            )
            if not author_url:
                continue
            out.append(PostHit(
                author=ProspectHit(
                    linkedin_url=author_url,
                    full_name=author.get("name"),
                    headline=author.get("headline"),
                    location=author.get("location"),
                    provider_id=author.get("id"),
                ),
                post_text=(it.get("text") or "")[:2000],
                post_url=it.get("share_url") or it.get("url"),
                posted_at=it.get("date"),
            ))
        return out

    # ------------------------------------------------------------------ profile

    def get_recent_posts(self, linkedin_url: str, limit: int = 5) -> list[Post]:
        provider_id = self._resolve_provider_id(linkedin_url)
        r = self._client.get(
            f"/users/{provider_id}/posts",
            params={"account_id": self.cfg.unipile_account_id, "limit": limit},
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        return [
            Post(
                post_id=str(it["id"]),
                url=it.get("share_url") or f"https://www.linkedin.com/feed/update/urn:li:activity:{it['id']}",
                author_url=linkedin_url,
                text=(it.get("text") or "")[:1000],
                posted_at=it.get("date"),
            )
            for it in items[:limit]
        ]

    # ------------------------------------------------------------------ react

    def react(self, post: Post, reaction: str = "LIKE") -> str:
        # Unipile accepts reaction_type in lowercase: "like", "celebrate", "support",
        # "love", "insightful", "funny"
        r = self._client.post(
            "/posts/reaction",
            json={
                "account_id": self.cfg.unipile_account_id,
                "post_id": post.post_id,
                "reaction_type": reaction.lower(),
            },
        )
        r.raise_for_status()
        return post.post_id

    # ----------------------------------------------------------------- invite

    def send_connection(self, linkedin_url: str, note: str | None = None) -> str:
        provider_id = self._resolve_provider_id(linkedin_url)
        payload: dict[str, object] = {
            "account_id": self.cfg.unipile_account_id,
            "provider_id": provider_id,
        }
        if note:
            payload["message"] = note[:300]
        r = self._client.post("/users/invite", json=payload)
        r.raise_for_status()
        return r.json().get("invitation_id") or "sent"

    # ------------------------------------------------------------------- chat

    def send_dm(self, linkedin_url: str, body: str) -> str:
        """Send a DM by starting a new chat. If a chat with this recipient
        already exists, Unipile appends to it rather than creating a duplicate."""
        provider_id = self._resolve_provider_id(linkedin_url)
        # POST /chats expects multipart/form-data per the SDK source.
        data = {
            "account_id": self.cfg.unipile_account_id,
            "text": body,
            "attendees_ids": provider_id,
        }
        r = self._client.post("/chats", data=data)
        r.raise_for_status()
        payload = r.json()
        return payload.get("chat_id") or payload.get("message_id") or "sent"
