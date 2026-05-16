# Manual Test Results

**Companion to**: `docs/MANUAL_TEST_PLAN.md`
**Purpose**: live tracker — fill in as you test. The plan doc is the spec; this doc is the record.

Use `[x]` for pass, `[F]` for fail (with notes), `[~]` for partial/blocked, `[-]` for skipped.

---

## Setup (Phase 0)

Run once before starting. Block on any failure.

| # | Check | Status | Notes |
|---|---|:---:|---|
| 0a | `pytest -q` returns 107 passed | [ ] | |
| 0b | `launchctl list \| grep linkedin-bot` shows PID | [ ] | |
| 0c | `.env` has all 6 required values | [ ] | |
| 0d | Live Unipile read test passes | [ ] | |
| 0e | `linkedin telegram-test` lands in chat | [ ] | |

**Started**: ____ · **Completed**: ____

---

## Phase A — Run today (~2 hours)

### A1. CLI subcommand smoke

| Command | Status | Notes |
|---|:---:|---|
| `linkedin status` | [ ] | |
| `linkedin pipeline` | [ ] | |
| `linkedin pipeline --status connection_sent` | [ ] | |
| `linkedin caps` | [ ] | |
| `linkedin campaign list` | [ ] | |
| `linkedin campaign show solo-founders-us` | [ ] | |
| `linkedin posts 4` | [ ] | |
| `linkedin poll` | [ ] | |

### A2. Drafter end-to-end

For each prospect tested:

| Prospect ID | Name | Draft generated? | Specific reference verified? | Length OK? | Notes |
|---|---|:---:|:---:|:---:|---|
| ___ | _________ | [ ] | [ ] | [ ] | |
| ___ | _________ | [ ] | [ ] | [ ] | |
| ___ | _________ | [ ] | [ ] | [ ] | |

### A3. Telegram approval flow

| Button | Status | Notes |
|---|:---:|---|
| Approve → "Sent" card + DB updates | [ ] | |
| Edit → force-reply → new text → fresh card | [ ] | |
| Reject → "Rejected" + strikethrough + DB | [ ] | |

### A4. Window enforcement

| Scenario | Status | Notes |
|---|:---:|---|
| Outside-window approval queues (status='approved') | [ ] | |
| Telegram card shows "queued for next window" | [ ] | |
| `send-approved --force` flushes the queue | [ ] | |

### A5. Retry / Giveup

| Action | Status | Notes |
|---|:---:|---|
| Broke Unipile creds — daemon reloaded | [ ] | |
| Failed send shows "⚠ Send failed" + 2 buttons | [ ] | |
| Tap Retry → re-fails same way | [ ] | |
| Restore creds + Retry → succeeds | [ ] | |
| Tap Give up → "Rejected" + DB updated | [ ] | |
| Cleanup: .env restored, daemon back to normal | [ ] | |

### A6. DRY_RUN

| Check | Status | Notes |
|---|:---:|---|
| DRY_RUN=1 set in .env | [ ] | |
| Daily run reports reactions/drafts | [ ] | |
| Prospect status advanced in DB | [ ] | |
| `actions` table rows show dry_run=1 | [ ] | |
| LinkedIn-side verified: no actual LIKE | [ ] | |
| DRY_RUN reset to 0 | [ ] | |

### A7. Daemon lifecycle

| Test | Status | Notes |
|---|:---:|---|
| Kill PID → auto-restart within 10s | [ ] | |
| `launchctl unload`/`load` clean cycle | [ ] | |
| Survives Claude Code session close | [ ] | |
| Log shows getUpdates every ~25s when idle | [ ] | |

### A8. Polling + inbound reply

| Check | Status | Notes |
|---|:---:|---|
| Reply arrived from test sender | [ ] | sender: _____ |
| `linkedin poll` recorded inbound message | [ ] | |
| Prospect status → 'replied' | [ ] | |
| Telegram notification fired | [ ] | |
| Any pending follow-up drafts cancelled | [ ] | |
| Cleanup: test prospect deleted | [ ] | |

**Phase A completed**: ____ · **Issues found**: ____

---

## Phase B — Time-dependent (over 14 days)

### B1. First Monday cron

| Check | Status | Notes |
|---|:---:|---|
| `/tmp/linkedin_outreach.log` shows daily run | [ ] | first fire: _______ |
| Drafts pushed to Telegram | [ ] | count: _____ |
| Summary message posted | [ ] | |
| `linkedin status` advances on the right prospects | [ ] | |

### B2. Follow-up cadence

Track in the table below as each prospect's DM1/2/3/ghost windows hit:

| Prospect | DM1 sent | DM2 due (D+4) | DM3 due (D+11) | Auto-ghost (D+14 post-DM3) |
|---|---|---|---|---|
| ______ | __/__ | __/__ [ ] | __/__ [ ] | __/__ [ ] |
| ______ | __/__ | __/__ [ ] | __/__ [ ] | __/__ [ ] |
| ______ | __/__ | __/__ [ ] | __/__ [ ] | __/__ [ ] |
| ______ | __/__ | __/__ [ ] | __/__ [ ] | __/__ [ ] |
| ______ | __/__ | __/__ [ ] | __/__ [ ] | __/__ [ ] |

### B3. Connection acceptance flow

| Prospect | Accepted on | Status updated? | DM1 drafted? | DM1 sent? |
|---|---|:---:|:---:|:---:|
| ______ | __/__ | [ ] | [ ] | [ ] |
| ______ | __/__ | [ ] | [ ] | [ ] |
| ______ | __/__ | [ ] | [ ] | [ ] |

### B4. Multi-day idempotency

| Check | Status | Notes |
|---|:---:|---|
| After 3 daily runs, no duplicate drafts in pending_drafts | [ ] | |
| Same post never reacted to twice | [ ] | |
| Caps reset cleanly each 24h window | [ ] | |

**Phase B completed**: ____ · **Issues found**: ____

---

## Phase C — Edge cases / chaos

### C1. Drafter edge cases

| Scenario | Status | Notes |
|---|:---:|---|
| No campaign → INSUFFICIENT_CONTEXT | [ ] | |
| No recent posts → INSUFFICIENT_CONTEXT | [ ] | |
| Only job-listing posts → INSUFFICIENT_CONTEXT | [ ] | |
| Locked/private profile → `enrich` returns False | [ ] | |

### C2. Cap enforcement

| Scenario | Status | Notes |
|---|:---:|---|
| `DAILY_MAX_REACTIONS=1` + 2 prospects → only 1 reacts | [ ] | |
| Cap exceeded → CLI react raises RateLimitExceeded | [ ] | |
| 24h later → cap resets, next action allowed | [ ] | |

### C3. Special characters

| Scenario | Status | Notes |
|---|:---:|---|
| Draft with underscores/asterisks/brackets renders fine | [ ] | |
| Approve button still works on that draft | [ ] | |
| Special chars visible as-typed (no broken formatting) | [ ] | |

### C4. Error paths

| Scenario | Status | Notes |
|---|:---:|---|
| Unipile DSN broken → daily logs error, continues | [ ] | |
| Telegram token revoked → daily warns, doesn't crash | [ ] | |
| Missing TELEGRAM_CHAT_ID → `--no-telegram` works | [ ] | |
| `claude` binary missing on host → DrafterError | [ ] | |
| Malformed prospect URL → skipped gracefully | [ ] | |

### C5. State transition coverage

| Status hop | Verified via natural flow OR manual SQL? | Notes |
|---|:---:|---|
| targeted → reacted | [ ] | |
| reacted → connection_sent | [ ] | |
| connection_sent → connected | [ ] | (manual usually) |
| connected → dm_sent | [ ] | |
| dm_sent → replied | [ ] | |
| any → skipped | [ ] | |
| disposition=interested | [ ] | |
| disposition=not_fit | [ ] | |
| disposition=ghosted | [ ] | |
| disposition=won | [ ] | |
| disposition=lost | [ ] | |
| disposition=deferred | [ ] | |

### C6. Concurrent runs

| Check | Status | Notes |
|---|:---:|---|
| `daily` × 2 simultaneously: no DB lock errors | [ ] | |
| No duplicate drafts produced | [ ] | |
| Both runs complete cleanly | [ ] | |

### C7. Mac Air migration (when ready)

| Step | Status | Notes |
|---|:---:|---|
| Fresh clone on Mac Air | [ ] | |
| `./setup.sh` completes | [ ] | |
| `./scripts/install_launchd.sh` loads daemon | [ ] | |
| `claude /login` confirmed working | [ ] | |
| Cron entry installed | [ ] | |
| `data/outreach.db` copied successfully | [ ] | |
| First test message via Telegram from Mac Air | [ ] | |

**Phase C completed**: ____ · **Issues found**: ____

---

## Bugs found

Append rows as bugs surface:

| ID | Scenario | What went wrong | Steps to repro | Status |
|---|---|---|---|---|
| 1 | _______ | _______ | _______ | open / fixed |
| 2 | _______ | _______ | _______ | open / fixed |

---

## Final sign-off

The system is "manually verified" once:
- Phase A ✅ clean (all rows checked, zero failures or all fixed)
- At least one full cycle of Phase B observed (one prospect went through DM1 → reply OR DM1 → DM2 → DM3 → ghost)
- Critical Phase C scenarios (C2, C3, C4) ✅

| | Date | By |
|---|---|---|
| Phase A | ____ | ____ |
| Phase B (one cycle) | ____ | ____ |
| Phase C (C2 + C3 + C4) | ____ | ____ |
| **Production-ready** | ____ | ____ |
