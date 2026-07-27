"""include_remote: expressing OR across filter dimensions the API only ANDs."""

from jd_scraper.filters import build_request_body, expand_queries
from jd_scraper.models import SearchProfile


def _profile(**overrides):
    base = {
        "name": "atl",
        "posted_within_days": 1,
        "locations": {"countries": ["US"], "patterns": ["Atlanta"]},
    }
    base.update(overrides)
    return SearchProfile.model_validate(base)


def test_single_query_when_include_remote_is_off():
    assert len(expand_queries(_profile())) == 1


def test_splits_into_location_and_remote_searches():
    queries = expand_queries(_profile(include_remote=True))
    assert [label for label, _ in queries] == ["location", "remote"]


def test_location_search_keeps_the_pattern_and_no_remote_flag():
    _, located = expand_queries(_profile(include_remote=True))[0]
    body = build_request_body(located)
    assert body["job_location_pattern_or"] == ["Atlanta"]
    assert "remote" not in body, "the location search must not be narrowed to remote"


def test_remote_search_drops_the_location_pattern():
    """The crux: keeping it would AND back down to remote-jobs-in-Atlanta."""
    _, remote = expand_queries(_profile(include_remote=True))[1]
    body = build_request_body(remote)

    assert body["remote"] is True
    assert "job_location_pattern_or" not in body
    assert body["job_country_code_or"] == ["US"], "country scope is still respected"


def test_explicit_remote_flag_is_honoured_not_widened():
    """If the user asked for remote-only, don't silently turn it into an OR."""
    queries = expand_queries(_profile(include_remote=True, remote=True))
    assert len(queries) == 1
    assert build_request_body(queries[0][1])["remote"] is True


def test_no_split_without_a_location_filter():
    profile = SearchProfile.model_validate(
        {"name": "n", "posted_within_days": 1, "include_remote": True}
    )
    assert len(expand_queries(profile)) == 1


def test_both_variants_keep_the_mandatory_filter():
    for _, variant in expand_queries(_profile(include_remote=True)):
        body = build_request_body(variant)
        assert body["posted_at_max_age_days"] == 1


def test_variants_do_not_mutate_the_original():
    profile = _profile(include_remote=True)
    expand_queries(profile)
    assert profile.locations.patterns == ["Atlanta"]
    assert profile.remote is None
    assert profile.include_remote is True
