"""Tests for the P0-1 probe's response-parsing helpers.

The probe itself needs live credentials, but its two pure helpers encode our
assumptions about the shape of a Unipile profile response — and those helpers
become the basis of the P1-2 positions mapper. Testing them now means the
probe's output can be trusted when it finally runs against the real API, and
that P1-2 starts from verified parsing logic rather than a guess.

The fixtures below are hypothetical response shapes, NOT captured samples.
They exist to prove the locator handles the plausible variants; the real
shape is exactly what P0-1 must still determine.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# The probe lives in scripts/ (not an installed package), so load it by path.
_spec = importlib.util.spec_from_file_location(
    "probe_unipile_experience", ROOT / "scripts" / "probe_unipile_experience.py"
)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


# ----------------------------- _find_positions -------------------------------

@pytest.mark.unit
def test_finds_positions_under_work_experience() -> None:
    payload = {"name": "X", "work_experience": [{"company": "Acme", "title": "CEO"}]}
    path, positions = probe._find_positions(payload)
    assert path == "work_experience"
    assert len(positions) == 1


@pytest.mark.unit
def test_finds_positions_under_alternate_key() -> None:
    payload = {"experience": [{"company_name": "Acme", "position": "Founder"}]}
    path, positions = probe._find_positions(payload)
    assert path == "experience"
    assert len(positions) == 1


@pytest.mark.unit
def test_finds_positions_when_wrapped_in_items() -> None:
    """Some APIs nest the list: {"experience": {"items": [...]}}."""
    payload = {"experience": {"items": [{"company": "Acme", "title": "CTO"}]}}
    path, positions = probe._find_positions(payload)
    assert path == "experience.items"
    assert len(positions) == 1


@pytest.mark.unit
def test_falls_back_to_any_company_shaped_list() -> None:
    """Unknown key name, but the objects clearly describe employment."""
    payload = {"careerHistory": [{"company": "Acme", "title": "COO"}]}
    path, positions = probe._find_positions(payload)
    assert path == "careerHistory"
    assert len(positions) == 1


@pytest.mark.unit
def test_returns_empty_when_no_positions() -> None:
    """The important negative case — this is what a NO_POSITIONS verdict rests on."""
    payload = {"name": "X", "headline": "Founder", "follower_count": 100}
    path, positions = probe._find_positions(payload)
    assert path is None
    assert positions == []


@pytest.mark.unit
def test_ignores_empty_lists() -> None:
    payload = {"work_experience": [], "experience": []}
    path, positions = probe._find_positions(payload)
    assert path is None
    assert positions == []


@pytest.mark.unit
def test_ignores_unrelated_lists() -> None:
    """A skills array must not be mistaken for employment history."""
    payload = {"skills": [{"name": "Python"}, {"name": "SQL"}]}
    path, positions = probe._find_positions(payload)
    assert path is None


# ----------------------------- _field_report ---------------------------------

@pytest.mark.unit
def test_field_report_detects_all_six_fields() -> None:
    positions = [{
        "company": "Goldman Sachs", "title": "VP",
        "start": "2019-06", "end": "2026-02",
        "current": False, "company_url": "https://linkedin.com/company/goldman-sachs",
    }]
    report = probe._field_report(positions)
    assert report["company"] == "company"
    assert report["title"] == "title"
    assert report["start_date"] == "start"
    assert report["end_date"] == "end"
    assert report["is_current"] == "current"
    assert report["company_url"] == "company_url"


@pytest.mark.unit
def test_field_report_matches_camel_case_spellings() -> None:
    positions = [{"companyName": "Acme", "jobTitle": "CEO", "startDate": "2026-03"}]
    report = probe._field_report(positions)
    assert report["company"] == "companyName"
    assert report["title"] == "jobTitle"
    assert report["start_date"] == "startDate"


@pytest.mark.unit
def test_field_report_flags_missing_start_date() -> None:
    """No start date is the single most consequential gap: without it neither
    detector can compute recency, and the P0-1 verdict becomes NO_DATES."""
    positions = [{"company": "Acme", "title": "Founder"}]
    report = probe._field_report(positions)
    assert report["start_date"] is None
    assert report["company"] == "company"


@pytest.mark.unit
def test_field_report_unions_keys_across_positions() -> None:
    """Current roles often omit end dates; the report should still find the
    key from whichever position carries it."""
    positions = [
        {"company": "Acme", "title": "Founder", "start": "2026-03", "current": True},
        {"company": "Goldman", "title": "VP", "start": "2019-06", "end": "2026-02"},
    ]
    report = probe._field_report(positions)
    assert report["end_date"] == "end"
    assert report["is_current"] == "current"
