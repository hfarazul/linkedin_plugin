"""PB-11 — cron-safe async job execution.

The behaviour under test is what stops PhantomBuster's latency from wedging
the hourly cron: submit returns immediately, collect applies results on a
later tick, and neither a stuck container nor a single bad job can stall the
queue behind it.

Synchronous providers take the same path so daily.py never branches on
provider — a job row is written either way, which also keeps the audit trail
identical regardless of who served the request.
"""

from __future__ import annotations

import pytest

from linkedin_agent.providers.capabilities import Capability, ProfileFacts


PROFILE_ROW = {
    "firstName": "Anjan", "lastName": "B",
    "linkedinProfileUrn": "ACoJOBTEST0000000000",
    "profileUrl": "https://www.linkedin.com/in/anjan-b/",
    "linkedinHeadline": "Software Engineer",
    "location": "Bengaluru",
    "linkedinJobTitle": "Software Engineer",
    "linkedinJobDateRange": "Feb 2026 - Present",
    "linkedinCompanyName": "TalkingLands",
    "linkedinCompanyEmployeesCount": "42",
    "linkedinPreviousJobTitle": "Co-Founder",
    "linkedinPreviousJobDateRange": "Aug 2025 - Present",
    "previousCompanyName": "dan Lab",
}


class FakeJobs:
    """Stands in for PhantomBusterJobs."""

    def __init__(self, *, status="finished", rows=None, container="c1"):
        self._status = status
        self._rows = rows if rows is not None else [PROFILE_ROW]
        self._container = container
        self.submitted = []

    def submit(self, agent_id, arguments=None):
        self.submitted.append((agent_id, arguments))
        return self._container

    def poll(self, container_id):
        return self._status

    def fetch_rows(self, agent_id):
        return list(self._rows)

    def fetch_all_rows(self, agent_id):
        # The agent's cumulative CSV across every run. Defaults to the same
        # rows as result.json, so only tests that set `all_rows` exercise the
        # deduplicated-profile recovery.
        return list(getattr(self, "all_rows", self._rows))

    def close(self):
        pass


class FakeAsyncProvider:
    name = "phantombuster"
    is_async = True

    def __init__(self, jobs):
        self._jobs = jobs

    def supports(self, capability):
        return capability in (Capability.PROFILE, Capability.EXPERIENCE)

    def _require_agent(self, capability):
        return "agent-123"

    def jobs(self):
        return self._jobs

    def close(self):
        pass


class FakeSyncProvider:
    name = "unipile"
    is_async = False

    def __init__(self, facts=None):
        self._facts = facts

    def supports(self, capability):
        return capability in (Capability.PROFILE, Capability.EXPERIENCE)

    def fetch_profile(self, identifier, *, with_experience=False):
        return self._facts

    def close(self):
        pass


def _router(provider):
    from linkedin_agent.providers.router import CapabilityRouter
    return CapabilityRouter(provider, None)


def _cfg():
    return type("CFG", (), {
        "unipile_api_key": None, "unipile_account_id": None,
        "unipile_dsn": None, "dry_run": False,
    })()


def _seed(db, provider_id="ACoJOBTEST0000000000"):
    return db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/job-test",
        full_name="Job Test", provider_id=provider_id)


# ----------------------------- submit: async ---------------------------------

@pytest.mark.integration
def test_async_submit_returns_immediately(db_env) -> None:
    """The whole point: no blocking, so a per-prospect loop stays fast."""
    from linkedin_agent import db, research_jobs
    pid = _seed(db)
    jobs = FakeJobs()
    out = research_jobs.submit_profile(
        _cfg(), pid, router=_router(FakeAsyncProvider(jobs)))

    assert out.status == "running"
    assert jobs.submitted, "the container should have been launched"
    job = db.get_job(out.job_id)
    assert job["status"] == "running"
    assert job["container_id"] == "c1"
    assert job["provider"] == "phantombuster"


@pytest.mark.integration
def test_duplicate_submit_is_skipped_while_one_is_in_flight(db_env) -> None:
    """Without this, every cron tick launches another container for the same
    prospect while the first is still running."""
    from linkedin_agent import db, research_jobs
    pid = _seed(db)
    jobs = FakeJobs()
    router = _router(FakeAsyncProvider(jobs))
    research_jobs.submit_profile(_cfg(), pid, router=router)
    second = research_jobs.submit_profile(_cfg(), pid, router=router)

    assert second.status == "skipped"
    assert len(jobs.submitted) == 1
    assert len(db.list_open_jobs()) == 1


# ----------------------------- submit: sync ----------------------------------

@pytest.mark.integration
def test_sync_provider_runs_inline_but_still_records_a_job(db_env) -> None:
    """One code path for both providers, and one audit trail."""
    from linkedin_agent import db, research_jobs
    pid = _seed(db)
    facts = ProfileFacts(provider_id="ACoJOBTEST0000000000",
                         headline="Founder", source="unipile")
    out = research_jobs.submit_profile(
        _cfg(), pid, router=_router(FakeSyncProvider(facts)))

    assert out.status == "finished"
    job = db.get_job(out.job_id)
    assert job["status"] == "finished"
    assert job["container_id"] is None
    assert db.get_prospect(pid)["headline"] == "Founder"


@pytest.mark.integration
def test_sync_failure_is_recorded_on_the_job(db_env) -> None:
    from linkedin_agent import db, research_jobs
    pid = _seed(db)
    out = research_jobs.submit_profile(
        _cfg(), pid, router=_router(FakeSyncProvider(None)))
    assert out.status == "failed"
    assert db.get_job(out.job_id)["status"] == "failed"


# ----------------------------- submit: guards --------------------------------

@pytest.mark.integration
def test_prospect_without_identifier_is_skipped(db_env) -> None:
    from linkedin_agent import db, research_jobs
    pid = db.upsert_prospect(linkedin_url="")
    out = research_jobs.submit_profile(
        _cfg(), pid, router=_router(FakeAsyncProvider(FakeJobs())))
    assert out.status == "skipped"


@pytest.mark.integration
def test_missing_prospect_is_skipped_not_raised(db_env) -> None:
    from linkedin_agent import research_jobs
    out = research_jobs.submit_profile(
        _cfg(), 9999, router=_router(FakeAsyncProvider(FakeJobs())))
    assert out.status == "skipped"


# ----------------------------- collect ---------------------------------------

@pytest.mark.integration
def test_collect_applies_a_finished_job(db_env) -> None:
    from linkedin_agent import db, research_jobs
    pid = _seed(db)
    provider = FakeAsyncProvider(FakeJobs(status="finished"))
    research_jobs.submit_profile(_cfg(), pid, router=_router(provider))

    result = research_jobs.collect(_cfg(), router=_router(provider))

    assert result.finished == 1
    prospect = db.get_prospect(pid)
    assert prospect["headline"] == "Software Engineer"
    assert prospect["enriched_at"] is not None
    assert len(db.list_positions(pid)) == 2
    assert db.list_open_jobs() == []


@pytest.mark.integration
def test_collect_leaves_running_jobs_alone(db_env) -> None:
    from linkedin_agent import db, research_jobs
    pid = _seed(db)
    provider = FakeAsyncProvider(FakeJobs(status="running"))
    research_jobs.submit_profile(_cfg(), pid, router=_router(provider))

    result = research_jobs.collect(_cfg(), router=_router(provider))

    assert result.still_running == 1
    assert result.finished == 0
    assert len(db.list_open_jobs()) == 1


@pytest.mark.integration
def test_collect_marks_errored_containers_failed(db_env) -> None:
    from linkedin_agent import db, research_jobs
    pid = _seed(db)
    provider = FakeAsyncProvider(FakeJobs(status="error"))
    out = research_jobs.submit_profile(_cfg(), pid, router=_router(provider))

    result = research_jobs.collect(_cfg(), router=_router(provider))

    assert result.failed == 1
    assert db.get_job(out.job_id)["status"] == "failed"


@pytest.mark.integration
def test_wedged_container_is_abandoned_after_max_polls(db_env) -> None:
    """A container that never finishes must not be re-polled forever on every
    cron tick."""
    from linkedin_agent import db, research_jobs
    pid = _seed(db)
    provider = FakeAsyncProvider(FakeJobs(status="running"))
    out = research_jobs.submit_profile(_cfg(), pid, router=_router(provider))

    with db.connect() as conn:
        conn.execute("UPDATE research_jobs SET attempts = ? WHERE id = ?",
                     (research_jobs.MAX_POLL_ATTEMPTS - 1, out.job_id))

    result = research_jobs.collect(_cfg(), router=_router(provider))
    assert result.timed_out == 1
    assert db.get_job(out.job_id)["status"] == "timeout"


@pytest.mark.integration
def test_one_bad_job_does_not_stall_the_queue(db_env) -> None:
    """Jobs are independent; a provider error on one must not prevent the
    others from being collected."""
    from linkedin_agent import db, research_jobs

    class Exploding(FakeJobs):
        def poll(self, container_id):
            raise RuntimeError("provider blew up")

    pid = _seed(db)
    other = db.upsert_prospect(linkedin_url="https://www.linkedin.com/in/other",
                               provider_id="ACoOTHER00000000000")
    db.create_job(Capability.PROFILE.value, "phantombuster",
                  prospect_id=pid, container_id="bad")
    db.create_job(Capability.PROFILE.value, "phantombuster",
                  prospect_id=other, container_id="bad2")

    result = research_jobs.collect(
        _cfg(), router=_router(FakeAsyncProvider(Exploding())))

    assert result.checked == 2
    assert result.failed == 2
    assert db.list_open_jobs() == [], "both jobs resolved, neither left hanging"


@pytest.mark.integration
def test_collect_with_no_jobs_is_a_cheap_noop(db_env) -> None:
    """Runs on every cron tick, so the empty case must not build a router."""
    from linkedin_agent import research_jobs
    result = research_jobs.collect(_cfg())
    assert result.checked == 0


@pytest.mark.integration
def test_job_for_a_deconfigured_provider_fails_cleanly(db_env) -> None:
    from linkedin_agent import db, research_jobs
    pid = _seed(db)
    job_id = db.create_job(Capability.PROFILE.value, "some-old-provider",
                           prospect_id=pid, container_id="c9")

    result = research_jobs.collect(
        _cfg(), router=_router(FakeAsyncProvider(FakeJobs())))

    assert result.failed == 1
    assert "no longer configured" in db.get_job(job_id)["error"]


# ----------------------------- applied data ----------------------------------

@pytest.mark.integration
def test_applied_profile_respects_the_no_overwrite_rule(db_env) -> None:
    """A job-applied profile must write exactly what an inline one would,
    including not erasing fields the provider did not return."""
    from linkedin_agent import db, research_jobs
    pid = _seed(db)
    with db.connect() as conn:
        conn.execute("UPDATE prospects SET pronoun = 'He/Him' WHERE id = ?", (pid,))

    provider = FakeAsyncProvider(FakeJobs())
    research_jobs.submit_profile(_cfg(), pid, router=_router(provider))
    research_jobs.collect(_cfg(), router=_router(provider))

    assert db.get_prospect(pid)["pronoun"] == "He/Him"


@pytest.mark.integration
def test_collect_logs_an_enrich_action(db_env) -> None:
    import json
    from linkedin_agent import db, research_jobs
    pid = _seed(db)
    provider = FakeAsyncProvider(FakeJobs())
    research_jobs.submit_profile(_cfg(), pid, router=_router(provider))
    research_jobs.collect(_cfg(), router=_router(provider))

    with db.connect() as conn:
        row = conn.execute(
            "SELECT payload FROM actions WHERE kind='enrich' AND prospect_id=?",
            (pid,)).fetchone()
    payload = json.loads(row["payload"])
    assert payload["via"] == "job"
    assert payload["positions"] == 2


@pytest.mark.integration
def test_job_result_for_a_different_person_is_not_applied(db_env):
    """The result file is agent-scoped, so it can hold somebody else's row.

    Two profile jobs in flight on the same Profile Scraper read the same
    `result.json`, and a container that reports finished can still be serving
    the previous run's rows. Taking rows[0] on trust wrote one prospect's
    headline and positions onto another — and stamped `enriched_at` in the
    same statement, so the wrong data then sat there for the whole staleness
    window instead of being refetched.

    `fetch_profile` has always guarded this inline with _match_requested. The
    job path did not.
    """
    from linkedin_agent import db, research_jobs

    pid = db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/the-right-person",
        full_name="Right Person",
        provider_id="ACoRIGHTPERSON000000",
    )
    job_id = db.create_job(Capability.PROFILE.value, "phantombuster",
                           prospect_id=pid, target="ACoRIGHTPERSON000000")
    job = db.get_job(job_id)

    # PROFILE_ROW is somebody else entirely — the previous run's target.
    with pytest.raises(research_jobs.ResultNotReady, match="not applying it"):
        research_jobs._apply_profile(job, [PROFILE_ROW])

    after = db.get_prospect(pid)
    assert after["headline"] is None, "another person's headline was written"
    assert after["enriched_at"] is None, "a miss must not stamp freshness"
    assert db.list_positions(pid) == []


@pytest.mark.integration
def test_job_result_for_the_requested_person_is_applied(db_env):
    """The counterpart: a matching row still lands, keyed on the provider id."""
    from linkedin_agent import db, research_jobs

    pid = db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/anjan-b/",
        full_name="Anjan B",
        provider_id="ACoJOBTEST0000000000",
    )
    job_id = db.create_job(Capability.PROFILE.value, "phantombuster",
                           prospect_id=pid, target="ACoJOBTEST0000000000")

    summary = research_jobs._apply_profile(db.get_job(job_id), [PROFILE_ROW])

    assert "applied" in summary
    after = db.get_prospect(pid)
    assert after["headline"] == "Software Engineer"
    assert after["enriched_at"] is not None


@pytest.mark.integration
def test_a_mismatched_result_defers_rather_than_finishing_or_failing(db_env):
    """A lagging result file and a wrong target are indistinguishable now.

    A container can report finished before result.json lands in S3, so an early
    read serves the previous run's rows. fetch_profile absorbs that by sleeping
    through RESULT_SETTLE_ATTEMPTS; a cron collect must not sleep, so the job
    stays open and is re-checked on the next tick.

    What is unconditional either way is that the row we were handed is not
    written. Refusing to write is the safe half; deciding the target is
    permanently wrong is the half that needs patience.
    """
    from linkedin_agent import db, research_jobs

    pid = db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/the-right-person",
        full_name="Right Person",
        provider_id="ACoRIGHTPERSON000000",
    )
    job_id = db.create_job(Capability.PROFILE.value, "phantombuster",
                           prospect_id=pid, target="ACoRIGHTPERSON000000",
                           container_id="c1")

    jobs = FakeJobs(status="finished", rows=[PROFILE_ROW])
    result = research_jobs.collect(_cfg(), router=_router(FakeAsyncProvider(jobs)))

    assert result.still_running == 1
    assert result.finished == 0 and result.failed == 0
    assert db.get_job(job_id)["status"] == "running"
    assert db.get_prospect(pid)["enriched_at"] is None


@pytest.mark.integration
def test_a_result_that_never_settles_eventually_times_out(db_env):
    """Deferring must not mean retrying forever. The existing poll ceiling
    applies, so a genuinely wrong target is abandoned rather than relaunched
    every tick for good."""
    from linkedin_agent import db, research_jobs

    pid = db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/the-right-person",
        full_name="Right Person",
        provider_id="ACoRIGHTPERSON000000",
    )
    job_id = db.create_job(Capability.PROFILE.value, "phantombuster",
                           prospect_id=pid, target="ACoRIGHTPERSON000000",
                           container_id="c1")
    with db.connect() as conn:
        conn.execute("UPDATE research_jobs SET attempts = ? WHERE id = ?",
                     (research_jobs.MAX_POLL_ATTEMPTS, job_id))

    jobs = FakeJobs(status="finished", rows=[PROFILE_ROW])
    result = research_jobs.collect(_cfg(), router=_router(FakeAsyncProvider(jobs)))

    assert result.timed_out == 1
    assert db.get_job(job_id)["status"] == "timeout"
    assert db.get_prospect(pid)["enriched_at"] is None


@pytest.mark.integration
def test_a_deduplicated_profile_is_served_from_the_cumulative_results(db_env):
    """The Phantom skips a profile it has already scraped, exiting in seconds
    and leaving the previous run's result.json. The row is still in the agent's
    cumulative CSV, so a skip is a cache hit rather than a failure — the same
    recovery fetch_profile makes inline, which the job path had lost."""
    from linkedin_agent import db, research_jobs

    pid = db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/anjan-b/",
        full_name="Anjan B",
        provider_id="ACoJOBTEST0000000000",
    )
    job_id = db.create_job(Capability.PROFILE.value, "phantombuster",
                           prospect_id=pid, target="ACoJOBTEST0000000000",
                           container_id="c1")

    # result.json holds the PREVIOUS run's person; the cumulative CSV has ours.
    other = dict(PROFILE_ROW)
    other["profileUrl"] = "https://www.linkedin.com/in/someone-else/"
    other["linkedinProfileUrn"] = "ACoSOMEONEELSE000000"
    jobs = FakeJobs(status="finished", rows=[other])
    jobs.all_rows = [other, PROFILE_ROW]

    result = research_jobs.collect(_cfg(), router=_router(FakeAsyncProvider(jobs)))

    assert result.finished == 1
    assert db.get_job(job_id)["status"] == "finished"
    assert db.get_prospect(pid)["headline"] == "Software Engineer"
