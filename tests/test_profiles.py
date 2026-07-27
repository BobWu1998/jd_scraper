"""Every profile shipped in profiles/ must load and produce a valid request body."""

from pathlib import Path

import pytest

from jd_scraper.filters import MANDATORY_FILTERS, build_request_body
from jd_scraper.models import SearchProfile

PROFILE_DIR = Path(__file__).resolve().parent.parent / "profiles"
PROFILES = sorted(PROFILE_DIR.glob("*.yml"))


def test_profiles_exist():
    assert PROFILES, f"no profiles found in {PROFILE_DIR}"


@pytest.mark.parametrize("path", PROFILES, ids=lambda p: p.stem)
def test_profile_is_valid_and_searchable(path):
    profile = SearchProfile.from_yaml(str(path))
    body = build_request_body(profile, page=0, page_size=profile.page_size)

    assert any(body.get(f) for f in MANDATORY_FILTERS), (
        f"{path.name} would be rejected by the API: no mandatory filter set"
    )
    assert body["limit"] <= profile.limit


def test_atlanta_profile_matches_intent():
    """Atlanta OR remote, junior/mid, last day, capped at 10, free preview."""
    profile = SearchProfile.from_yaml(str(PROFILE_DIR / "atlanta-ml.yml"))
    body = build_request_body(profile, page=0, page_size=profile.page_size)

    assert body["job_title_or"] == ["machine learning", "computer vision", "robotics"]
    assert body["job_seniority_or"] == ["junior", "mid_level"]
    assert body["posted_at_max_age_days"] == 1
    assert body["job_location_pattern_or"] == ["Atlanta", "Remote"]
    assert body["limit"] == 10
    assert body["blur_company_data"] is True

    # The crux: `remote` must stay unset. Setting it true would AND with the
    # Atlanta pattern and return only remote-jobs-in-Atlanta.
    assert "remote" not in body, "remote must stay unset so location patterns OR"
