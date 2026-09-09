"""Cron-safe execution of provider work that may take minutes.

Unipile answers in milliseconds. PhantomBuster boots a browser, scrapes, and
writes to S3 — seconds to minutes per call. `daily.py` iterates prospects in a
loop, so blocking on the latter would make an hourly cron overrun its own
schedule and drift until runs overlap.

This module gives both providers one API:

    submit(...)   ->  sync provider: runs now, job recorded as finished
                      async provider: launches, job recorded as running
    collect(...)  ->  polls running jobs, applies finished results

Synchronous providers still get a job row. Keeping one code path means
daily.py does not branch on provider, and the audit trail looks the same
whichever one served the request.

Results are applied by handlers registered per capability, so adding a new
async capability is a handler plus an entry in HANDLERS — the submit/collect
machinery does not change.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from . import db
from .config import Config
from .providers import Capability, MalformedResponse, ProviderError, build_router
from .providers.phantombuster import _match_requested

logger = logging.getLogger("linkedin.jobs")

# A container polled this many times without finishing is abandoned. At one
# poll per cron tick (hourly) that is roughly a day — long enough for a slow
# scrape, short enough that a wedged run does not get polled forever.
MAX_POLL_ATTEMPTS = 24


@dataclass
class SubmitResult:
    job_id: int | None
    status: str                 # running | finished | failed | skipped
    detail: str = ""


@dataclass
class CollectResult:
    checked: int = 0
    finished: int = 0
    still_running: int = 0
    failed: int = 0
    timed_out: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"checked {self.checked} · finished {self.finished} · "
                f"running {self.still_running} · failed {self.failed} · "
                f"timed out {self.timed_out}")


# --------------------------------------------------------------- handlers


def _apply_profile(job, rows: list) -> str:
    """Persist a finished profile scrape onto the prospect.

    Reuses enrichment's field mapping so a job-applied profile and an inline
    one write identical rows — including the rule that only fields the provider
    actually returned are written, so a provider gap cannot erase data the
    other provider previously stored.

    The result file is agent-scoped, not job-scoped: two profile jobs in flight
    on the same Phantom read the same `result.json`, and a container that
    finished can still be serving the previous run's rows. Taking `rows[0]` on
    trust therefore writes one prospect's headline and positions onto another —
    and because `enriched_at` is stamped in the same statement, the wrong data
    then sits there unrefreshed for the whole staleness window.

    So the row is matched against the identifier the job was launched for, the
    same check `fetch_profile` makes inline. A miss is a failure, not a
    fallback: applying an unmatched row is the bug.
    """
    from .enrichment import _facts_to_db_fields

    prospect_id = job["prospect_id"]
    if not prospect_id:
        return "no prospect_id on job"
    if not rows:
        return "no rows returned"

    target = job["target"]
    if not target:
        raise MalformedResponse(
            f"job {job['id']} has no target to match the result against",
            provider=job["provider"])
    facts = _match_requested(rows, target)
    if facts is None:
        returned = ", ".join(
            str(r.get("linkedinProfileSlug") or r.get("profileUrl") or "?")
            for r in rows[:3])
        raise MalformedResponse(
            f"job {job['id']} asked for {target!r} but the result held "
            f"{returned!r} — not applying it to prospect {prospect_id}",
            provider=job["provider"])
    fields = _facts_to_db_fields(facts)
    fields["enriched_at"] = db.now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    with db.connect() as conn:
        conn.execute(f"UPDATE prospects SET {sets} WHERE id = ?",
                     list(fields.values()) + [prospect_id])

    stored = db.replace_positions(prospect_id, facts.positions) if facts.positions else 0
    db.log_action(prospect_id, "enrich",
                  json.dumps({"provider": job["provider"], "via": "job",
                              "positions": stored}), "ok", False)
    return f"applied {len(fields)} fields, {stored} positions"


# capability -> (agent argument builder, result handler)
HANDLERS = {
    Capability.PROFILE.value: _apply_profile,
}


# ----------------------------------------------------------------- submit


def submit_profile(cfg: Config, prospect_id: int, *, router=None) -> SubmitResult:
    """Queue (or immediately run) a profile fetch for one prospect."""
    db.init_db()
    prospect = db.get_prospect(prospect_id)
    if not prospect:
        return SubmitResult(None, "skipped", f"no prospect {prospect_id}")
    identifier = prospect["provider_id"] or prospect["linkedin_url"]
    if not identifier:
        return SubmitResult(None, "skipped", "no provider_id or url")

    # Do not queue work already in flight. The async equivalent of daily.py's
    # _has_pending_draft guard: without it, every cron tick would launch
    # another container for the same prospect while the first is still running.
    if db.has_open_job(Capability.PROFILE.value, prospect_id):
        return SubmitResult(None, "skipped", "job already running")

    own_router = router is None
    if own_router:
        router = build_router(cfg)
    try:
        chain = router.providers_for(Capability.PROFILE)
        if not chain:
            return SubmitResult(None, "skipped", "no provider supports profile")
        provider = chain[0]

        if not provider.is_async:
            # Synchronous provider: run it now, but still record a job so the
            # audit trail does not depend on which provider answered.
            job_id = db.create_job(Capability.PROFILE.value, provider.name,
                                   prospect_id=prospect_id, target=identifier)
            try:
                from .enrichment import enrich
                ok = enrich(cfg, prospect_id, router=router)
            except Exception as e:
                db.finish_job(job_id, status="failed", error=str(e)[:300])
                return SubmitResult(job_id, "failed", str(e)[:200])
            db.finish_job(job_id, status="finished" if ok else "failed",
                          result_summary="inline" if ok else None,
                          error=None if ok else "profile fetch returned nothing")
            return SubmitResult(job_id, "finished" if ok else "failed", "ran inline")

        # Async provider: launch and return immediately.
        agent = provider._require_agent(Capability.PROFILE)
        url = identifier if str(identifier).startswith("http") else \
            f"https://www.linkedin.com/in/{identifier}"
        arguments = {"spreadsheetUrl": url}
        container_id = provider.jobs().submit(agent, arguments)
        job_id = db.create_job(
            Capability.PROFILE.value, provider.name, prospect_id=prospect_id,
            target=identifier, container_id=container_id,
            arguments=json.dumps(arguments))
        logger.info("queued profile job %d for prospect %d (container %s)",
                    job_id, prospect_id, container_id)
        return SubmitResult(job_id, "running", f"container {container_id}")
    except ProviderError as e:
        return SubmitResult(None, "failed", f"{type(e).__name__}: {e}")
    finally:
        if own_router:
            router.close()


# ---------------------------------------------------------------- collect


def collect(cfg: Config, *, router=None, limit: int = 50) -> CollectResult:
    """Poll every running job; apply the results of those that finished.

    Safe to call on every cron tick. Jobs that are still running are left
    alone; jobs polled past MAX_POLL_ATTEMPTS are abandoned so a wedged
    container cannot be re-polled forever.
    """
    db.init_db()
    result = CollectResult()
    jobs = db.list_open_jobs(limit=limit)
    if not jobs:
        return result

    own_router = router is None
    if own_router:
        router = build_router(cfg)
    try:
        for job in jobs:
            result.checked += 1
            try:
                _collect_one(job, router, result)
            except ProviderError as e:
                # A provider failure is about this job, not the batch. Record
                # it and keep going so one bad container cannot stall the
                # queue behind it.
                logger.warning("job %d failed: %s", job["id"], e)
                db.finish_job(job["id"], status="failed", error=str(e)[:300])
                result.failed += 1
                result.errors.append(f"job {job['id']}: {e}")
            except Exception as e:
                logger.exception("unexpected error collecting job %d", job["id"])
                db.finish_job(job["id"], status="failed", error=str(e)[:300])
                result.failed += 1
                result.errors.append(f"job {job['id']}: {e}")
    finally:
        if own_router:
            router.close()
    return result


def _collect_one(job, router, result: CollectResult) -> None:
    provider = next((p for p in router.providers_for(Capability(job["capability"]))
                     if p.name == job["provider"]), None)
    if provider is None:
        db.finish_job(job["id"], status="failed",
                      error=f"provider {job['provider']} no longer configured")
        result.failed += 1
        return

    if not provider.is_async:
        # Only async providers leave running jobs behind; a synchronous one
        # here means an earlier run was interrupted mid-write.
        db.finish_job(job["id"], status="failed",
                      error="synchronous job left running; likely an interrupted run")
        result.failed += 1
        return

    attempts = db.bump_job_attempts(job["id"])
    status = provider.jobs().poll(job["container_id"])

    if status not in ("finished", "error", "aborted"):
        if attempts >= MAX_POLL_ATTEMPTS:
            db.finish_job(job["id"], status="timeout",
                          error=f"still {status!r} after {attempts} polls")
            result.timed_out += 1
            logger.warning("job %d abandoned after %d polls (status=%s)",
                           job["id"], attempts, status)
        else:
            result.still_running += 1
        return

    if status != "finished":
        db.finish_job(job["id"], status="failed", error=f"container ended {status!r}")
        result.failed += 1
        return

    agent = provider._require_agent(Capability(job["capability"]))
    rows = provider.jobs().fetch_rows(agent)
    handler = HANDLERS.get(job["capability"])
    if handler is None:
        db.finish_job(job["id"], status="failed",
                      error=f"no handler for capability {job['capability']}")
        result.failed += 1
        return

    summary = handler(job, rows)
    db.finish_job(job["id"], status="finished", result_summary=summary)
    result.finished += 1
    logger.info("job %d finished: %s", job["id"], summary)
