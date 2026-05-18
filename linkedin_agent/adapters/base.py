from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ProspectHit:
    linkedin_url: str
    full_name: str | None = None
    headline: str | None = None
    company: str | None = None
    title: str | None = None
    location: str | None = None
    # LinkedIn-internal provider id (e.g. 'ACoAAA...'). Populated by adapters
    # that know it from search; used internally to skip a profile-resolve hop.
    provider_id: str | None = None


@dataclass
class Post:
    # post_id is the numeric activity id (string-form). For reaction/comment
    # calls Unipile expects this id directly.
    post_id: str
    url: str
    author_url: str
    text: str
    posted_at: str | None = None
    # Legacy alias used by older callers — same value as post_id.

    @property
    def post_urn(self) -> str:
        return self.post_id


@dataclass
class PostHit:
    """Result from a post-content search (vs profile search). Pairs the author
    profile with the post that matched the query. Use the post text as
    pitch_context when importing — the drafter can reference what the
    prospect actually wrote."""
    author: ProspectHit
    post_text: str
    post_url: str | None = None
    posted_at: str | None = None


class LinkedInAdapter(ABC):
    @abstractmethod
    def search(self, query: str, limit: int = 20) -> list[ProspectHit]:
        """Free-text people search. Returns ProspectHits, newest match first."""

    def search_posts(
        self, keywords: str, *, limit: int = 20,
        date_posted: str = "past_month",
        author_keywords: str | None = None,
    ) -> list[PostHit]:
        """Keyword search across LinkedIn POST content (not profile headlines).
        Returns posts with their authors so we can import the right person and
        stash the post text as pitch_context.

        `date_posted`: one of "past_24h", "past_week", "past_month".
        `author_keywords`: optional headline filter to bias toward role
        (e.g. "founder" excludes service providers).

        Default impl raises NotImplementedError — adapters that support it
        (e.g. UnipileAdapter) override. Playwright backend can stay on
        profile-search until/unless we wire post-search on that side too."""
        raise NotImplementedError("post-search not supported by this adapter")

    @abstractmethod
    def get_recent_posts(self, linkedin_url: str, limit: int = 5) -> list[Post]:
        """Recent activity for a profile."""

    @abstractmethod
    def react(self, post: Post, reaction: str = "LIKE") -> str:
        """React to a post. Returns a result identifier (URN, etc.)."""

    @abstractmethod
    def send_connection(self, linkedin_url: str, note: str | None = None) -> str:
        """Send a connection request, optionally with a personalized note (≤300 chars)."""

    @abstractmethod
    def send_dm(self, linkedin_url: str, body: str) -> str:
        """Send a direct message. Assumes already connected (or InMail credit available)."""

    def close(self) -> None:
        """Optional cleanup."""
