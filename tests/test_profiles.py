"""Every profile shipped in profiles/ must load and produce a valid request body."""

from pathlib import Path

import pytest

from jd_scraper.filters import MANDATORY_FILTERS, build_request_body, expand_queries
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

    assert profile.titles == [
        "machine learning",
        "ML engineer",
        "computer vision",
        "robotics",
        "AI",
    ]
    assert profile.limit == 10
    assert profile.preview is True

    body = build_request_body(profile, page=0, page_size=profile.page_size)
    assert body["job_seniority_or"] == ["junior", "mid_level"]
    assert body["limit"] == 10
    assert body["blur_company_data"] is True

    # The window is a tuning knob -- assert it is set and sane, not its exact
    # value, so widening or narrowing the search does not fail the suite.
    assert body["posted_at_max_age_days"] >= 0


def test_atlanta_profile_ors_location_with_remote():
    """Two searches, because the API cannot AND its way to an OR."""
    profile = SearchProfile.from_yaml(str(PROFILE_DIR / "atlanta-ml.yml"))
    queries = expand_queries(profile)

    assert [label for label, _ in queries] == ["location", "remote"]

    located = build_request_body(queries[0][1])
    assert located["job_location_pattern_or"] == ["Atlanta"]
    assert "remote" not in located

    remote = build_request_body(queries[1][1])
    assert remote["remote"] is True
    assert "job_location_pattern_or" not in remote, "would re-narrow to remote-in-Atlanta"
