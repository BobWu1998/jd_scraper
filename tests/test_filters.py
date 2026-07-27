import pytest

from jd_scraper.filters import EmptyFilterError, apply_source_filter, build_request_body, is_linkedin
from jd_scraper.models import Job, SearchProfile


def test_maps_core_filters(profile_dict):
    profile = SearchProfile.model_validate(profile_dict)
    body = build_request_body(profile, page=2, page_size=25)

    assert body["job_title_or"] == ["Machine Learning Engineer"]
    assert body["posted_at_max_age_days"] == 7
    assert body["job_country_code_or"] == ["US"]
    assert body["page"] == 2
    assert body["limit"] == 25
    assert body["order_by"] == [{"field": "date_posted", "desc": True}]


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
            "min_salary_usd": 150000,
            "companies_exclude": ["Staffing Co"],
            "description_contains": ["pytorch"],
            "locations": {"countries": ["US"], "patterns": ["San Francisco"]},
        }
    )
    body = build_request_body(SearchProfile.model_validate(profile_dict))

    assert body["remote"] is True
    assert body["job_title_not"] == ["Intern"]
    assert body["job_seniority_or"] == ["senior"]
    assert body["min_salary_usd"] == 150000
    assert body["company_name_not"] == ["Staffing Co"]
    assert body["job_description_pattern_or"] == ["pytorch"]
    assert body["job_location_pattern_or"] == ["San Francisco"]


def test_extra_passthrough_overrides_mapping(profile_dict):
    profile_dict["extra"] = {"job_title_or": ["override"], "brand_new_filter": 42}
    body = build_request_body(SearchProfile.model_validate(profile_dict))

    assert body["job_title_or"] == ["override"]
    assert body["brand_new_filter"] == 42


def test_profile_with_no_filters_is_rejected():
    profile = SearchProfile.model_validate({"name": "empty"})
    with pytest.raises(EmptyFilterError):
        build_request_body(profile)


def test_unknown_profile_key_is_rejected(profile_dict):
    profile_dict["typoed_key"] = 1
    with pytest.raises(Exception):
        SearchProfile.model_validate(profile_dict)


@pytest.mark.parametrize(
    "job, expected",
    [
        (Job(id="a", url="https://www.linkedin.com/jobs/view/1"), True),
        (Job(id="b", final_url="https://linkedin.com/jobs/view/2"), True),
        (Job(id="c", source_url="https://LinkedIn.com/jobs/view/3"), True),
        (Job(id="d", source="linkedin"), True),
        (Job(id="e", url="https://boards.greenhouse.io/x/jobs/4"), False),
        (Job(id="f"), False),
    ],
)
def test_linkedin_detection(job, expected):
    assert is_linkedin(job) is expected


def test_source_filter_respects_toggle(sample_response, profile_dict):
    jobs = [Job.model_validate(j) for j in sample_response["data"]]

    profile_dict["sources"] = {"linkedin_only": True}
    kept = apply_source_filter(jobs, SearchProfile.model_validate(profile_dict))
    assert [j.id for j in kept] == ["job-1", "job-3"]

    profile_dict["sources"] = {"linkedin_only": False}
    kept = apply_source_filter(jobs, SearchProfile.model_validate(profile_dict))
    assert len(kept) == 3


def test_unknown_response_fields_are_preserved(sample_response):
    job = Job.model_validate(sample_response["data"][0])
    assert job.model_dump()["an_unexpected_field"] == "must not crash the parser"
