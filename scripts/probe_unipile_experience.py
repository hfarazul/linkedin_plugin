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

# Section request variants.
#
# The parameter name matters and is easy to get wrong: Unipile's marketing
# pages say `with_sections=linkedin_experience`, but the API reference for
# GET /users/{identifier} says `linkedin_sections=experience`. An unknown
# query parameter is silently ignored rather than rejected, so the wrong
# spelling produces a response identical to the baseline -- which reads as
# "this API cannot return experience" when it actually means "you asked
# wrongly". `with_sections` is kept below purely as a control: seeing it match
# baseline while `linkedin_sections` differs is what proves the distinction.
SECTION_VARIANTS = {
    "baseline":          {},
    "with_sections":     {"with_sections": ["linkedin_experience"]},
    "linkedin_sections": {"linkedin_sections": ["experience"]},
    "sections_all":      {"linkedin_sections": ["*"]},
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
    # v1 lives under /api/v1; v2 is served from /v2 with no /api prefix.
    prefix = "/api/v1" if api_version == "v1" else f"/{api_version}"
    return httpx.Client(
        base_url=f"https://{cfg.unipile_dsn}{prefix}",
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


def _scrub(text: str, cfg) -> str:
    """Remove credential values from anything we print or write to disk.

    Unipile authenticates with a header, so responses should never echo the
    key — but "should never" is not a guarantee worth betting a public repo on,
    and error bodies sometimes reflect request context back.
    """
    for secret in (cfg.unipile_api_key, cfg.unipile_account_id):
        if secret and len(secret) >= 8:
            text = text.replace(secret, "<redacted>")
    return text


def _fetch(client: httpx.Client, cfg, identifier: str, sections: dict,
           api_version: str = "v1") -> tuple[int | str, dict | str]:
    """Returns (status, payload). Status is an int for an HTTP response, or a
    short string tag for a transport failure so the caller can explain it
    rather than dying with a traceback.

    v1 and v2 place account_id differently: v1 takes it as a query parameter,
    v2 makes it a path segment (/v2/{account_id}/users/{id})."""
    params: dict = dict(sections)
    if api_version == "v2":
        path = f"/{cfg.unipile_account_id}/users/{identifier}"
    else:
        path = f"/users/{identifier}"
        params["account_id"] = cfg.unipile_account_id
    try:
        r = client.get(path, params=params)
    except httpx.ConnectError as e:
        return "CONNECT_ERROR", _scrub(str(e), cfg)[:200]
    except httpx.TimeoutException:
        return "TIMEOUT", "request timed out after 30s"
    except httpx.HTTPError as e:
        return "HTTP_ERROR", f"{type(e).__name__}: {_scrub(str(e), cfg)[:200]}"
    if r.status_code != 200:
        return r.status_code, _scrub(r.text, cfg)[:400]
    try:
        return 200, r.json()
    except Exception:
        return 200, _scrub(r.text, cfg)[:400]


def _explain_failure(status, detail: str) -> list[str]:
    """Turn a failed fetch into something actionable rather than a bare code."""
    if status == "CONNECT_ERROR":
        return ["  Could not reach the host. Check UNIPILE_DSN is your real DSN",
                "  (from the Unipile dashboard), not the .env.example placeholder."]
    if status == "TIMEOUT":
        return ["  Timed out. Network issue, or the DSN points somewhere unreachable."]
    if status == 401 or status == 403:
        return ["  Rejected. UNIPILE_API_KEY is wrong, expired, or not valid for this DSN."]
    if status == 404:
        return ["  Not found. Either the identifier is wrong, or UNIPILE_ACCOUNT_ID",
                "  does not match a connected LinkedIn account."]
    if status == 429:
        return ["  Rate limited. Wait and retry; see the throttle notes in",
                "  .claude/skills/news-signal-outreach.md."]
    return [f"  Unexpected status {status}: {detail[:160]}"]


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
    ap.add_argument("--check-config", action="store_true",
                    help="Validate credentials and exit without calling the API.")
    args = ap.parse_args()

    cfg = load_config()

    # --- preflight: config must be real before anything else is worth trying --
    problems: list[str] = []
    for name, value in (("UNIPILE_API_KEY", cfg.unipile_api_key),
                        ("UNIPILE_ACCOUNT_ID", cfg.unipile_account_id),
                        ("UNIPILE_DSN", cfg.unipile_dsn)):
        if not value:
            problems.append(f"{name} is empty")
    # .env.example ships a placeholder DSN; an unedited copy fails confusingly
    # (DNS error) rather than obviously, so name it explicitly.
    if cfg.unipile_dsn and "13xxx" in cfg.unipile_dsn:
        problems.append(f"UNIPILE_DSN is still the .env.example placeholder ({cfg.unipile_dsn})")

    print("Unipile config:")
    print(f"  UNIPILE_API_KEY     {'set (' + str(len(cfg.unipile_api_key)) + ' chars)' if cfg.unipile_api_key else 'EMPTY'}")
    print(f"  UNIPILE_ACCOUNT_ID  {'set (' + str(len(cfg.unipile_account_id)) + ' chars)' if cfg.unipile_account_id else 'EMPTY'}")
    print(f"  UNIPILE_DSN         {cfg.unipile_dsn or 'EMPTY'}")

    if problems:
        print("\nBLOCKED - configuration incomplete:")
        for p in problems:
            print(f"  - {p}")
        print("\nP0-1 cannot be answered without live Unipile access.")
        print("Fill these in .env (values from the Unipile dashboard), then re-run.")
        print("Do not paste credentials into chat - .env is gitignored.")
        return 2

    if args.check_config:
        print("\nConfig looks complete. Re-run without --check-config and with at")
        print("least one identifier to probe actual profile responses.")
        return 0

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
                status, payload = _fetch(client, cfg, ident, sections, args.api_version)
                if status != 200:
                    print(f"  {variant:11s} FAILED ({status})")
                    for line in _explain_failure(status, str(payload)):
                        print(line)
                    continue
                bodies[variant] = payload
                path = OUT_DIR / f"profile_{n}_{variant}.json"
                # Scrub before writing: these files are diagnostic output the
                # operator may well paste somewhere.
                body_text = _scrub(json.dumps(payload, indent=2, ensure_ascii=False), cfg)
                path.write_text(body_text, encoding="utf-8")
                if isinstance(payload, dict):
                    print(f"  {variant:11s} HTTP 200, {len(payload)} top-level keys -> {path.name}")

            if "baseline" not in bodies:
                verdicts.append({"id": short, "verdict": "FETCH_FAILED"})
                continue

            # Which parameter spelling actually changes the response? Seeing
            # with_sections match baseline while linkedin_sections differs is
            # the proof that the spelling, not the API, was the limitation.
            base_json = json.dumps(bodies["baseline"], sort_keys=True)
            for variant in ("with_sections", "linkedin_sections", "sections_all"):
                if variant not in bodies:
                    continue
                differs = json.dumps(bodies[variant], sort_keys=True) != base_json
                print(f"  {variant:18s} changes response? {'YES' if differs else 'no'}")

            best_variant, best_positions, best_path = None, [], None
            for variant in ("sections_all", "linkedin_sections", "with_sections", "baseline"):
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

            # Date granularity decides whether the transition detectors can
            # work at all: "started within 180 days" is meaningless if every
            # start date is 1/1/YYYY. Print the raw values so the precision is
            # visible rather than assumed.
            start_key, end_key = report["start_date"], report["end_date"]
            if start_key:
                samples = [(p.get(start_key), p.get(end_key) if end_key else None)
                           for p in best_positions[:5]]
                print(f"    date samples (start, end): {samples}")
                day_month_precision = sum(
                    1 for s, _ in samples
                    if isinstance(s, str) and not s.startswith("1/1/")
                )
                print(f"    positions with finer-than-year precision: "
                      f"{day_month_precision}/{len(samples)}")

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

    total = len(verdicts)
    usable = sum(1 for v in verdicts if v["verdict"] == "USABLE")
    # A request that never reached the API says nothing about what the API can
    # return. Counting transport failures as "no dated history" would
    # manufacture evidence for PhantomBuster out of a bad DSN, so answered and
    # unanswered profiles are kept strictly separate.
    fetch_failed = sum(1 for v in verdicts if v["verdict"] == "FETCH_FAILED")
    answered = total - fetch_failed

    print(f"\n  {usable}/{answered} answered profiles returned positions with a start date")
    if fetch_failed:
        print(f"  {fetch_failed}/{total} profiles could not be fetched at all")

    (OUT_DIR / "verdict.json").write_text(
        _scrub(json.dumps(verdicts, indent=2), cfg), encoding="utf-8")

    if answered == 0:
        print("\n  VERDICT: UNVERIFIED - LIVE UNIPILE RESPONSE REQUIRED")
        print("  Every request failed before reaching the API, so this run is")
        print("  evidence about connectivity, NOT about Unipile's capability.")
        print("  Fix the errors above and re-run. Do not treat this as a reason")
        print("  to build PhantomBuster.")
        print(f"\n  Raw bodies + verdict: {OUT_DIR}  (gitignored -- personal data)")
        return 1

    if usable == answered:
        print("\n  VERDICT: Unipile is sufficient. Skip PhantomBuster for the MVP.")
        print("  Next: P1-2 (positions table + mapper) using the field map above.")
    elif usable:
        print("\n  VERDICT: PARTIAL. Usable for some profiles only.")
        print("  Decide whether coverage is good enough before considering Phase 2.")
    else:
        print("\n  VERDICT: Unipile returned profiles but NO dated history")
        print("  on this account/API version.")
        print("  Before building PhantomBuster, retry with --api-version v2;")
        print("  a v1/v2 difference is far cheaper to resolve than a new vendor.")

    print(f"\n  Raw bodies + verdict: {OUT_DIR}  (gitignored -- personal data)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
