"""PhantomBuster job execution: launch -> poll -> fetch.

PhantomBuster is asynchronous in a way Unipile is not. You queue an agent, it
boots a container, scrapes for seconds to minutes, then writes result.json to
S3. There is no synchronous "give me this profile" call.

This module hides that behind `run()`, which blocks with a bounded timeout.
That is honest for CLI use (a profile scrape is ~10-30s) and deliberately NOT
suitable for iterating hundreds of prospects inside the hourly cron -- PB-11
adds a job table for that. `submit()` and `collect()` are exposed separately so
the batch path can be built on the same primitives rather than a second client.

Endpoints (verified live against api.phantombuster.com/api/v2):
    POST /agents/launch            queue a run, returns containerId
    GET  /containers/fetch         container status
    GET  /agents/fetch             orgS3Folder + s3Folder for result files
    GET  /agents/fetch-output      console log, and the result URLs it prints
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

import httpx

from .base import (
    MalformedResponse,
    ProviderAuthError,
    ProviderRateLimited,
    ProviderTimeout,
)

logger = logging.getLogger("linkedin.providers.phantombuster")

API_BASE = "https://api.phantombuster.com/api/v2"
S3_BASE = "https://phantombuster.s3.amazonaws.com"

# A container that has not finished within this window is treated as timed out.
# Generous because a Phantom boots a browser; still bounded so a wedged run
# cannot hang a CLI session indefinitely.
DEFAULT_TIMEOUT_SEC = 300
POLL_INTERVAL_SEC = 5

# Container states that mean "no longer running".
_TERMINAL = {"finished", "error", "aborted", "unknown"}


@dataclass
class JobResult:
    container_id: str
    status: str
    rows: list
    console: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "finished"


class PhantomBusterJobs:
    """Thin client for the launch/poll/fetch cycle."""

    def __init__(self, api_key: str, *, timeout: int = DEFAULT_TIMEOUT_SEC) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self._client = httpx.Client(
            base_url=API_BASE,
            headers={"X-Phantombuster-Key-1": api_key,
                     "accept": "application/json"},
            timeout=60.0,
        )

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------- plumbing

    def _request(self, method: str, path: str, **kwargs):
        try:
            r = self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as e:
            raise ProviderTimeout(str(e), provider="phantombuster") from e
        except httpx.HTTPError as e:
            raise ProviderTimeout(f"{type(e).__name__}: {e}",
                                  provider="phantombuster") from e
        if r.status_code in (401, 403):
            raise ProviderAuthError(
                f"HTTP {r.status_code} -- check PHANTOMBUSTER_API_KEY",
                provider="phantombuster")
        if r.status_code == 429:
            raise ProviderRateLimited("HTTP 429", provider="phantombuster")
        if r.status_code >= 400:
            raise MalformedResponse(f"HTTP {r.status_code}: {r.text[:200]}",
                                    provider="phantombuster")
        try:
            return r.json()
        except Exception as e:
            raise MalformedResponse("response was not JSON",
                                    provider="phantombuster") from e

    # ------------------------------------------------------------- lifecycle

    def configure(self, agent_id: str, arguments: dict) -> dict:
        """Merge `arguments` into the agent's SAVED argument and persist it.

        This is required, not an optimisation. A launch-time `argument` is
        accepted by the API and then ignored: verified on 2026-09-03 by asking
        a Profile Scraper for one profile and receiving the one it was
        configured with, both as a JSON string and as an object. Only the saved
        argument decides what a Phantom actually does.

        Merging rather than replacing keeps the session cookie, user agent and
        every other UI-configured setting intact — they are not in our payload
        and overwriting them would break the agent.

        CAUTION: this mutates shared state. Two runs retargeting the same agent
        concurrently will clobber each other, and a human editing that Phantom
        in the UI will see it change under them. Use an agent dedicated to
        programmatic use.
        """
        detail = self._request("GET", "/agents/fetch", params={"id": str(agent_id)})
        saved = detail.get("argument")
        try:
            merged = json.loads(saved) if isinstance(saved, str) else dict(saved or {})
        except Exception:
            merged = {}
        merged.update(arguments)
        self._request("POST", "/agents/save",
                      json={"id": str(agent_id), "argument": json.dumps(merged)})
        return merged

    def submit(self, agent_id: str, arguments: dict | None = None) -> str:
        """Queue an agent. Returns the container id.

        Any arguments are written to the agent's saved configuration first,
        because the launch payload's own `argument` field does not take effect.
        """
        if arguments:
            self.configure(agent_id, arguments)
        payload: dict = {"id": str(agent_id)}
        data = self._request("POST", "/agents/launch", json=payload)
        container_id = data.get("containerId") or data.get("data", {}).get("containerId")
        if not container_id:
            raise MalformedResponse(f"launch returned no containerId: {data}",
                                    provider="phantombuster")
        logger.info("phantombuster: launched agent %s -> container %s",
                    agent_id, container_id)
        return str(container_id)

    def poll(self, container_id: str) -> str:
        """Current container status. Terminal values are in _TERMINAL."""
        data = self._request("GET", "/containers/fetch", params={"id": container_id})
        return (data or {}).get("status") or "unknown"

    def wait(self, container_id: str, *, timeout: int | None = None) -> str:
        deadline = time.monotonic() + (timeout or self.timeout)
        status = "unknown"
        while time.monotonic() < deadline:
            status = self.poll(container_id)
            if status in _TERMINAL:
                return status
            time.sleep(POLL_INTERVAL_SEC)
        raise ProviderTimeout(
            f"container {container_id} still {status!r} after "
            f"{timeout or self.timeout}s", provider="phantombuster")

    # --------------------------------------------------------------- results

    def result_urls(self, agent_id: str) -> tuple[str | None, str | None]:
        """(json_url, csv_url) for an agent's most recent result files."""
        data = self._request("GET", "/agents/fetch", params={"id": str(agent_id)})
        org, folder = data.get("orgS3Folder"), data.get("s3Folder")
        if not (org and folder):
            return None, None
        base = f"{S3_BASE}/{org}/{folder}"
        return f"{base}/result.json", f"{base}/result.csv"

    def fetch_rows(self, agent_id: str) -> list:
        """Read the agent's result.json.

        JSON only, never the CSV: the CSV export is mis-encoded (UTF-8 read as
        latin-1, e.g. "Next.js â€¢ React"), and ingesting that would corrupt
        headlines in our database.

        An empty list is a legitimate result, not an error -- the Inbox
        Scraper is incremental and returns [] when nothing is new.
        """
        json_url, _ = self.result_urls(agent_id)
        if not json_url:
            raise MalformedResponse(f"agent {agent_id} has no S3 result folder",
                                    provider="phantombuster")
        try:
            r = httpx.get(json_url, timeout=60.0, follow_redirects=True)
        except httpx.HTTPError as e:
            raise ProviderTimeout(f"fetching results: {e}",
                                  provider="phantombuster") from e
        if r.status_code == 403:
            raise ProviderAuthError("S3 result file not readable",
                                    provider="phantombuster")
        if r.status_code != 200:
            raise MalformedResponse(f"result.json HTTP {r.status_code}",
                                    provider="phantombuster")
        try:
            data = r.json()
        except Exception as e:
            raise MalformedResponse("result.json was not JSON",
                                    provider="phantombuster") from e
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
        raise MalformedResponse(f"unexpected result type {type(data).__name__}",
                                provider="phantombuster")

    def console(self, agent_id: str) -> str:
        try:
            data = self._request("GET", "/agents/fetch-output",
                                 params={"id": str(agent_id)})
        except Exception:
            return ""
        return str((data or {}).get("output", ""))

    # ------------------------------------------------------------------ run

    def run(self, agent_id: str, arguments: dict | None = None,
            *, timeout: int | None = None) -> JobResult:
        """Launch, wait, and fetch. Blocking and bounded."""
        container_id = self.submit(agent_id, arguments)
        status = self.wait(container_id, timeout=timeout)
        console = self.console(agent_id)
        if status != "finished":
            raise MalformedResponse(
                f"container {container_id} ended as {status!r}; "
                f"console tail: {console[-300:]}", provider="phantombuster")
        return JobResult(container_id=container_id, status=status,
                         rows=self.fetch_rows(agent_id), console=console)
