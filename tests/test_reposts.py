"""Reposts are interests, never the prospect's own words.

The Activity Extractor returns reposts alongside original posts, and they used
to be dropped outright: the drafter read every post as "something you wrote",
so quoting a repost back put somebody else's words in the prospect's mouth.

Manav's call for the first version of the cadence: a person who reposts rather
than writes is still telling us what they care about, and that should be used.
So reposts now travel — typed as exactly that and nothing more:

  * `interests`, never `observations`: not quotable as theirs
  * never a SIGNAL: a reposted "we're hiring" is somebody else's hiring
  * never the tier: a repost-only prospect is still `weak`
  * never a reaction: the daily step likes only something they wrote
  * never in the drafter's raw `recent_posts`, which it reads as their posts

and a gate rejects a draft that presents one as their words anyway.
"""

from __future__ import annotations

import pytest

from linkedin_agent import daily as daily_mod
from linkedin_agent import drafter as d
from linkedin_agent.adapters.base import Post
from linkedin_agent.adapters.fake_adapter import FakeAdapter
from linkedin_agent.evidence import (EvidenceKind, Tier, build_evidence,
                                     matching_proof_domain)
from linkedin_agent.providers.capabilities import Position, ProfileFacts
from linkedin_agent.providers.phantombuster import PhantomBusterProvider

from tests.test_provider_phantombuster import (OWN_POST_ROW, REPOST_ROW,
                                               _activity_provider)


PROFILE = "https://www.linkedin.com/in/iamghazi/"

FACTS = ProfileFacts(
    full_name="Sam Lee",
    headline="Head of Operations",
    positions=[Position(company="Northwind", title="Head of Operations",
                        start_date="2024-01-01", is_current=True,
                        date_precision="month")],
)

EVAL_REPOST = ("Evals are the new unit tests. If your agent has no regression "
               "suite, every prompt change is a production deploy.")
HIRING_REPOST = "We're hiring senior backend engineers in Bangalore. DM me."
OWN_WORDS = ("Spent the week moving our warehouse scheduling off spreadsheets "
             "and onto a proper system.")


def _repost(text=EVAL_REPOST) -> dict:
    return {"text": text, "posted_at": None, "is_repost": True}


def _own(text=OWN_WORDS) -> dict:
    return {"text": text, "posted_at": None, "is_repost": False}


def _row(n: int, *, own: bool) -> dict:
    base = dict(OWN_POST_ROW if own else REPOST_ROW)
    base["postUrl"] = f"https://www.linkedin.com/feed/update/urn:li:activity:{n}"
    base["postContent"] = f"{'own' if own else 'repost'} {n}"
    return base


# ============================ the provider =================================

@pytest.mark.unit
def test_a_repost_is_marked_as_one(monkeypatch) -> None:
    provider, _ = _activity_provider(monkeypatch, [REPOST_ROW, OWN_POST_ROW])
    posts = provider.get_recent_posts(PROFILE, 5, include_reposts=True)
    assert [(p.text[:7], p.is_repost) for p in posts] == [
        ("My late", False), ("I was a", True)]


@pytest.mark.unit
def test_their_own_posts_come_first_whatever_the_scrape_order(monkeypatch) -> None:
    """A caller that takes posts[0] must get something they wrote."""
    provider, _ = _activity_provider(monkeypatch, [REPOST_ROW, OWN_POST_ROW])
    posts = provider.get_recent_posts(PROFILE, 5, include_reposts=True)
    assert posts[0].is_repost is False


@pytest.mark.unit
def test_reposts_cannot_crowd_out_their_own_words(monkeypatch) -> None:
    """Capped separately. Three reposts ahead of three posts, limit three,
    used to mean three reposts and none of the posts they wrote."""
    rows = [_row(i, own=False) for i in range(3)] + [_row(i + 10, own=True)
                                                     for i in range(3)]
    provider, _ = _activity_provider(monkeypatch, rows)
    posts = provider.get_recent_posts(PROFILE, 3, include_reposts=True)
    assert sum(not p.is_repost for p in posts) == 3
    assert sum(p.is_repost for p in posts) == 3


@pytest.mark.unit
def test_without_the_opt_in_nothing_changes(monkeypatch) -> None:
    provider, _ = _activity_provider(monkeypatch, [REPOST_ROW, OWN_POST_ROW])
    posts = provider.get_recent_posts(PROFILE, 5)
    assert [p.is_repost for p in posts] == [False]


# ============================ the router adapter ===========================

class _PostsOnlyProvider(PhantomBusterProvider):
    """Records exactly what the router handed it."""

    def __init__(self):
        self.kwargs_seen: list[dict] = []

    def supports(self, capability):
        return True

    def verification(self, capability):
        return "verified"

    def get_recent_posts(self, linkedin_url, limit=5, **kwargs):
        self.kwargs_seen.append(kwargs)
        return []


@pytest.mark.unit
def test_the_opt_in_is_only_passed_when_it_is_set() -> None:
    """So a provider that has never heard of reposts is called exactly as
    before, rather than failing on an unexpected keyword."""
    from linkedin_agent.providers.router import CapabilityRouter
    from linkedin_agent.providers.router_adapter import RouterAdapter

    provider = _PostsOnlyProvider()
    adapter = RouterAdapter(CapabilityRouter(provider, None))
    adapter.get_recent_posts(PROFILE, 3)
    adapter.get_recent_posts(PROFILE, 3, include_reposts=True)
    assert provider.kwargs_seen == [{}, {"include_reposts": True}]


# ============================ the daily cron ===============================

def _post(n: int, *, repost: bool) -> Post:
    return Post(post_id=f"urn:li:activity:{n}", url=f"https://x/{n}",
                author_url="https://www.linkedin.com/in/someone-else" if repost
                else PROFILE, text=f"{'shared' if repost else 'own'} post {n}",
                is_repost=repost)


class _ActivityAdapter(FakeAdapter):
    def __init__(self, cfg, posts):
        super().__init__(cfg)
        self._posts = posts

    def get_recent_posts(self, linkedin_url, limit=5, *, include_reposts=False):
        self._record("get_recent_posts", linkedin_url, limit=limit,
                     include_reposts=include_reposts)
        return [p for p in self._posts if include_reposts or not p.is_repost]


def _run_daily(posts):
    from tests.test_daily import _make_cfg

    captured = []

    def drafter(kind, prospect_id, recent_posts=None, evidence=None):
        captured.append({"kind": kind, "posts": recent_posts,
                         "evidence": evidence})
        return f"stub-{kind}"

    cfg = _make_cfg()
    adapter = _ActivityAdapter(cfg, posts)
    daily_mod.run_daily(cfg, adapter=adapter, telegram=None, drafter=drafter)
    return adapter, captured


@pytest.mark.integration
def test_the_react_step_never_likes_a_repost(db_env) -> None:
    """Liking a repost is liking a stranger's post on the prospect's behalf.
    The repost comes first in the scrape here, and must still be skipped."""
    from tests.test_daily import _seed_prospect

    _seed_prospect("targeted", linkedin_url=PROFILE)
    adapter, _ = _run_daily([_post(1, repost=True), _post(2, repost=False)])

    reacted = [c[1][0] for c in adapter.calls if c[0] == "react"]
    assert reacted == ["urn:li:activity:2"]


@pytest.mark.integration
def test_reposts_alone_earn_no_reaction(db_env) -> None:
    """Exactly the behaviour before reposts were fetched: nothing of their own
    to react to, so no reaction and no advance to `reacted`."""
    from linkedin_agent import db
    from tests.test_daily import _seed_prospect

    pid = _seed_prospect("targeted", linkedin_url=PROFILE)
    adapter, _ = _run_daily([_post(1, repost=True)])

    assert not [c for c in adapter.calls if c[0] == "react"]
    assert db.get_prospect(pid)["status"] == "targeted"


@pytest.mark.integration
def test_the_drafter_is_told_which_posts_are_reposts(db_env) -> None:
    """The conversion to dicts used to keep only text and date, which would
    have handed a repost to the evidence engine as their own post."""
    from tests.test_daily import _seed_prospect

    _seed_prospect("targeted", linkedin_url=PROFILE)
    _, captured = _run_daily([_post(1, repost=True), _post(2, repost=False)])

    connect = [c for c in captured if c["kind"] == "connect_note"]
    assert connect, "the connect step never drafted"
    assert {(p["text"], p["is_repost"]) for p in connect[0]["posts"]} == {
        ("own post 2", False), ("shared post 1", True)}
    assert len(connect[0]["evidence"]["interests"]) == 1
    assert len(connect[0]["evidence"]["observations"]) == 1


# ============================ the evidence engine ==========================

@pytest.mark.unit
def test_a_repost_is_an_interest_not_an_observation() -> None:
    bundle = build_evidence(FACTS, [_repost()])
    assert bundle.observations == []
    assert len(bundle.interests) == 1
    assert bundle.interests[0].kind is EvidenceKind.OBSERVATION
    assert "someone else" in bundle.interests[0].source


@pytest.mark.unit
def test_reposts_alone_leave_the_tier_weak() -> None:
    """Moderate means "reference what they wrote". A repost-only prospect has
    written nothing, and the moderate tier would say otherwise."""
    assert build_evidence(FACTS, [_repost()]).tier is Tier.WEAK
    assert build_evidence(FACTS, [_own()]).tier is Tier.MODERATE


@pytest.mark.unit
def test_a_reposted_hiring_post_is_not_their_hiring() -> None:
    """The pain gate exists to stop borrowed claims. A reposted "we're hiring"
    is somebody else's company hiring."""
    bundle = build_evidence(FACTS, [_repost(HIRING_REPOST)])
    assert bundle.signals == []
    assert bundle.pain_claim_licensed is False
    # The same words written by them are a signal.
    assert build_evidence(FACTS, [_own(HIRING_REPOST)]).pain_claim_licensed


@pytest.mark.unit
def test_the_unknowns_say_they_have_written_nothing_themselves() -> None:
    unknowns = " ".join(build_evidence(FACTS, [_repost()]).unknowns)
    assert "written nothing themselves" in unknowns
    assert "no posts retrieved" not in unknowns


@pytest.mark.unit
def test_the_payload_carries_interests_separately() -> None:
    payload = build_evidence(FACTS, [_own(), _repost()]).as_dict()
    assert [o["detail"] for o in payload["observations"]] == [OWN_WORDS]
    assert [i["detail"] for i in payload["interests"]] == [EVAL_REPOST]


@pytest.mark.unit
def test_an_interest_can_steer_which_of_our_work_is_mentioned() -> None:
    """Positioning picks a true thing about US; it licenses nothing about
    them. What caught their interest is exactly what should steer it."""
    vc = "Our portfolio company just closed its round. Proud of this team."
    assert matching_proof_domain(FACTS, build_evidence(FACTS, [])) is None
    assert matching_proof_domain(
        FACTS, build_evidence(FACTS, [_repost(vc)])) is not None


# ============================ the drafter ==================================

@pytest.mark.integration
def test_the_drafters_raw_posts_hold_only_their_own(db_env) -> None:
    """The prompt describes `recent_posts` as their posts."""
    from linkedin_agent import db

    pid = db.upsert_prospect(linkedin_url=PROFILE, full_name="Sam Lee")
    inp = d.build_input("dm1", pid, recent_posts=[_own(), _repost()])
    assert [p["text"] for p in inp.recent_posts] == [OWN_WORDS]


REPOST_ONLY = build_evidence(FACTS, [_repost()]).as_dict()
BOTH = build_evidence(FACTS, [_own(), _repost()]).as_dict()
OWN_ONLY = build_evidence(FACTS, [_own()]).as_dict()


@pytest.mark.unit
@pytest.mark.parametrize("body", [
    "Your post on evals stuck with me.",
    "You wrote about regression suites for agents.",
    "In your words, evals are the new unit tests.",
])
def test_with_only_reposts_any_authorship_is_a_misattribution(body) -> None:
    assert d._misattributes_repost(body, REPOST_ONLY) is not None


@pytest.mark.unit
@pytest.mark.parametrize("body", [
    "Saw you shared a post on evals for agents.",
    "You reposted something on regression suites for agents, which is why I'm writing.",
])
def test_saying_they_shared_it_is_honest(body) -> None:
    assert d._misattributes_repost(body, REPOST_ONLY) is None


@pytest.mark.unit
def test_their_own_words_may_be_attributed_to_them() -> None:
    body = "You wrote about moving your warehouse scheduling off spreadsheets."
    assert d._misattributes_repost(body, BOTH) is None


@pytest.mark.unit
def test_a_reposts_words_may_not_be_attributed_to_them() -> None:
    body = "You wrote that every prompt change is a production deploy."
    assert d._misattributes_repost(body, BOTH) is not None


@pytest.mark.unit
def test_quoting_a_repost_is_a_misattribution_even_unattributed() -> None:
    body = 'Loved the line "every prompt change is a production deploy".'
    assert d._misattributes_repost(body, BOTH) is not None


@pytest.mark.unit
def test_without_reposts_the_gate_never_fires() -> None:
    """Follow-ups fetch no posts, and a dm2 may refer back to the post dm1
    engaged with. This gate is about reposts, so it stays out of that."""
    assert d._misattributes_repost("Following up on your post.", OWN_ONLY) is None
    assert d._misattributes_repost("Following up on your post.", None) is None


@pytest.mark.unit
def test_draft_rejects_a_misattributed_repost_and_retries(monkeypatch) -> None:
    rest = ("the point about regression suites is one I keep coming back "
            "to as well.\n\nWe build AI agents at Agentic Labs.\n\nI have no "
            "idea whether any of that is relevant to what you do at "
            "Northwind, so I would rather ask than guess. Would a short "
            "conversation be useful, or is this not something on your "
            "plate?\n\nBest,\nHaque\nAgentic Labs")
    bad = "Hi Sam,\n\nYour post on evals for agents stuck with me, and " + rest
    good = "Hi Sam,\n\nSaw you shared a post on evals for agents, and " + rest
    responses = [bad, good]
    seen = []

    def fake(prompt, timeout=90):
        seen.append(prompt)
        return responses[min(len(seen) - 1, 1)]

    monkeypatch.setattr(d, "_invoke_claude", fake)
    monkeypatch.setattr(d, "build_input",
                        lambda kind, pid, recent_posts=None, evidence=None:
                        d.DrafterInput(kind=kind, campaign={"brief": ""},
                                       prospect={}, evidence=evidence))
    tries: list = []
    body = d.draft("dm1", 1, evidence=REPOST_ONLY, attempts_out=tries)

    assert body == good
    assert tries[0].category == "misattributed_repost"
    assert "REPOSTED" in seen[1]
