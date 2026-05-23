from __future__ import annotations

# Offline adapter for tests and dry-run smoke checks. Never touches the network.
# Wire by setting LINKEDIN_BACKEND=fake in .env.

import os

from ..config import Config
from .base import LinkedInAdapter, Post, PostHit, ProspectHit


class FakeAdapter(LinkedInAdapter):
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, name: str, *args, **kwargs) -> None:
        self.calls.append((name, args, kwargs))

    def search(self, query: str, limit: int = 20) -> list[ProspectHit]:
        self._record("search", query, limit=limit)
        # Test hooks — let integration tests drive scenarios that the default
        # query-echo headline can't naturally produce (e.g. funding-import's
        # no-match path needs headlines that lack a founder/CEO keyword).
        if os.environ.get("LINKEDIN_FAKE_EMPTY_SEARCH") == "1":
            return []
        headline_override = os.environ.get("LINKEDIN_FAKE_HEADLINE")
        return [
            ProspectHit(
                linkedin_url=f"https://www.linkedin.com/in/fake-{i}-{query.replace(' ', '-').lower()}",
                full_name=f"Fake Person {i}",
                headline=headline_override or f"Headline {i} matching {query}",
                company=f"Company {i}",
                title="Founder",
                location="Remote",
            )
            for i in range(1, limit + 1)
        ]

    def search_posts(self, keywords: str, *, limit: int = 20,
                     date_posted: str = "past_month",
                     author_keywords: str | None = None) -> list[PostHit]:
        self._record("search_posts", keywords, limit=limit,
                     date_posted=date_posted, author_keywords=author_keywords)
        out: list[PostHit] = []
        for i in range(1, limit + 1):
            slug = f"fake-post-author-{i}-{keywords.replace(' ', '-')[:30]}".lower()
            author = ProspectHit(
                linkedin_url=f"https://www.linkedin.com/in/{slug}",
                full_name=f"Fake Author {i}",
                headline=f"Founder building {keywords[:40]}",
                location="San Francisco, CA",
                provider_id=f"ACo{slug.replace('-', '_')}",
            )
            out.append(PostHit(
                author=author,
                post_text=f"Hey LinkedIn — I'm a non-tech founder working on {keywords}. {i}",
                post_url=f"https://www.linkedin.com/feed/update/urn:li:activity:fake-{i}",
                posted_at="2026-05-17T12:00:00Z",
            ))
        return out

    def get_recent_posts(self, linkedin_url: str, limit: int = 5) -> list[Post]:
        self._record("get_recent_posts", linkedin_url, limit=limit)
        slug = linkedin_url.rstrip("/").rsplit("/", 1)[-1]
        return [
            Post(
                post_id=f"urn:li:activity:{slug}-{i}",
                url=f"https://www.linkedin.com/feed/update/urn:li:activity:{slug}-{i}/",
                author_url=linkedin_url,
                text=f"Sample post {i} from {slug}",
                posted_at="2026-05-14T12:00:00Z",
            )
            for i in range(1, limit + 1)
        ]

    def react(self, post: Post, reaction: str = "LIKE") -> str:
        self._record("react", post.post_urn, reaction=reaction)
        return post.post_urn

    def send_connection(self, linkedin_url: str, note: str | None = None) -> str:
        self._record("send_connection", linkedin_url, note=note)
        return "fake-invitation-id"

    def send_dm(self, linkedin_url: str, body: str) -> str:
        self._record("send_dm", linkedin_url, body=body)
        return "fake-message-id"
