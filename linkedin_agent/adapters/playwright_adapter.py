from __future__ import annotations

# WARNING: Browser automation of LinkedIn violates their Terms of Service and
# carries real account-restriction risk. Use only on a throwaway/secondary account
# during development. Move to the Unipile adapter for sustained outreach.

import re
import urllib.parse
from typing import Iterable

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from ..config import Config
from .base import LinkedInAdapter, Post, ProspectHit

LINKEDIN_HOME = "https://www.linkedin.com"
PROFILE_RE = re.compile(r"https?://(?:www\.)?linkedin\.com/in/[^/?#]+")


class PlaywrightAdapter(LinkedInAdapter):
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    # ---------------------------------------------------------------- lifecycle

    def _ensure(self) -> Page:
        if self._page:
            return self._page
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        state_path = self.cfg.playwright_state_path
        storage_state = str(state_path) if state_path.exists() else None
        self._context = self._browser.new_context(
            storage_state=storage_state,
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        self._page = self._context.new_page()
        return self._page

    def close(self) -> None:
        if self._context:
            self._context.close()
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    def login_interactive(self) -> None:
        """Open a non-headless browser so the user can log in manually,
        then save the session state for future headless runs."""
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto(f"{LINKEDIN_HOME}/login")
            print("→ Log in to LinkedIn in the opened browser window.")
            print("→ Once you reach the home feed, return here and press Enter.")
            input()
            self.cfg.playwright_state_path.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(self.cfg.playwright_state_path))
            print(f"✓ Saved session to {self.cfg.playwright_state_path}")
            context.close()
            browser.close()

    # ------------------------------------------------------------------- search

    def search(self, query: str, limit: int = 20) -> list[ProspectHit]:
        # Selectors below were stable as of 2026-Q2 but LinkedIn ships breaking
        # DOM changes frequently. If search returns 0 hits, re-inspect the page
        # in DevTools and update the selectors here.
        page = self._ensure()
        url = (
            f"{LINKEDIN_HOME}/search/results/people/"
            f"?keywords={urllib.parse.quote(query)}&origin=GLOBAL_SEARCH_HEADER"
        )
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_selector("div.search-results-container", timeout=15000)

        hits: list[ProspectHit] = []
        seen: set[str] = set()
        for card in page.locator("div.search-results-container li").element_handles()[:limit * 2]:
            link_handle = card.query_selector("a[href*='/in/']")
            if not link_handle:
                continue
            href = link_handle.get_attribute("href") or ""
            m = PROFILE_RE.search(href)
            if not m:
                continue
            profile_url = m.group(0)
            if profile_url in seen:
                continue
            seen.add(profile_url)
            name_handle = card.query_selector("span[aria-hidden='true']")
            name = (name_handle.inner_text().strip() if name_handle else None) or None
            headline_handle = card.query_selector("div.entity-result__primary-subtitle")
            headline = (headline_handle.inner_text().strip() if headline_handle else None) or None
            location_handle = card.query_selector("div.entity-result__secondary-subtitle")
            location = (location_handle.inner_text().strip() if location_handle else None) or None
            hits.append(ProspectHit(
                linkedin_url=profile_url,
                full_name=name,
                headline=headline,
                location=location,
            ))
            if len(hits) >= limit:
                break
        return hits

    # ------------------------------------------------------------------- posts

    def get_recent_posts(self, linkedin_url: str, limit: int = 5) -> list[Post]:
        page = self._ensure()
        activity_url = linkedin_url.rstrip("/") + "/recent-activity/all/"
        page.goto(activity_url, wait_until="domcontentloaded")
        page.wait_for_selector("div.feed-shared-update-v2, div[data-urn]", timeout=15000)

        posts: list[Post] = []
        for el in page.locator("div[data-urn^='urn:li:activity']").element_handles()[:limit]:
            urn = el.get_attribute("data-urn") or ""
            if not urn:
                continue
            text_handle = el.query_selector("div.update-components-text")
            text = text_handle.inner_text().strip() if text_handle else ""
            posts.append(Post(
                post_id=urn,
                url=f"{LINKEDIN_HOME}/feed/update/{urn}/",
                author_url=linkedin_url,
                text=text[:500],
            ))
        return posts

    # ------------------------------------------------------------------ actions

    def react(self, post: Post, reaction: str = "LIKE") -> str:
        page = self._ensure()
        page.goto(post.url, wait_until="domcontentloaded")
        like_btn = page.locator("button[aria-label*='React Like']").first
        like_btn.wait_for(timeout=10000)
        like_btn.click()
        return post.post_urn

    def send_connection(self, linkedin_url: str, note: str | None = None) -> str:
        page = self._ensure()
        page.goto(linkedin_url, wait_until="domcontentloaded")
        # "Connect" is sometimes hidden behind the "More" dropdown.
        connect_visible = page.locator("button:has-text('Connect')").first
        if connect_visible.count() == 0 or not connect_visible.is_visible():
            page.locator("button:has-text('More')").first.click()
            page.locator("div[role='button']:has-text('Connect')").first.click()
        else:
            connect_visible.click()
        if note:
            page.locator("button:has-text('Add a note')").click()
            page.locator("textarea[name='message']").fill(note[:300])
        page.locator("button:has-text('Send')").click()
        return "sent"

    def send_dm(self, linkedin_url: str, body: str) -> str:
        page = self._ensure()
        page.goto(linkedin_url, wait_until="domcontentloaded")
        page.locator("button:has-text('Message')").first.click()
        page.locator("div[contenteditable='true'][aria-label*='message']").first.fill(body)
        page.locator("button:has-text('Send')").first.click()
        return "sent"
