# Manual Test Plan

**Purpose**: 107 automated tests prove the *code paths*. This plan verifies the *real-world behavior* — Telegram UI, LinkedIn UI, drafter quality, time-based scenarios, and failure modes that only show up against live services.

**Time investment**: ~2-3 hours for Phase A (today). Phases B and C unfold over 14 days as the pipeline matures.

**Output format**: each scenario has a ✅/❌ box at the bottom. Fill it in as you go; bugs found get filed as new tasks.

---

## 0. Pre-test setup

Before running any of these, verify the environment is sane.

```bash
cd ~/Work/Linkedin_outreach

# 0a. Tests pass offline
.venv/bin/pytest -q
# Expected: 107 passed

# 0b. Daemon is running under launchd
launchctl list | grep linkedin-bot
# Expected: a numeric PID in column 1

# 0c. .env has all six required values
grep -E '^(LINKEDIN_BACKEND|UNIPILE_API_KEY|UNIPILE_ACCOUNT_ID|UNIPILE_DSN|TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID)=' .env | wc -l
# Expected: 6

# 0d. Live Unipile read works
.venv/bin/pytest -m live -q tests/test_unipile_live.py::test_search_returns_real_profiles
# Expected: 1 passed

# 0e. Telegram round-trip works
.venv/bin/python -m linkedin_agent telegram-test
# Expected: test message arrives in your Telegram chat
```

**Stop here if anything in 0a-0e fails.** Investigate before continuing.

---

## Phase A — Run today (~2 hours)

Run these in order. Most can be done with self-prospect (prospect_id = 4, Haque Farazul) so no external people get touched.

### A1. CLI subcommand smoke (~10 min)

For each command, confirm it produces the expected outcome AND doesn't crash.

| Command | Expected | ✅/❌ |
|---|---|---|
| `linkedin status` | Renders dashboard with caps + pipeline counts | ☐ |
| `linkedin pipeline` | Lists current prospects | ☐ |
| `linkedin pipeline --status connection_sent` | Shows only `connection_sent` prospects | ☐ |
| `linkedin caps` | Renders today's usage table | ☐ |
| `linkedin campaign list` | Shows 4 campaigns (3 active + 1 archived) | ☐ |
| `linkedin campaign show solo-founders-us` | Renders the full brief markdown | ☐ |
| `linkedin posts 4` | Shows Haque's recent posts | ☐ |
| `linkedin poll` | Polls Unipile, reports 0 new (no replies yet) | ☐ |

### A2. Drafter end-to-end against real Unipile + claude -p (~15 min)

Verify the drafter produces clean, specific drafts referencing real prospect content.

For each prospect still in `targeted`/`reacted`, run the preview pass:

```bash
.venv/bin/python <<'EOF'
from linkedin_agent.adapters import get_adapter
from linkedin_agent.config import load as load_config
from linkedin_agent.drafter import draft, DrafterError
from linkedin_agent import db

cfg = load_config()
adapter = get_adapter(cfg)
try:
    for p in db.list_prospects(status="reacted"):
        url = f"https://www.linkedin.com/in/{p['provider_id']}" if p['provider_id'] else p['linkedin_url']
        posts = adapter.get_recent_posts(url, limit=3)
        recent = [{"text": x.text, "posted_at": x.posted_at} for x in posts]
        try:
            body = draft("connect_note", int(p['id']), recent_posts=recent)
            print(f"✓ #{p['id']} {p['full_name']} ({len(body)} chars): {body[:120]}…")
        except DrafterError as e:
            print(f"✗ #{p['id']} {p['full_name']}: {e}")
finally:
    adapter.close()
EOF
```

**Acceptance:**
- Each successful draft references a specific detail from the prospect's actual recent posts (read 2-3 to verify, don't just glance)
- INSUFFICIENT_CONTEXT outcomes look honest (no posts / no real ICP fit)
- Length is under the cap (300 chars for connect_note)

✅/❌: ☐

### A3. Telegram approval flow — all three buttons (~20 min)

Push 3 test drafts, exercise Approve / Edit / Reject in order.

**Setup** — use prospect_id=4 (yourself):
```bash
.venv/bin/python -m linkedin_agent _debug-enqueue 4 connect_note "TEST APPROVE — tap ✅ Approve. Should send connection-request-to-self and show '✓ Sent at HH:MM' in Telegram." --push
.venv/bin/python -m linkedin_agent _debug-enqueue 4 connect_note "TEST EDIT — tap ✏️ Edit, reply with new text, then approve. Verify fresh card with new text." --push
.venv/bin/python -m linkedin_agent _debug-enqueue 4 connect_note "TEST REJECT — tap ❌ Reject. Should show '❌ Rejected' with strike-through." --push
```

Note: you can't actually send a connection request to yourself on LinkedIn, but the flow should still process and mark the draft `sent`. The Unipile call may return an error you can verify.

Easier: use `LINKEDIN_FAKE_WINDOW=open` and `DRY_RUN=1` in the env so the daemon thinks-it-sent without actually calling Unipile:
```bash
# Stop the daemon, restart with DRY_RUN=1 in its env
launchctl unload ~/Library/LaunchAgents/com.cortivo.linkedin-bot.plist
# Edit ~/Library/LaunchAgents/com.cortivo.linkedin-bot.plist temporarily to add:
#   <key>EnvironmentVariables</key>
#   <dict>
#     <key>DRY_RUN</key>
#     <string>1</string>
#     <key>LINKEDIN_FAKE_WINDOW</key>
#     <string>open</string>
#   </dict>
launchctl load ~/Library/LaunchAgents/com.cortivo.linkedin-bot.plist
```

Or simply test against another prospect URL with their permission.

**Verification** (per button):

| Button | Expected | ✅/❌ |
|---|---|---|
| **Approve** | Telegram card shows "✓ Sent at HH:MM"; pending_drafts.status='sent' in DB; action logged | ☐ |
| **Edit** | Bot replies with force-reply prompt; you reply with new text; fresh approval card appears with your text | ☐ |
| **Reject** | Card shows "❌ Rejected" with strike-through; pending_drafts.status='rejected'; reject_reason='user_rejected' | ☐ |

### A4. Window enforcement (~10 min)

Test that approvals tapped outside the window queue rather than send.

```bash
# Outside business hours OR in dry-run with FAKE_WINDOW=closed:
LINKEDIN_FAKE_WINDOW=closed .venv/bin/python -m linkedin_agent _debug-enqueue 4 connect_note "WINDOW TEST — outside-hours approval should queue, not send." --push
```

Tap Approve in Telegram. **Expected**:
- Daemon's "Approved" callback fires
- Telegram card edits to "⏸️ Approved — queued for Mon 9:00 AM" (or similar)
- pending_drafts.status = `approved` (not `sent`)
- No Unipile send action logged

Then:
```bash
# Confirm the queued draft is sittable
sqlite3 -separator " | " data/outreach.db "SELECT id, status FROM pending_drafts WHERE status='approved' ORDER BY id DESC LIMIT 1"

# Force-send (simulating the next-day window opening)
.venv/bin/python -m linkedin_agent send-approved --force
```

✅/❌: ☐

### A5. Retry/Giveup on failure (~10 min)

Force a send failure to verify the retry/giveup buttons work.

```bash
# Temporarily break Unipile creds in .env (rename UNIPILE_API_KEY to UNIPILE_API_KEY_BACKUP)
cp .env .env.bak
sed -i.tmp 's/^UNIPILE_API_KEY=/UNIPILE_API_KEY_BAD=/' .env
# Restart daemon to pick up broken config
launchctl unload ~/Library/LaunchAgents/com.cortivo.linkedin-bot.plist
launchctl load ~/Library/LaunchAgents/com.cortivo.linkedin-bot.plist

# Enqueue a draft that will fail when you tap Approve
.venv/bin/python -m linkedin_agent _debug-enqueue 4 connect_note "RETRY TEST — Unipile broken, expect ⚠ Send failed card with Retry/Giveup buttons." --push
```

Tap **Approve** → expect "⚠ Send failed" with `🔄 Retry` and `❌ Give up` buttons.

| Action | Expected | ✅/❌ |
|---|---|---|
| Tap **🔄 Retry** | Daemon re-attempts; same failure surfaces again (still broken creds); buttons reappear | ☐ |
| Restore .env → tap **🔄 Retry** | Send succeeds, card shows "✓ Sent" | ☐ |
| (Alternate flow) Tap **❌ Give up** | Card shows "❌ Rejected"; pending_drafts.status='rejected' | ☐ |

**Cleanup**:
```bash
mv .env.bak .env && rm -f .env.tmp
launchctl unload ~/Library/LaunchAgents/com.cortivo.linkedin-bot.plist
launchctl load ~/Library/LaunchAgents/com.cortivo.linkedin-bot.plist
```

### A6. DRY_RUN end-to-end (~10 min)

Verify DRY_RUN=1 blocks every real LinkedIn write.

```bash
# Set DRY_RUN=1 temporarily
sed -i.tmp 's/^DRY_RUN=.*/DRY_RUN=1/' .env

# Run daily with a fresh targeted prospect — none currently exist, so first seed one:
.venv/bin/python -m linkedin_agent search "test query for dry run" --limit 1
# (Note caps — this will count against daily_max_searches)

# Force window open so react step doesn't skip
LINKEDIN_FAKE_WINDOW=open .venv/bin/python -m linkedin_agent daily
```

**Acceptance:**
- Console shows reactions / drafts in the summary
- The prospect's status DID advance (targeted → reacted) in the DB
- `actions` table has rows where `dry_run=1` for react and any drafts
- **Spot-check the prospect's LinkedIn post manually** — no LIKE from your account

Cleanup:
```bash
mv .env.tmp .env || sed -i 's/^DRY_RUN=.*/DRY_RUN=0/' .env
```

✅/❌: ☐

### A7. Daemon lifecycle (~5 min)

| Test | Expected | ✅/❌ |
|---|---|---|
| Kill daemon PID, wait 12s | New PID appears in `launchctl list \| grep linkedin-bot` | ☐ |
| `launchctl unload` then `launchctl load` | Clean stop, clean restart, log shows "bot daemon starting" | ☐ |
| Close Claude Code (this session) and reopen | Daemon still running with same PID | ☐ |
| Tail `data/bot-daemon.err.log` while idle | New getUpdates entry every ~25s | ☐ |

### A8. Polling + inbound reply (~15 min)

The trickiest path because it needs a real inbound message.

**Option 1 — natural test**: Wait for one of the 10 pending connections to either accept (you'd see status flip) or decline + send a "no thanks" reply.

**Option 2 — synthetic test**: Have a friend send you any LinkedIn message. Then:
```bash
# Insert the friend as a prospect (so poll's sender-match works)
.venv/bin/python <<'EOF'
from linkedin_agent import db
db.upsert_prospect(
    linkedin_url="https://www.linkedin.com/in/<their-slug>",
    full_name="Friend Test",
    provider_id="<their-ACo-provider-id>",   # find via Unipile search
)
EOF

# Run poll
.venv/bin/python -m linkedin_agent poll
```

**Expected:**
- Their message recorded in `messages` table with direction='inbound'
- Their prospect.status changes to 'replied'
- Telegram receives a "💬 New reply from..." notification
- Any pending follow-up drafts for them get cancelled (none yet for a new prospect, but check the logic anyway)

Cleanup: delete the friend prospect after test.

✅/❌: ☐

---

## Phase B — Time-dependent (over the next 14 days)

These can't be done today because they need elapsed time. Set calendar reminders.

### B1. Daily cron actually fires (Monday morning)

**Day 1, 9:01 AM local time**:
```bash
tail -20 /tmp/linkedin_outreach.log
```

| Check | Expected | ✅/❌ |
|---|---|---|
| Log shows "daily run complete" timestamp | ✅ | ☐ |
| Pipeline advanced for any prospects in active states | Check `linkedin status` | ☐ |
| Drafts pushed to Telegram for any reacted/connected prospects | Check phone | ☐ |
| Summary message posted to Telegram | At the end | ☐ |

### B2. Followup cadence (DM2 + DM3)

**Day +4 from a sent DM1**: that prospect should have a DM2 draft appear in Telegram.

**Day +11**: DM3 draft.

**Day +14 after DM3**: auto-ghost flips disposition to 'ghosted'.

Track in a calendar:
- DM1 sent → DM2 due: ☐ (date: ____)
- DM2 sent → DM3 due: ☐ (date: ____)
- DM3 sent → auto-ghost: ☐ (date: ____)

### B3. Connection acceptance flow

When any of the 10 pending connections accepts:

| Check | Expected | ✅/❌ |
|---|---|---|
| `linkedin status` shows them as `connected` (need manual flag OR poll picks it up) | ☐ |
| Next daily run drafts DM1 for them | ☐ |
| DM1 lands in Telegram approval flow | ☐ |

Note: LinkedIn doesn't push acceptance events to Unipile reliably. You may need to manually run:
```bash
.venv/bin/python -c "
from linkedin_agent import db
db.set_status(<prospect_id>, 'connected')
"
```

This is a documented gap — worth knowing.

### B4. Multi-day idempotency

After 3-4 daily runs:
- No duplicate drafts for the same prospect+kind
- No double reactions on the same post
- Caps reset cleanly each 24h window

✅/❌: ☐

---

## Phase C — Edge cases / chaos (any time)

Run these once during Phase A or after to harden confidence.

### C1. Drafter edge cases

| Scenario | How to trigger | Expected | ✅/❌ |
|---|---|---|---|
| No campaign attached | Add a prospect with `campaign_id=NULL` | INSUFFICIENT_CONTEXT (drafter has no positioning) | ☐ |
| No recent posts | Find a prospect with no public posts | INSUFFICIENT_CONTEXT | ☐ |
| Only job-listing posts | Find a prospect whose posts are recruiting only | INSUFFICIENT_CONTEXT | ☐ |
| Locked / private profile | Search returns a profile, then enrich fails | `enrich` returns False, prospect stays unenriched | ☐ |
| Drafter retries on oversize | Hard to force in production — covered by unit test `test_draft_retries_on_oversize_and_succeeds` | (use automated coverage) | ☐ |

### C2. Cap enforcement

| Scenario | Expected | ✅/❌ |
|---|---|---|
| Set `DAILY_MAX_REACTIONS=1` in `.env`, daily run with 2 targeted prospects | Only 1 reacts; `result.skipped_cap_hit` contains `react` | ☐ |
| Try `linkedin react <id>` when cap exceeded | `RateLimitExceeded` raised, no Unipile call | ☐ |
| Wait 24h, run again | Cap resets, second prospect now reacts | ☐ |

### C3. Special characters in Telegram

Force-enqueue drafts with characters that historically broke Markdown:

```bash
.venv/bin/python -m linkedin_agent _debug-enqueue 4 connect_note "Test with chars: _under_score_ *asterisks* [brackets] <html> & amp; \"quotes\" 'apostrophes' and emojis 🎯 in random spots" --push
```

| Check | Expected | ✅/❌ |
|---|---|---|
| Telegram message arrives | ✅ (not silently dropped) | ☐ |
| Characters render as written | All special chars visible, no broken formatting | ☐ |
| Approve button still works | ✅ | ☐ |

### C4. Error paths

| Scenario | How to test | Expected | ✅/❌ |
|---|---|---|---|
| Unipile API down | Set UNIPILE_DSN to `api999.unipile.com:99999` temporarily | Daily logs the error, continues to next step | ☐ |
| Telegram bot token revoked | Revoke + re-create in @BotFather | Drafts created in DB but Telegram push logs warning, doesn't crash | ☐ |
| .env missing TELEGRAM_BOT_TOKEN | Comment out the line | `linkedin daily --no-telegram` works; `daily` (no flag) logs "telegram disabled" warning | ☐ |
| Claude Code not authenticated on host | (Move to Mac Air without `claude /login` first) | Drafter raises DrafterError on first call | ☐ |
| Prospect URL malformed | Manually insert `linkedin_url='not-a-url'` | Adapter resolve fails gracefully, prospect skipped | ☐ |

### C5. State transition coverage

Walk a single test prospect through every status value:

```
targeted → reacted → connection_sent → connected → dm_sent → replied
```

Verify each transition shows correctly in `linkedin pipeline`.

For disposition values: manually set each via SQL and verify `linkedin status` doesn't break:
```sql
UPDATE prospects SET disposition='interested' WHERE id=<id>;  -- repeat for: not_fit, ghosted, won, lost, deferred
```

✅/❌: ☐

### C6. Concurrent runs

While `linkedin daily` is mid-execution, run it again in another terminal:

| Check | Expected | ✅/❌ |
|---|---|---|
| No DB-locked errors | SQLite handles this OK with WAL | ☐ |
| No duplicate reactions or drafts | Per-step idempotency holds | ☐ |
| Both runs complete cleanly | No crashes | ☐ |

### C7. Migration to Mac Air (when ready)

Per `CLAUDE.md` "Deployment — dedicated Mac" section. Validation:

| Step | Expected | ✅/❌ |
|---|---|---|
| `./setup.sh` runs to completion on fresh clone | ☐ |
| `./scripts/install_launchd.sh` loads daemon | ☐ |
| `claude /login` already done OR run on host | ☐ |
| Cron entry installed | `crontab -l` shows it | ☐ |
| `scp data/outreach.db` to Mac Air → pipeline state preserved | ☐ |
| First test message via Telegram from Mac Air daemon | ☐ |

---

## Reporting bugs found

For each failure during testing, file a task with:

- **Scenario reference** (e.g. "A3 — Edit button")
- **Steps to reproduce** (exact commands run)
- **Expected vs actual** 
- **Logs** (`data/bot-daemon.err.log` + relevant terminal output)

Then we triage and fix before moving forward.

---

## Sign-off

| Phase | Date completed | Issues found | Notes |
|---|---|---|---|
| Phase A | ____ | ____ | |
| Phase B | (rolling) | ____ | |
| Phase C | ____ | ____ | |

System is considered "manually verified" once Phase A is clean and at least one full cycle of Phase B (DM1 → reply OR DM1 → DM2 → DM3 → ghost) has been observed.
