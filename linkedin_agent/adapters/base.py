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


class LinkedInAdapter(ABC):
    @abstractmethod
    def search(self, query: str, limit: int = 20) -> list[ProspectHit]:
        """Free-text people search. Returns ProspectHits, newest match first."""

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
