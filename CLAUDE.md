# LinkedIn Outreach Agent — Software Agency Lead-Gen

This project runs LinkedIn outreach for Agentic Labs (previously Cortivo), a software agency. **You (Claude Code) are the agent.** The Python package `linkedin_agent` is the toolkit you drive.

Day-to-day, most operations are automated by cron. You're invoked when there's *judgment* to apply: writing a campaign brief, drafting a custom message, replying to an interested prospect.

---

## Read this first — invariants that are not obvious from the code

These were each established by a defect that reached, or nearly reached, a real person. Every one of them is enforced somewhere in code, but the reasoning lives here. **Do not "simplify" past any of them without reading the linked rationale.**

### 1. `messages` is not proof that a DM was sent

A row in `messages` means *we composed something and dispatched it*. It does not mean LinkedIn delivered it, and it does not mean it was a DM — **connection notes are recorded as outbound messages too**.

**`dm_count` is the single source of truth for DM1 gating.** `record_message` never touches it. If you find yourself writing `SELECT ... FROM messages` to decide whether to send a first DM, you are reintroducing a bug.

### 2. Connection notes are outbound messages

`send_draft_via_adapter` and `linkedin connect` both write the note to `messages`. Two things depend on it:

- **Reply detection.** `poll` decides whether an inbound halts the sequence by asking whether it post-dates our last outbound. Without the note recorded, every prospect at `connection_sent`/`connected` had no outbound row and fell through to a coarse age check — and a reply to a connect note is the commonest inbound there is.
- **Drafter context.** `build_input` reads `messages` for dm2/dm3/reply threads. Without the note, the thread shown to the drafter was missing its first turn.

Note dm1 does *not* currently pull thread context (`build_input` only does so for dm2/dm3/reply), so the first DM cannot see the connect note and may repeat it. Known, deliberate, one-word fix if you want it.

### 3. An unconfirmed send advances the pipeline but not the clock

PhantomBuster reports that a *container finished*, not that a recipient received anything. That is neither success nor failure, and it gets its own handling:

- `dm_count` **is** bumped — otherwise the next cron tick drafts DM1 again, and repeat sends are the worse failure.
- `last_dm_at` **is cleared** — a follow-up cadence timed from a send nobody observed produces "circling back on what I sent last week" to someone who was sent nothing.

`db.record_dm(prospect_id, confirmed=...)` — **`confirmed` is required, deliberately.** A default silently handed the old behaviour to unmigrated call sites once already. Detect the state with `providers.router_adapter.is_unconfirmed(api_result)`; log it with `bot_daemon.log_unconfirmed`. `linkedin status` surfaces these so parked prospects aren't forgotten.

Consequence worth knowing: `ghosted` can outrun its evidence. If an unconfirmed DM3 never landed, "messaged three times, never replied" is a claim about behaviour we didn't observe. The `send_unconfirmed` action row preserves the truth.

### 4. Unverified writes stay disarmed by default

`PHANTOMBUSTER_ENABLE_UNVERIFIED` gates unverified **writes** (`react`, `connect`, `send_dm`). Setting an agent id *configures* a Phantom; it does not *arm* one.

Unverified **reads** need no opt-in — with one provider left, gating a read off routes around nothing, and a malformed read raises rather than reaching anyone.

**`DRY_RUN=0` is not authorization.** It is currently `0` on the deployment host. The opt-in gate is the real backstop. `linkedin providers` shows `DISARMED` (configured, deliberately off) distinctly from `NONE` (nothing configured) — different problems, opposite fixes.

### 5. `lastMessageDate` is millisecond-precision ISO

Real Inbox Scraper output looks like `"2025-05-31T09:57:02.966Z"`, not date-only. The inbound-vs-outbound ordering comparison in `poll` depends on this: a same-day reply must not sort behind our outbound.

### 6. The evidence gates must be wired before any email sending ships

**This is a hard prerequisite, not a nice-to-have.**

`linkedin_agent/evidence.py` types everything known about a prospect by *what it licenses us to say*, and `drafter.draft()` enforces eight gates on top of it. But **no production path passes `evidence=`** — `daily.py`, `followup.py` and `poll.py` all call the drafter without it, and the only caller of `build_evidence` is `scripts/smoke_e2e.py`.

With `evidence=None`, `enforce_pain_gate` is False for DM kinds and `thin_evidence` is False, so **the pain-claim and inferred-relevance gates are inactive on the live DM path.** Nothing reaches anyone today because email sending isn't built and LinkedIn writes are disarmed — but those gates are what make a first-touch draft defensible, and they must be in the execution path before the email path goes live.

### 7. A repost is an interest, not their words

The Activity Extractor returns reposts alongside original posts. `daily` and the smoke harness fetch them (`include_reposts=True`), and they arrive marked `Post.is_repost`. `evidence.build_evidence` types them as `interests`: an `OBSERVATION` with `authored=False`. That is all they are:

- never in `observations`, never the tier: a prospect who only reposts is still `weak`
- never a `SIGNAL`: a reposted "we're hiring" is somebody else's hiring
- never in the drafter's raw `recent_posts`, which the prompt calls their posts
- never reacted to: the react step likes only a post they wrote

The `misattributed_repost` gate rejects a draft that presents one as their words. **Anything converting `Post`s to dicts must keep `is_repost`** — use `daily.draft_posts`. The old `{"text", "posted_at"}` shape silently turns a repost into their own post.

---

## The system in 30 seconds

```
campaign brief (markdown)
        │
        ▼
hourly cron (`linkedin daily`)
        │  - syncs campaigns from markdown files
        │  - polls PhantomBuster Inbox Scraper for inbound replies
        │  - checks pending invites for acceptance (bounded per cycle)
        │  - reacts to targeted prospects' recent posts
        │  - drafts connect notes / DM1 / DM2 / DM3 via the message-drafter subagent
        ▼
Telegram bot (drafts post with Approve/Edit/Reject buttons)
        │
        ▼  user taps on phone
PhantomBuster → LinkedIn
```

Drafts approved outside business hours stay queued (`status='approved'`) until the next 9-5 Mon-Fri window.

**Unipile has been removed.** `LINKEDIN_PRIMARY_PROVIDER=phantombuster` is the only routable provider; `build_router` raises on `unipile`. The module and its tests survive as a date-parsing reference only.

## Architecture — five layers

```
CLI (click)              cli.py — ~30 commands
        │
Orchestration            daily.py (8-step cron), poll.py, followup.py, bot_daemon.py
        │
Judgment                 evidence.py, icp_scoring.py, drafter.py (the eight gates)
        │
Capability seam          providers/{base,capabilities,router}.py
        │                Capability enum → router → provider. Nothing above this
        │                seam sees vendor JSON.
Vendor                   providers/phantombuster.py + pb_jobs.py
```

Two seams carry the design:

- **`providers/router.py`** picks a provider per *capability*, never per vendor, and its fallback policy is keyed on *error class*: `UnsupportedCapability` and `MalformedResponse` may fall through; `ProviderAuthError` and `ProviderRateLimited` never do (both providers drive one LinkedIn account, so falling back deepens a block); writes fall back on nothing but an explicit "cannot".
- **`providers/router_adapter.py`** presents the router as the legacy `LinkedInAdapter`, so existing call sites in `cli.py` / `daily.py` / `bot_daemon.py` were never rewritten.

## Capability state

Check the live view rather than trusting this table — `linkedin providers` reads the actual config.

| Capability | State | Notes |
|---|---|---|
| profile / experience | **verified** | Profile Scraper, run live many times |
| inbox_read | **verified** | Inbox Scraper |
| acceptance_check | **verified** | From `connectionDegree` |
| recent_posts | unverified | Runs live and works; flag is conservative |
| search_people | unverified | Configured, output never inspected |
| search_posts | **no provider** | No PhantomBuster equivalent exists |
| react / connect / send_dm | **disarmed** | See invariant 4 |
| email sending | **not built** | Renders and stops at `NOT SENT` |

> **One agent, one job.** Retargeting a Phantom writes to its **saved** configuration — a launch-time argument is accepted and then ignored. Two runs pointing the same agent at different profiles clobber each other. This is also why the result file is agent-scoped and why `_apply_profile` matches the returned row against the job's target before writing it.

## Campaign-first workflow

Every prospect belongs to a campaign. Campaigns are markdown files under `campaigns/`, and they are the source of truth — DB rows are derived.

`campaigns/_agentic_labs.md` is the **only authority on what we may claim about the company**. The `ungrounded_cortivo_claim` gate checks names, figures and practice claims against it. Editing it changes what the drafter is allowed to say on the next draft. (Agentic Labs was previously Cortivo; `_cortivo.md` is retired.)

It separates **CITABLE** claims (published on theagenticlabs.ai — the prospect can check them) from **APPROVED** ones (true, but unpublished — usable, never linked).

Two traps specific to this file, both of which have already bitten:

- **Writing a forbidden phrase into the brief in order to forbid it approves it.** The brief is also the approved vocabulary and number set, so a quoted "6-10 weeks" whitelists `6` and `10`. HTML comments are stripped before tokenising, precisely because they quote the incidents they explain — but prose outside a comment is not.
- **Some claims are built entirely from approved parts.** "A 3-4 person team" uses digits approved for unrelated claims (`3` from "Top 3% on Toptal", `4` from "4 minutes"). The number gate checks membership, not meaning, so these are caught by `_contains_unsupported_authority` in `drafter.py` instead. Literal phrase lists alone were not enough: "six-to-ten weeks", "20+ years" and "a 3 to 4-person team" all got past them. So the text is folded first (hyphens, `+`, number words) and the claims are matched as patterns, only in sentences about us. Add a rewording to `test_rewording_does_not_get_a_claim_past_the_gate` before widening a pattern.

**Claims about a person live in `senders/<slug>.md`, not here.** A sender is chosen by the campaign's `sender:` frontmatter, then `OUTREACH_SENDER`, then the default (`haque`). A sender's `personal_seniority` is licensed only when they send, and only in the first person — Manav's "two decades" is his own, confirmed by him, so "we bring two decades" is rejected even when he sends. A named sender with no profile is a hard error, never a fallback.

To create one: `linkedin campaign create <slug>` scaffolds the file; edit it; `linkedin campaign sync` (or any `daily` run) refreshes the DB.

## Pipeline stages

```
targeted → reacted → connection_sent → connected → dm_sent → replied
```

Plus a `disposition` column (set after conversation begins):
`interested · not_fit · ghosted · won · lost · deferred`

`ghosted` auto-applies 14 days after DM3 with no reply — falling back to `last_action_at` when `last_dm_at` is NULL, so an unconfirmed final send doesn't park a prospect forever. The others are manual flags.

## Daily ops — what the cron does

| Step | What | Approval needed |
|---|---|---|
| `campaigns sync` | Refresh DB from markdown files | — |
| `poll` | Fetch inbound replies, halt sequences, **auto-draft a reply** | **Yes (Telegram)** |
| `check-accepts` | Detect accepted invites → `connected`. **Bounded per cycle** (`DAILY_MAX_ACCEPTANCE_CHECKS`, default 4) — each check is a profile re-scrape, and an unbounded sweep outran the hourly cron. Rotates on `acceptance_checked_at`. | — |
| `react` | Like recent post of each `targeted` prospect → `reacted` | No (low stakes) |
| `connect` | Draft connect note for each `reacted` prospect | **Yes (Telegram)** |
| `dm1` | Draft first DM for each `connected` prospect with `dm_count=0` | **Yes (Telegram)** |
| `followup` | Draft DM2 (after 4d) / DM3 (after 11d) for `dm_sent` prospects | **Yes (Telegram)** |
| `send-approved` | Flush drafts approved outside the business-hours window | — |
| `auto-ghost` | Mark stale `dm_count=3` prospects as `ghosted` | — |

## Reply handling — two separate gates

`poll` asks two different questions about an inbound, and they must not be collapsed:

- **Should this halt the sequence?** Decided by *ordering* — is the inbound newer than our last outbound? A genuine reply halts however old it is. Age is only the fallback where we have never written to them.
- **Should we auto-draft a reply to it?** Decided by *age* (`REPLY_DRAFT_MAX_AGE_DAYS`, default 30). A 40-day-old reply stops the sequence without being answered 40 days late.

The Inbox Scraper is incremental, so its first run against a real account returns the entire backlog — threads up to 15 months old. A backlog inbound is recorded and notified (logged as `reply_stale`) but must never touch the pipeline.

## When Claude Code is needed (interactive)

1. **Creating a new campaign** — follow the protocol below. Don't just `campaign create` and let the user write a brief in the dark.
2. **Recrafting a reply the auto-drafter got wrong.** Read the full thread (`messages` for that prospect) and the campaign brief, draft, send via `linkedin dm <pid> "..."`.
3. **Tuning the drafter prompt.** Iterate on `.claude/agents/message-drafter.md`, then re-run `scripts/smoke_e2e.py` to see the new style.
4. **One-off prospect work.** Use the message-drafter subagent directly.

## Testing one profile, end to end

**The command to reach for first.** Runs the real pipeline against a throwaway DB and narrates all twelve stages — which provider served each, how long it took, and *why* a record was accepted or rejected.

```powershell
.venv\Scripts\python.exe scripts\smoke_e2e.py --profile "https://www.linkedin.com/in/<slug>/"
```

Add `--real-drafter` to invoke Claude and write an actual email (up to 3 calls if gates reject attempts). Other flags: `--campaign <slug>`, `--kind email1|dm1|connect_note`, `--skip-geo`, `--skip-role` (both log the bypass so it never looks like a pass), `--real-telegram`.

Stages that tell you the most: **07** (ICP scoring — should we contact them at all), **10** (`pain_claim_licensed` — what may we claim), **11** (attempts and which gate fired), **12** (validation, then the rendered email, then `NOT SENT`).

`scripts/fingerprint_report.py <dir>` measures what a *batch* of drafts has in common. Every fingerprint this project has removed was invisible in a single draft and obvious in five.

## The eight-email cadence

Leadership's GTM brief asks for eight emails to one prospect: professional and direct, leading with the sender's authority, built on their LinkedIn activity where it exists and on their role where it does not, with at least three short follow-ups and a threaded subject. `linkedin_agent/sequence.py` drafts it as **one thread with state**, never eight independent drafts.

```powershell
.venv\Scripts\python.exe scripts\smoke_e2e.py --profile "https://www.linkedin.com/in/<slug>/" --campaign ops-leaders-ai-cadence --real-drafter --sequence
```

The whole thread lands in `data/sequences/<slug>-<stamp>.md` (plus `.json` state), gitignored because it holds a real person's details. `--sequence` refuses a prospect the campaign's ICP drops, and refuses `--skip-geo`/`--skip-role`.

What the cadence does differently from the rest of the system, each enforced in code:

- **A role-based problem is a `HYPOTHESIS`** (`evidence.build_hypotheses`) — our guess, typed as one. It licenses a *question* ("something I run into a lot with COOs is X — true for you?") and a pattern stated about other people. `hypothesis_as_fact` rejects the guess's own words said to "you" outside a question. It never sets the tier or licenses a claim.
- **The career angle is never said back.** Long tenure (4+ years in role, or 6+ continuous at the employer) adds one guess, `next_lever`, which asks whether AI implementation is a lever they are looking at. `career_diagnosis` rejects "stuck", "next chapter", "hero", anything about their career, and any tenure figure. The guess itself carries no number.
- **Content is rationed at selection, not caught afterwards.** Each main email is handed at most one piece of their activity, and the ledger never offers a piece already used in 3 emails or in the 2 emails before. An email's evidence holds only its piece, so a signal licenses a claim only in the email given the post that made it.
- **It stops rather than pads.** With nothing fresh left for a main email, the thread ends there and says why.
- **A case study is cited only where its documented description supports the guess** — `RoleHypothesis.proof`, each entry annotated with what the brief shows. There is no fallback list: the first cut had one, and cited Experial (documented only as *piloted*) for "pilots that never go live" and Bespoke (a wealth-advisor copilot) for "manual reporting". A guess with no supported case study gets an email that cites none. Cited verbatim from the brief, each once, as "we built", never "I built", no links. The opener never takes the `proof_point` angle, which asks the drafter to pick one by resemblance.
- **Own posts vs reposts.** "You wrote/posted" only for their own posts; a repost is "shared/reposted", read at most as a possible interest, never quoted, never a diagnosis. Likes, reactions and comments are not collected, and `unseen_activity` rejects any mention of one.
- **The company is named at most once per main email, never in a follow-up, and in no more than three emails** (`company_repetition`). A company whose identifying word is an ordinary one ("Capital", "Work") is not policed.
- **No narrated change of subject** in a main email (`announced_transition`: "leaving X aside", "I'll leave X there", "different question this time").
- **Each follow-up has its own job** — a yes-or-no version, a practical example, a narrower question, a close — so none just re-asks the previous question. Two jobs have a deterministic signature and are checked (`followup_job`): the yes-or-no email must ask a closed question, and the close must say it is the last note and ask about timing or who owns this. The example and narrower-question jobs have none — telling them from a reworded repeat needs meaning, not pattern — so they rest on their instruction, and the review file says they were not checked. Read the regenerated thread.
- Main emails are 80–119 words, follow-ups at most 50 and must ask a question, the first subject has their first name as word one or two, and no email may repeat a seven-word run from an earlier one.

## Campaign creation protocol — follow this every time

### Phase 1 — Clarifying questions (ask all 8)

1. **Who, specifically?** Role + company stage + size.
2. **Where?** Country/region/cities.
3. **What pain?** 2-3 specific points the prospect would recognize.
4. **Why now?** What trigger makes them open to outreach this quarter?
5. **What Cortivo angle?** Mutual connections, shared school (IIT), a vertical we've shipped in.
6. **Anti-claims?** What this campaign explicitly avoids saying.
7. **Tone?** Financial/operational? Peer-to-peer? Consultative? Technical?
8. **Search queries?** 2-3 LinkedIn classic-search keyword strings. Classic search is keyword-only — "we just raised" returns investors talking about deals.

Push back on vague answers: "founders" → stage + vertical; "AI companies" → buyer profile; "tech founders" → technical vs non-technical (huge ICP-fit signal).

### Phase 2 — Search validation (mandatory gate)

```
linkedin validate-query "<query>" --limit 10 --campaign <slug>
```

Grades each result on geography + role + noise exclusion. Exits 0 if keepers ≥ 6/10. All pass → Phase 3. All fail → iterate (add a city, add a stage qualifier, drop investor-vocabulary phrases). Mixed → use only the queries that pass.

### Phase 3 — Generate the brief

Write `campaigns/<slug>.md` using `campaigns/_agentic_labs.md` as canon. Optional frontmatter overrides: `icp_role_required`, `icp_role_excluded`, `icp_geo_required`. Then `campaign sync` and show the rendered brief.

### Phase 4 — First import is small

**Import 5-10 first**, eyeball with `pipeline --status targeted`, scale only after a couple reach `connected`.

## CLI reference

All commands are `python -m linkedin_agent <subcommand>` (or `linkedin <subcommand>` with the venv active). On Windows: `.venv\Scripts\python.exe -m linkedin_agent <subcommand>`.

### Day-to-day
| Command | Purpose |
|---|---|
| `init` | Create the SQLite DB and required directories |
| `status` | Caps, window, pipeline by stage, replies, due follow-ups, **unconfirmed sends** |
| `daily` | Run the full cron cycle once |
| `providers` | Capability routing table — owner, evidence level, disarmed writes |
| `caps` | Usage vs. daily caps |
| `poll` | Fetch inbound replies only |
| `check-accepts` | Detect accepted invites (the `daily` step, runnable alone) |
| `followup` | Draft due DM2/DM3 only |
| `pipeline [--status STATUS]` | List prospects |
| `jobs [--collect]` | Async provider work in flight |
| `healthcheck` | Did the cron actually fire? |

### Campaigns
`campaign create|sync|list|show|archive|assign`

### Discovery + manual outreach
| Command | Purpose |
|---|---|
| `validate-query "<q>" --limit N --campaign <slug>` | Grade a query before committing to it |
| `search "<query>" --campaign <slug> --limit N` | Search and import |
| `search-posts "<keywords>" --campaign <slug>` | Search post *content*, import authors. **Currently unroutable** — no PhantomBuster equivalent |
| `enrich --prospect-id N` / `--all-stale` | Refresh a stored profile |
| `posts <pid>` / `react <pid>` / `connect <pid> --note "..."` / `dm <pid> "<body>"` | Manual actions |

### Telegram + send window
`bot-run`, `telegram-test`, `telegram-push-draft <id>`, `_debug-enqueue <pid> <kind> "<body>"`, `send-approved [--force]`

## Safety rules

1. **Never bypass the CLI.** Adapter methods don't enforce rate limits.
2. **Check `caps` or `status` before bulk actions.** Hard caps (30 reactions / 20 connections / 10 DMs / 50 searches per 24h) raise rather than silently trimming.
3. **Don't push unapproved drafts.** The Telegram approval flow is the human-in-the-loop quality check.
4. **Never retry a write on another provider.** A write whose outcome is unknown is how you double-invite someone.
5. **Stop, don't retry.** Captcha screens, "unusual activity" warnings, unexpected 4xx → tell the user. Retrying through an anti-bot signal is how an account gets restricted.

## State

- **DB**: `data/outreach.db` (SQLite). Schema is created and migrated idempotently by `init_db()`; add columns via `_PROSPECT_COLUMNS` and friends, never by hand.
- **Campaign briefs**: `campaigns/*.md` — source of truth.
- **Action log**: `actions` table — every API call, drafter result, status transition. Notable kinds: `send_unconfirmed`, `reply_stale`, `accept_detected`, `daily_completed`.
- **Signals / evidence / positions / research_jobs**: first-class tables. `signals` and `evidence` are append-only by design — a changed world produces a new signal, so the trail of what we believed when we sent a message stays intact.

## Tests

```bash
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 COLUMNS=200 .venv/Scripts/python.exe -m pytest -q
```

894 passed, 7 deselected. The offline suite is hermetic by construction: `conftest.py` strips `PHANTOMBUSTER_*` / `LINKEDIN_PRIMARY_*` from the environment so a developer's `.env` cannot make the suite hit the network. Live tests are opt-in via markers.

## Docs

- `docs/RUNBOOK.md` — operator manual; what each command does and what the system refuses to do.
- `docs/PHANTOMBUSTER_MIGRATION.md` — why Unipile was removed.
- `docs/TRANSITION_SIGNAL_PLAN.html` — the architecture review behind this phase.
- `docs/PLAN.md`, `docs/PHASE0_STATUS.md` — historical; predate the PhantomBuster migration and describe a Unipile-backed system. Read them as history, not as current state.

## Deployment — dedicated Mac (always-on host)

1. Clone to `~/Work/Linkedin_outreach`, run `setup.sh` (venv, deps, Chromium, DB, `.env`).
2. **Install Claude Code on the host** and run `claude /login` — the drafter shells out to `claude -p` and needs the host authenticated. Verify with `python -c "import shutil; print(shutil.which('claude'))"`.
3. Install the bot daemon: `./scripts/install_launchd.sh` (starts on login, restarts within 10s on crash, logs to `data/bot-daemon.{out,err}.log`).
4. Install the cron: `0 9-16 * * 1-5 /Users/<you>/Work/Linkedin_outreach/scripts/daily_outreach.sh >> /tmp/linkedin_outreach.log 2>&1`
5. **Prevent sleep during work hours** — Lock Screen → "Prevent automatic sleeping when display is off", or `caffeinate -d &`.
6. Verify: `launchctl list | grep linkedin-bot`, `tail -f data/bot-daemon.out.log`, and a Telegram tap.

Daemon lifecycle: `launchctl unload|load ~/Library/LaunchAgents/com.cortivo.linkedin-bot.plist` (re-running `install_launchd.sh` is idempotent).

Migrating between Macs: copy `data/outreach.db` and `.env`; briefs are in git. Re-run `install_launchd.sh`.
