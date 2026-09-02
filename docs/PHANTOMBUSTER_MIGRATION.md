# PhantomBuster migration — plan and task list

**Supersedes** the Phase 2 section of `TRANSITION_SIGNAL_PLAN.html`, which was
written before any PhantomBuster testing and reached several conclusions the
testing has since overturned.

**Decision context:** the move is a provider preference (the GTM team works in
PhantomBuster), not a technical gap. Recorded because it shapes the priority:
capability parity matters more than capability improvement.

**Architecture:** PhantomBuster is the PRIMARY provider. Unipile is FALLBACK
for explicitly documented, verified capability gaps only. Routing is per
capability (`linkedin_agent/providers/router.py`), never per provider.

Last updated: 2026-09-02

---

## 1. Verified capability matrix

Evidence levels are deliberate and load-bearing:

| Level | Meaning |
|---|---|
| **VERIFIED** | A real run's output was inspected and satisfies what our code needs |
| **PARTIAL** | Works, with a documented limitation |
| **UNVERIFIED** | Phantom exists and is plausible — output shape never inspected |
| **GAP** | No PhantomBuster equivalent found |

A similarly-named Phantom is **not** evidence. This investigation produced two
false conclusions from documentation alone: the Unipile probe reported "no work
history" because it used a parameter name from a marketing page, and the first
audit claimed PhantomBuster had "no inbound message model" when it has two
Phantoms for exactly that.

### Reads

| Capability | Provider | Level | Evidence / limitation |
|---|---|---|---|
| Profile enrichment | PhantomBuster | **VERIFIED** | 60 fields incl. `linkedinProfileUrn` (the `ACoAA…` id), headline, location, `connectionDegree`, `professionalEmail` |
| Work experience | PhantomBuster | **PARTIAL** | `linkedinJobDateRange` = `"Feb 2026 - Present"` — **month precision**, better than Unipile's year-only. **Current + one previous job only**; no full history |
| Company firmographics | PhantomBuster | **VERIFIED** | `linkedinCompanyEmployeesCount`, `linkedinCompanySize`, `linkedinCompanyFounded`. **Unipile does not provide this** — removes the need for a curated large-employer list |
| Inbound DM reading | PhantomBuster | **PARTIAL** | Inbox Scraper verified; limitations in §2 |
| Thread-level reply detection | PhantomBuster | **VERIFIED** | `isLastMessageFromMe == FALSE` identifies a prospect reply |
| Prospect matching | PhantomBuster | **VERIFIED** | `linkedInUrls` yields `ACoAA…`, matching `prospects.provider_id`; passes our existing `_PROVIDER_ID_RE`. **No new matching logic needed** |
| API result retrieval | PhantomBuster | **VERIFIED** | `agents/fetch` → `orgS3Folder`/`s3Folder` → `.../result.json`. Not restricted on the trial plan |
| People search | PhantomBuster | **UNVERIFIED** | Agent `8312841525206625` exists, `lastEndType=None` — **never executed** |
| Post/content search | PhantomBuster | **UNVERIFIED** | Search Export docs list `Content (Posts)` via *Switch to advanced setup*. **Not assumed unsupported** — output shape untested (see T-3) |
| Recent posts / activity | PhantomBuster | **UNVERIFIED** | Activity Extractor; no agent created |
| Acceptance detection | PhantomBuster | **UNVERIFIED** | `connectionDegree` is present in Profile Scraper output, so a re-scrape mirrors Unipile's `network_distance` check. Sent Request Extractor is an alternative |

### Writes

| Capability | Provider | Level | Note |
|---|---|---|---|
| Reactions | PhantomBuster | **UNVERIFIED** | Auto Liker; no agent |
| Connection requests | PhantomBuster | **UNVERIFIED** | Auto Connect; max 10/launch |
| DM sending | PhantomBuster | **UNVERIFIED** | Message Sender; 1st-degree only |
| Withdraw invite | PhantomBuster | **UNVERIFIED** | Auto Invitation Withdrawer |
| **Structured per-recipient action errors** | **Unipile** | **GAP** | Containers report `exitCode`, not LinkedIn's error taxonomy. `cooldown_until` depends on `422 errors/already_invited_recently` |

### Application layer — unaffected

Follow-up cadence, campaigns, ICP scoring, signals/evidence, drafter, Telegram
approval, send window, caps and audit logging are **provider-agnostic already**.
They read the DB and normalized DTOs, never a vendor. No changes required.

---

## 2. Inbox Scraper — documented limitations

From the real CSV output of agent `1327575646342095`.

**One row per thread, latest message only.** Not a message list. If a prospect
sends two messages between scrapes, only the last survives. Full conversation
history requires the separate Message Thread Scraper Phantom.

*Impact:* tolerable. We record our own outbound messages at send time, so the
drafter's `prior_messages` becomes our sends plus their latest reply — enough
to draft a reply, but lossy.

**`linkedInUrls` carries the provider_id.** Verified: it yields `ACoAA…`
strings that pass `_PROVIDER_ID_RE` and match `prospects.provider_id`. Prospect
matching reuses `db.get_prospect_by_provider_id()` unchanged.

**`firstnameFrom` / `lastnameFrom` / `occupationFrom` describe the SENDER of
the latest message, not the prospect.** Verified: when
`isLastMessageFromMe=TRUE` these hold *our own* account's details. Mapping them
onto a prospect would silently overwrite records with our own name. **Always
identify the prospect from `linkedInUrls`.**

**No native per-message ID.** Deduplication uses
`sha256(threadUrl + "|" + lastMessageDate)[:32]` written to
`messages.external_id`, whose UNIQUE index already enforces idempotency.
`threadUrl` alone would be wrong — it would suppress every later reply in a
thread. Millisecond timestamps make collisions effectively impossible.

**The Phantom is incremental.** Console output: `"No more data to load,
exiting… Got 0 thread. No new threads found."` It returns only threads new
since the last run, so it deduplicates at source. The synthetic key is a safety
net, not the primary mechanism.

**Unread filter — configured, behaviour UNVERIFIED.** The agent runs with
`"inboxFilter": "unread"` (confirmed via `agents/fetch`), but the test inbox
had no unread messages, so selection behaviour is untested. Matters because thread ordering is *not* by
recency (observed rows spanned 2025-03 → 2026-05 unsorted), so with the
100-threads/launch cap a busy inbox could push a new reply out of the window.

**CSV encoding is corrupted** — `Next.js â€¢ React` is UTF-8 read as latin-1.
The **JSON path is clean**, which is one reason the implementation reads
`result.json`, never the CSV.

**Rate limits:** 100 threads/launch, up to 8 launches/day (~every 3h), versus
the current hourly Unipile poll. **Accepted deliberately** — B2B replies rarely
need sub-3-hour detection. Reversible without code changes: pin `inbox_read`
back to Unipile with a router capability override.

---

## 3. Task list

### Completed

- [x] **T-0** P0-1: Unipile experience verified live (`linkedin_sections=experience`)
- [x] **T-1** PB-0: automated capability probe — `scripts/probe_phantombuster.py`
- [x] **T-2** Profile Scraper output verified; field map captured
- [x] **T-4** Inbox Scraper output verified; limitations documented (§2)
- [x] **T-5** API result retrieval verified (S3 `result.json`, not plan-restricted)
- [x] **T-6** Prospect matching via `linkedInUrls` → `provider_id` verified
- [x] **PB-1** Capability abstraction: `Capability`, DTOs, `CapabilityRouter`,
      typed error taxonomy, `UnipileProvider`, explicit fallback policy — 38 tests

### Outstanding validation — requires a human with PhantomBuster UI access

- [ ] **T-3** Content/Posts search via *Switch to advanced setup*. Does output
      contain **post text** + author URL? Decides the post-search gap.
- [ ] **T-7** Auto Connect **failure** shape. Distinguishable reason, or generic
      failure? Decides whether cooldown protection survives.
- [ ] **T-8** Message Sender per-recipient delivery confirmation. The send funnel
      starts the follow-up clock off this signal.
- [ ] **T-9** Unread filter behaviour (needs an actual unread message).
      Partial: the agent's saved arguments show `"inboxFilter": "unread"`,
      so the filter is CONFIGURED. That is not behaviour — T-9 needs an
      actual unread message to prove the filter selects correctly.
      Also explains the empty first smoke run: no unread messages, rather
      than incremental exhaustion.
- [ ] **T-10** Agent-slot tier. ~10 Phantoms needed; the plan allows **5**.

### Implementation

- [x] **PB-2** PhantomBuster job runner (launch → poll → fetch) + provider
- [x] **PB-3** Normalize PB output into `Position` / `ProfileFacts` / `InboundMessage`
- [x] **PB-4** Capability gating so unverified capabilities cannot silently go live
- [x] **PB-5** Config: `LINKEDIN_PRIMARY_PROVIDER`, agent-id mapping
- [x] **PB-6** `linkedin providers` — routing visible from the CLI
- [x] **PB-7** Migrate `enrichment.py` to the router (profile + experience)
- [x] **P1-2** `positions` table + experience persistence (unblocked by P0-1)
- [x] **PB-8** Migrate `poll.py` to the router (inbound + dedup + matching)
      — implementation complete and fully unit/integration tested (387 tests).
      Remaining: **live-data verification** — confirm a newly received
      LinkedIn message is returned by the real Inbox Scraper and passes
      through normalization → provider-ID matching → persistence →
      Telegram draft workflow. Run `scripts/smoke_pb_inbox.py --expect-from`.
- [ ] **PB-9** Migrate discovery call sites (`search`, `search-posts`, `funding_lookup`)
- [ ] **PB-10** Migrate writes (react/connect/DM) — **blocked on T-7, T-8**
- [x] **PB-11** Async job table for cron-safe batch execution
      (`research_jobs` table, `research_jobs.py`, `linkedin jobs [--collect]`)
- [ ] **PB-12** Flip defaults to PhantomBuster-primary; Unipile fallback only

---

## 4. Architectural rules

**Writes never auto-fall-back.** Only `UnsupportedCapability` routes a write to
another provider. A write whose outcome is unknown may have partially
completed; retrying it elsewhere sends a second invitation or DM to a real
person. Enforced in `router.py`, pinned by test.

**Auth and rate-limit failures never fall back.** An expired cookie is a human
problem that silent fallback would hide. Both providers drive the *same*
LinkedIn account, so falling back on a throttle relocates the abuse.

**Unverified capabilities are not advertised.** `PhantomBusterProvider.supports()`
returns True only for capabilities whose output has been inspected, unless
explicitly enabled by config. This is what prevents an untested Auto Connect
run from reaching real prospects.

**Date precision travels with the data.** `Position.date_precision` and
`has_usable_start` stop a detector claiming "started within 180 days" from a
year-only date.

---

## 5. Remaining Unipile dependencies

| Capability | Reason | Removable when |
|---|---|---|
| Structured action errors | No PhantomBuster equivalent; `cooldown_until` depends on `422 already_invited_recently` | Cooldown state is tracked locally via Sent Request Extractor |
| Post/content search | Unverified on PhantomBuster | T-3 passes |
| All writes | Unverified on PhantomBuster | T-7 and T-8 pass |


---

## 6. Operational security

**A Phantom's saved arguments contain the LinkedIn `sessionCookie`.**
`GET /agents/fetch` returns it in clear text. That cookie *is* the account:
anyone holding it can act as the user until it is invalidated.

`scripts/smoke_pb_inbox.py --show-args` redacts it, but any ad-hoc script
calling `agents/fetch` will print it. Treat agent-argument dumps the way you
would treat a password.

This is a structural difference from Unipile, which holds the session on its
own side and never returns it. It is the concrete form of the ban/credential
risk noted when the provider decision was made.
