#!/usr/bin/env python
"""P0-1 probe — can Unipile give us dated LinkedIn work history?

This is the gate for the whole transition-signal architecture. If the profile
endpoint already returns positions with start dates, transition detection is a
small extension of `enrichment.py` and PhantomBuster is unnecessary. If it does
not, Phase 2 (a separate async ProfileSource) becomes required.

What it does, per profile:
  1. GET /users/{id}                          -- the call enrichment.py makes today
  2. GET /users/{id}?with_sections=...        -- same call asking for experience
  3. Compares the two. If they are byte-identical, the parameter is being
     ignored on this API version, which is itself the answer.

It then reports, per profile, whether we can see the six fields the
`positions` table needs: company, title, start date, end date, is-current,
company URL.

Privacy: raw responses contain real people's profile data and this repository
is public, so bodies are written to data/probe/ (gitignored). Only a
values-free schema summary is meant for docs/. Nothing here prints or stores
the API key -- Unipile authenticates with a header, not a URL.

Usage (run on the host that has real .env credentials):

    python scripts/probe_unipile_experience.py --from-db 5
    python scripts/probe_unipile_experience.py ACoAAA... williamhgates
    python scripts/probe_unipile_experience.py --from-db 5 --api-version v2
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from linkedin_agent.config import load as load_config  # noqa: E402

OUT_DIR = ROOT / "data" / "probe"

# Section names from Unipile's v2 documentation. v1 may ignore these entirely
# -- determining that is one of this probe's jobs.
SECTION_VARIANTS = {
    "baseline":   None,
    "experience": ["linkedin_experience"],
    "full":       ["linkedin_*_preview", "linkedin_experience", "linkedin_skills"],
}

# The six fields the `positions` table needs (plan section 5). Each entry is a
# list of plausible key spellings; the probe reports which, if any, is present.
REQUIRED_FIELDS = {
    "company":     ["company", "company_name", "companyName", "organisation", "organization"],
    "title":       ["title", "position", "role", "job_title", "jobTitle"],
    "start_date":  ["start", "start_date", "startDate", "starts_at", "date_start", "from"],
    "end_date":    ["end", "end_date", "endDate", "ends_at", "date_end", "to"],
    "is_current":  ["current", "is_current", "isCurrent", "present"],
    "company_url": ["company_url", "companyUrl", "company_link", "profile_url", "company_id"],
}


def _client(cfg, api_version: str) -> httpx.Client:
    return httpx.Client(
        base_url=f"https://{cfg.unipile_dsn}/api/{api_version}",
        headers={"X-API-KEY": cfg.unipile_api_key, "accept": "application/json"},
        timeout=30.0,
    )


def _identifiers_from_db(limit: int) -> list[str]:
    from linkedin_agent import db
    if not db.DB_PATH.exists():
        print(f"! no DB at {db.DB_PATH} -- pass identifiers on the command line instead")
        return []
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT provider_id FROM prospects "
            "WHERE provider_id IS NOT NULL AND provider_id != '' LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [r["provider_id"] for r in rows]


def _fetch(client: httpx.Client, cfg, identifier: str, sections) -> tuple[int, dict | str]:
    params: dict = {"account_id": cfg.unipile_account_id}
    if sections:
        params["with_sections"] = sections
    r = client.get(f"/users/{identifier}", params=params)
    if r.status_code != 200:
        return r.status_code, r.text[:400]
    try:
        return 200, r.json()
    except Exception:
        return 200, r.text[:400]


def _find_positions(payload: dict) -> tuple[str | None, list]:
    """Locate the list of positions in the response, wherever it lives.
    Returns (json_path, positions) or (None, []) if nothing looks like one."""
    candidate_keys = (
        "work_experience", "experience", "positions", "linkedin_experience",
        "employment", "jobs", "work_history",
    )
    for key in candidate_keys:
        value = payload.get(key)
        if isinstance(value, list) and value:
            return key, value
        # Sometimes wrapped: {"experience": {"items": [...]}}
        if isinstance(value, dict):
            for inner in ("items", "elements", "values", "data"):
                if isinstance(value.get(inner), list) and value[inner]:
                    return f"{key}.{inner}", value[inner]
    # Last resort: any top-level list whose first item mentions a company.
    for key, value in payload.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            keys = {k.lower() for k in value[0]}
            if keys & {"company", "company_name", "companyname"}:
                return key, value
    return None, []


def _field_report(positions: list) -> dict[str, str | None]:
    """For each required field, which key spelling actually appears."""
    seen: dict[str, str | None] = {}
    sample_keys: set[str] = set()
    for p in positions:
        if isinstance(p, dict):
            sample_keys |= set(p.keys())
    for field, spellings in REQUIRED_FIELDS.items():
        seen[field] = next((s for s in spellings if s in sample_keys), None)
    return seen


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("identifiers", nargs="*",
                    help="provider ids (ACo...) or public identifiers")
    ap.add_argument("--from-db", type=int, default=0,
                    help="pull N provider_ids from the prospects table")
    ap.add_argument("--api-version", default="v1",
                    help="Unipile API version path segment (default: v1, what the code uses)")
    args = ap.parse_args()

    cfg = load_config()
    missing = [n for n, v in (("UNIPILE_API_KEY", cfg.unipile_api_key),
                              ("UNIPILE_ACCOUNT_ID", cfg.unipile_account_id),
                              ("UNIPILE_DSN", cfg.unipile_dsn)) if not v]
    if missing:
        print(f"BLOCKED: missing credentials in .env: {', '.join(missing)}")
        print("Run this on the host that has real Unipile credentials.")
        return 2

    identifiers = list(args.identifiers)
    if args.from_db:
        identifiers += _identifiers_from_db(args.from_db)
    if not identifiers:
        print("No identifiers. Pass some, or use --from-db N.")
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    client = _client(cfg, args.api_version)
    verdicts: list[dict] = []

    try:
        for n, ident in enumerate(identifiers, 1):
            short = ident[:18]
            print(f"\n=== [{n}/{len(identifiers)}] {short}... (api {args.api_version}) ===")
            bodies: dict[str, dict | str] = {}

            for variant, sections in SECTION_VARIANTS.items():
                status, payload = _fetch(client, cfg, ident, sections)
                if status != 200:
                    print(f"  {variant:11s} HTTP {status}: {str(payload)[:120]}")
                    continue
                bodies[variant] = payload
                path = OUT_DIR / f"profile_{n}_{variant}.json"
                path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                                encoding="utf-8")
                if isinstance(payload, dict):
                    print(f"  {variant:11s} HTTP 200, {len(payload)} top-level keys -> {path.name}")

            if "baseline" not in bodies or "experience" not in bodies:
                verdicts.append({"id": short, "verdict": "FETCH_FAILED"})
                continue

            # Does with_sections change anything at all on this API version?
            same = json.dumps(bodies["baseline"], sort_keys=True) == \
                   json.dumps(bodies["experience"], sort_keys=True)
            print(f"  with_sections honored? {'NO - responses identical' if same else 'YES - response differs'}")

            best_variant, best_positions, best_path = None, [], None
            for variant in ("experience", "full", "baseline"):
                body = bodies.get(variant)
                if not isinstance(body, dict):
                    continue
                json_path, positions = _find_positions(body)
                if positions:
                    best_variant, best_positions, best_path = variant, positions, json_path
                    break

            if not best_positions:
                print("  positions: NONE FOUND in any variant")
                verdicts.append({"id": short, "verdict": "NO_POSITIONS",
                                 "sections_honored": not same})
                continue

            print(f"  positions: {len(best_positions)} found "
                  f"(variant={best_variant}, json path='{best_path}')")
            report = _field_report(best_positions)
            for field, key in report.items():
                mark = "OK  " if key else "MISS"
                print(f"    [{mark}] {field:12s} -> {key or '(not present)'}")
            print(f"    position keys seen: {sorted(best_positions[0].keys())}")

            has_dates = bool(report["start_date"])
            verdicts.append({
                "id": short,
                "verdict": "USABLE" if has_dates else "NO_DATES",
                "variant": best_variant,
                "json_path": best_path,
                "count": len(best_positions),
                "fields": report,
            })
    finally:
        client.close()

    # ------------------------------------------------------------- summary
    print("\n" + "=" * 66)
    print("P0-1 SUMMARY")
    print("=" * 66)
    for v in verdicts:
        print(f"  {v['id']:20s} {v['verdict']}")

    usable = sum(1 for v in verdicts if v["verdict"] == "USABLE")
    total = len(verdicts)
    print(f"\n  {usable}/{total} profiles returned positions with a start date")
    if total and usable == total:
        print("\n  VERDICT: Unipile is sufficient. Skip PhantomBuster for the MVP.")
        print("  Next: P1-2 (positions table + mapper) using the field map above.")
    elif usable:
        print("\n  VERDICT: PARTIAL. Usable for some profiles only.")
        print("  Decide whether coverage is good enough before considering Phase 2.")
    else:
        print("\n  VERDICT: Unipile does NOT provide dated history on this account/version.")
        print("  Before building PhantomBuster, retry with --api-version v2;")
        print("  a v1/v2 difference is far cheaper to resolve than a new vendor.")

    (OUT_DIR / "verdict.json").write_text(json.dumps(verdicts, indent=2), encoding="utf-8")
    print(f"\n  Raw bodies + verdict: {OUT_DIR}  (gitignored -- contains personal data)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
