import pytest

from jd_scraper.filters import (
    MANDATORY_FILTERS,
    MissingMandatoryFilterError,
    apply_source_filter,
    build_request_body,
    domain_of,
    is_linkedin,
)
from jd_scraper.models import Job, SearchProfile


def test_maps_core_filters(profile_dict):
    profile = SearchProfile.model_validate(profile_dict)
    body = build_request_body(profile, page=2, page_size=25)

    assert body["job_title_or"] == ["Machine Learning Engineer"]
    assert body["posted_at_max_age_days"] == 7
    assert body["job_country_code_or"] == ["US"]
    assert body["page"] == 2
    assert body["limit"] == 25
    assert body["order_by"][0] == {"field": "date_posted", "desc": True}


def test_omits_unset_filters(profile_dict):
    body = build_request_body(SearchProfile.model_validate(profile_dict))
    for absent in ("remote", "min_salary_usd", "company_name_not", "job_title_not"):
        assert absent not in body


def test_optional_filters_are_mapped(profile_dict):
    profile_dict.update(
        {
            "remote": True,
            "title_exclude": ["Intern"],
            "seniority": ["senior"],
            "employment_types": ["full_time"],
            "min_salary_usd": 150000,
            "companies_exclude": ["Staffing Co"],
            "exclude_recruiting_agencies": True,
            "description_contains": ["pytorch"],
            "description_pattern": ["(?i)llm"],
            "description_exclude": ["clearance"],
            "locations": {"countries": ["US"], "patterns": ["San Francisco"], "ids": [5391959]},
        }
    )
    body = build_request_body(SearchProfile.model_validate(profile_dict))

    assert body["remote"] is True
    assert body["job_title_not"] == ["Intern"]
    assert body["job_seniority_or"] == ["senior"]
    assert body["employment_statuses_or"] == ["full_time"]
    assert body["min_salary_usd"] == 150000
    assert body["company_name_not"] == ["Staffing Co"]
    assert body["company_type"] == "direct_employer"
    # Whole-word and regex description filters are distinct API fields.
    assert body["job_description_contains_or"] == ["pytorch"]
    assert body["job_description_pattern_or"] == ["(?i)llm"]
    assert body["job_description_contains_not"] == ["clearance"]
    assert body["job_location_pattern_or"] == ["San Francisco"]
    assert body["job_location_or"] == [{"id": 5391959}]


def test_linkedin_only_filters_server_side(profile_dict):
    """The whole point: don't pay credits for results we would discard locally."""
    body = build_request_body(SearchProfile.model_validate(profile_dict))
    assert body["url_domain_or"] == ["linkedin.com"]


def test_extra_source_domains_are_added(profile_dict):
    profile_dict["sources"] = {"linkedin_only": True, "domains": ["greenhouse.io"]}
    body = build_request_body(SearchProfile.model_validate(profile_dict))
    assert body["url_domain_or"] == ["linkedin.com", "greenhouse.io"]


def test_no_source_filter_when_linkedin_only_disabled(profile_dict):
    profile_dict["sources"] = {"linkedin_only": False}
    body = build_request_body(SearchProfile.model_validate(profile_dict))
    assert "url_domain_or" not in body


def test_extra_passthrough_overrides_mapping(profile_dict):
    profile_dict["extra"] = {"job_title_or": ["override"], "brand_new_filter": 42}
    body = build_request_body(SearchProfile.model_validate(profile_dict))

    assert body["job_title_or"] == ["override"]
    assert body["brand_new_filter"] == 42


def test_preview_and_totals_are_opt_in(profile_dict):
    body = build_request_body(SearchProfile.model_validate(profile_dict))
    assert "blur_company_data" not in body
    assert "include_total_results" not in body

    body = build_request_body(
        SearchProfile.model_validate(profile_dict), preview=True, include_total_results=True
    )
    assert body["blur_company_data"] is True
    assert body["include_total_results"] is True


# --- the API's mandatory-filter rule ------------------------------------------


def test_title_only_profile_is_rejected_locally():
    """The API rejects this; catching it locally saves a round trip."""
    profile = SearchProfile.model_validate({"name": "titles-only", "titles": ["ML Engineer"]})
    with pytest.raises(MissingMandatoryFilterError):
        build_request_body(profile)


def test_location_only_profile_is_rejected_locally():
    profile = SearchProfile.model_validate(
        {"name": "loc-only", "locations": {"countries": ["US"]}}
    )
    with pytest.raises(MissingMandatoryFilterError):
        build_request_body(profile)


@pytest.mark.parametrize(
    "profile_extra",
    [
        {"posted_within_days": 7},
        {"posted_after": "2026-07-01"},
        {"posted_before": "2026-07-15"},
        {"companies": ["Google"]},
    ],
)
def test_each_mandatory_filter_satisfies_the_rule(profile_extra):
    profile = SearchProfile.model_validate({"name": "ok", **profile_extra})
    body = build_request_body(profile)
    assert any(body.get(f) for f in MANDATORY_FILTERS)


def test_mandatory_rule_can_be_satisfied_via_extra():
    profile = SearchProfile.model_validate(
        {"name": "raw", "extra": {"company_linkedin_url_or": ["acme"]}}
    )
    body = build_request_body(profile)
    assert body["company_linkedin_url_or"] == ["acme"]


def test_unknown_profile_key_is_rejected(profile_dict):
    profile_dict["typoed_key"] = 1
    with pytest.raises(Exception):
        SearchProfile.model_validate(profile_dict)


def test_invalid_seniority_is_rejected(profile_dict):
    profile_dict["seniority"] = ["principal"]
    with pytest.raises(Exception, match="unknown seniority"):
        SearchProfile.model_validate(profile_dict)


def test_preview_with_company_filter_is_rejected(profile_dict):
    profile_dict.update({"preview": True, "companies": ["Google"]})
    with pytest.raises(Exception, match="preview mode"):
        SearchProfile.model_validate(profile_dict)


# --- LinkedIn detection --------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.linkedin.com/jobs/view/1", True),
        ("https://linkedin.com/jobs/view/2", True),
        ("https://LinkedIn.com/jobs/view/3", True),
        ("https://uk.linkedin.com/jobs/view/4", True),
        ("https://boards.greenhouse.io/x/jobs/5", False),
        # Must not be fooled by a lookalike host.
        ("https://notlinkedin.com/jobs/view/6", False),
        ("https://evil.com/linkedin.com/jobs", False),
        (None, False),
    ],
)
def test_linkedin_detection(url, expected):
    assert is_linkedin(Job(id=1, url=url)) is expected


def test_domain_of():
    assert domain_of("https://www.linkedin.com/jobs/view/1") == "linkedin.com"
    assert domain_of("https://boards.greenhouse.io/x") == "boards.greenhouse.io"
    assert domain_of(None) is None


def test_source_filter_respects_toggle(sample_response, profile_dict):
    jobs = [Job.model_validate(j) for j in sample_response["data"]]

    profile_dict["sources"] = {"linkedin_only": True}
    kept = apply_source_filter(jobs, SearchProfile.model_validate(profile_dict))
    assert [j.id for j in kept] == [1111, 3333]

    profile_dict["sources"] = {"linkedin_only": False}
    kept = apply_source_filter(jobs, SearchProfile.model_validate(profile_dict))
    assert len(kept) == 3


def test_unknown_response_fields_are_preserved(sample_response):
    job = Job.model_validate(sample_response["data"][0])
    assert job.model_dump()["an_unexpected_field"] == "must not crash the parser"


def test_company_name_prefers_company_object(sample_response):
    job = Job.model_validate(sample_response["data"][0])
    assert job.company == "Acme AI", "the deprecated field is still parsed"
    assert job.company_name() == "Acme AI Inc", "company_object.name wins"
