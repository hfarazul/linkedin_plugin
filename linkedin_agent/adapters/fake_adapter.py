from __future__ import annotations

# Offline adapter for tests and dry-run smoke checks. Never touches the network.
# Wire by setting LINKEDIN_BACKEND=fake in .env.

from ..config import Config
from .base import LinkedInAdapter, Post, ProspectHit


class FakeAdapter(LinkedInAdapter):
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, name: str, *args, **kwargs) -> None:
        self.calls.append((name, args, kwargs))

    def search(self, query: str, limit: int = 20) -> list[ProspectHit]:
        self._record("search", query, limit=limit)
        return [
            ProspectHit(
                linkedin_url=f"https://www.linkedin.com/in/fake-{i}-{query.replace(' ', '-').lower()}",
                full_name=f"Fake Person {i}",
                headline=f"Headline {i} matching {query}",
                company=f"Company {i}",
                title="Founder",
                location="Remote",
            )
            for i in range(1, limit + 1)
        ]

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
