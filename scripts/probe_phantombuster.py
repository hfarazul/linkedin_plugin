#!/usr/bin/env python
"""PB-0 probe — what does PhantomBuster actually return through its API?

Companion to probe_unipile_experience.py, and written for the same reason:
the Unipile probe initially reported "no work history" because it used a
parameter name taken from a marketing page. Docs are not evidence. This script
reads the real output of real runs on the real account.

It answers the four questions the migration decision depends on:

  Q1  Can we retrieve results programmatically at all, on this plan?
      (JSON export is restricted on some tiers — if this fails nothing else
      matters.)
  Q2  Profile Scraper: does it return work history with start AND end dates,
      and at what granularity? Year-only dates would break the transition
      detectors, which need a recency window.
  Q3  Inbox Scraper: is there a stable per-message id, and is full thread
      history available? Our messages.external_id UNIQUE index is how inbound
      polling stays idempotent.
  Q4  Does the API payload carry more than the CSV export does?

Privacy: output contains real profile and message data, and this repository is
public. Bodies are written to data/probe/ (gitignored) and the API key is
scrubbed from everything printed or saved.

Usage:
    python scripts/probe_phantombuster.py              # inspect last run of every agent
    python scripts/probe_phantombuster.py --agent-id 1327575646342095
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from linkedin_agent.config import ROOT as _CFG_ROOT  # noqa: E402,F401  (loads .env)
import os  # noqa: E402

OUT_DIR = ROOT / "data" / "probe" / "phantombuster"
BASE = "https://api.phantombuster.com/api/v2"

# Field names that would satisfy each requirement. We report which spelling is
# actually present rather than assuming one.
MESSAGE_ID_KEYS = ("messageId", "message_id", "id", "eventUrn", "messageUrn",
                   "entityUrn", "urn", "backendUrn")
EXPERIENCE_KEYS = ("experience", "workExperience", "work_experience", "positions",
                   "jobs", "experiences", "companyHistory")
DATE_KEYS = ("start", "startDate", "dateRange", "from", "starts_at", "startedOn")


def _scrub(text: str, key: str | None) -> str:
    if key and len(key) >= 8:
        text = text.replace(key, "<redacted>")
    return text


def _client(key: str) -> httpx.Client:
    return httpx.Client(base_url=BASE,
                        headers={"X-Phantombuster-Key-1": key,
                                 "accept": "application/json"},
                        timeout=60.0)


def _get(c: httpx.Client, path: str, **params):
    try:
        r = c.get(path, params=params)
    except httpx.HTTPError as e:
        return None, f"{type(e).__name__}: {e}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:200]}"
    try:
        return r.json(), None
    except Exception:
        return r.text, None


def _walk_keys(obj, prefix="", depth=0, out=None):
    """Collect dotted key paths so nested experience blocks are visible."""
    if out is None:
        out = set()
    if depth > 4:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            out.add(p)
            _walk_keys(v, p, depth + 1, out)
    elif isinstance(obj, list) and obj:
        _walk_keys(obj[0], f"{prefix}[]", depth + 1, out)
    return out


def _find_first(rows: list, candidates: tuple) -> str | None:
    keys = set()
    for r in rows[:20]:
        if isinstance(r, dict):
            keys |= set(r.keys())
    return next((c for c in candidates if c in keys), None)


def _date_granularity(values: list) -> str:
    """Year-only dates make a 180-day recency window meaningless."""
    seen = [v for v in values if isinstance(v, str) and v.strip()]
    if not seen:
        return "no date values found"
    finer = [v for v in seen if not re.match(r"^(1/1/|Jan(uary)?\s+)?\d{4}$", v.strip())
             and not re.match(r"^1/1/\d{4}$", v.strip())]
    return f"{len(finer)}/{len(seen)} finer than year-only; samples={seen[:5]}"


def analyse(label: str, rows: list) -> dict:
    """Report what this payload can and cannot support."""
    findings: dict = {"agent": label, "row_count": len(rows)}
    if not rows:
        findings["verdict"] = "EMPTY"
        return findings

    all_paths = set()
    for r in rows[:20]:
        all_paths |= _walk_keys(r)
    findings["key_paths"] = sorted(all_paths)[:60]

    # Q3 — per-message id
    msg_id = _find_first(rows, MESSAGE_ID_KEYS)
    findings["message_id_field"] = msg_id

    # Q2 — experience block
    exp_key = _find_first(rows, EXPERIENCE_KEYS)
    findings["experience_field"] = exp_key
    if exp_key:
        positions = []
        for r in rows[:20]:
            v = r.get(exp_key)
            if isinstance(v, list):
                positions.extend(v)
        findings["position_count"] = len(positions)
        if positions and isinstance(positions[0], dict):
            findings["position_keys"] = sorted(positions[0].keys())
            dk = _find_first(positions, DATE_KEYS)
            findings["position_date_field"] = dk
            if dk:
                findings["date_granularity"] = _date_granularity(
                    [p.get(dk) for p in positions])

    # Thread-history shape: is there a nested list of messages per row?
    nested_lists = {p for p in all_paths if p.endswith("[]")}
    findings["nested_lists"] = sorted(nested_lists)[:15]
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent-id", default=None, help="Probe just this agent.")
    args = ap.parse_args()

    key = os.getenv("PHANTOMBUSTER_API_KEY")
    if not key:
        print("BLOCKED: PHANTOMBUSTER_API_KEY not set in .env")
        return 2
    print(f"PhantomBuster key: set ({len(key)} chars)\n")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    c = _client(key)
    summaries = []
    try:
        agents, err = _get(c, "/agents/fetch-all")
        if err:
            print(f"BLOCKED: cannot list agents -- {err}")
            return 2
        if args.agent_id:
            agents = [a for a in agents if str(a.get("id")) == str(args.agent_id)]

        for a in agents:
            aid, name = a.get("id"), a.get("name")
            print("=" * 70)
            print(f"AGENT {aid}  {name!r}")
            print("=" * 70)

            detail, err = _get(c, "/agents/fetch", id=aid)
            if err:
                print(f"  agents/fetch failed: {err}")
                continue
            org_s3 = detail.get("orgS3Folder")
            s3 = detail.get("s3Folder")
            print(f"  orgS3Folder={'set' if org_s3 else 'MISSING'}  s3Folder={'set' if s3 else 'MISSING'}")

            rows: list = []
            source = None

            # --- Q1/Q4: the S3 result file, which is the full dataset --------
            if org_s3 and s3:
                url = f"https://phantombuster.s3.amazonaws.com/{org_s3}/{s3}/result.json"
                try:
                    r = httpx.get(url, timeout=60.0, follow_redirects=True)
                    print(f"  S3 result.json -> HTTP {r.status_code}")
                    if r.status_code == 200:
                        data = r.json()
                        rows = data if isinstance(data, list) else [data]
                        source = "s3:result.json"
                        (OUT_DIR / f"agent_{aid}_result.json").write_text(
                            _scrub(json.dumps(data, indent=2, ensure_ascii=False), key),
                            encoding="utf-8")
                except Exception as e:
                    print(f"  S3 fetch error: {type(e).__name__}: {str(e)[:120]}")

            # --- fallback: container output ----------------------------------
            if not rows:
                conts, err = _get(c, "/containers/fetch-all", agentId=aid)
                items = (conts or {}).get("containers", conts) or []
                if isinstance(items, list) and items:
                    cid = items[0].get("id")
                    print(f"  latest container: {cid}")
                    out, err = _get(c, "/containers/fetch-result-object", id=cid)
                    if out and out.get("resultObject"):
                        try:
                            data = json.loads(out["resultObject"])
                            rows = data if isinstance(data, list) else [data]
                            source = "containers/fetch-result-object"
                            (OUT_DIR / f"agent_{aid}_resultobject.json").write_text(
                                _scrub(json.dumps(data, indent=2, ensure_ascii=False), key),
                                encoding="utf-8")
                        except Exception as e:
                            print(f"  resultObject not JSON: {e}")
                    else:
                        print(f"  no resultObject ({err or 'empty'})")

            if not rows:
                print("  NO RESULT DATA RETRIEVED\n")
                summaries.append({"agent": name, "verdict": "NO_DATA"})
                continue

            print(f"  retrieved {len(rows)} rows via {source}")
            f = analyse(name or str(aid), rows)
            summaries.append(f)

            print(f"\n  -- field inventory ({len(f.get('key_paths', []))} paths) --")
            for p in f.get("key_paths", [])[:40]:
                print(f"     {p}")
            print(f"\n  message id field : {f.get('message_id_field') or 'NONE FOUND'}")
            print(f"  experience field : {f.get('experience_field') or 'NONE FOUND'}")
            if f.get("position_keys"):
                print(f"  position keys    : {f['position_keys']}")
                print(f"  date field       : {f.get('position_date_field')}")
                print(f"  date granularity : {f.get('date_granularity')}")
            if f.get("nested_lists"):
                print(f"  nested lists     : {f['nested_lists']}")
            print()
    finally:
        c.close()

    (OUT_DIR / "pb0_summary.json").write_text(
        json.dumps(summaries, indent=2, ensure_ascii=False), encoding="utf-8")
    print("=" * 70)
    print("PB-0 SUMMARY")
    print("=" * 70)
    for s in summaries:
        print(f"  {s.get('agent')!r:45s} rows={s.get('row_count', 0)} "
              f"msg_id={s.get('message_id_field') or '-'} "
              f"exp={s.get('experience_field') or '-'}")
    print(f"\n  Raw payloads: {OUT_DIR}  (gitignored -- real profile/message data)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
