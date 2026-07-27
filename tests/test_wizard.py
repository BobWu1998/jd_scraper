import typer
import yaml

from jd_scraper.models import SearchProfile
from jd_scraper.wizard import build_profile_interactively, profile_to_yaml, write_profile

# Answers in prompt order: name, titles, title_exclude, posted_within_days, countries,
# patterns, remote, seniority, min_salary, companies_exclude, exclude_agencies,
# linkedin_only, limit.
ANSWERS = [
    "ml-us",
    "Machine Learning Engineer, ML Engineer",
    "Intern",
    "14",
    "US",
    "San Francisco",
    "y",
    "senior",
    "150000",
    "Staffing Co",
    "y",
    "y",
    "25",
]
NUM_PROMPTS = len(ANSWERS)


def _scripted(monkeypatch, answers):
    """Feed scripted answers, modeling typer's real Enter-takes-the-default behaviour."""
    remaining = list(answers)

    def fake_prompt(text, default="", **kwargs):
        if not remaining:
            return default
        value = remaining.pop(0)
        # Pressing Enter on an empty line yields the default, not "".
        return default if value == "" else value

    def fake_confirm(text, default=False, **kwargs):
        if not remaining:
            return default
        value = remaining.pop(0)
        if value == "":
            return default
        return str(value).lower() in {"y", "yes", "true"}

    monkeypatch.setattr(typer, "prompt", fake_prompt)
    monkeypatch.setattr(typer, "confirm", fake_confirm)


def test_wizard_collects_all_criteria(monkeypatch):
    _scripted(monkeypatch, ANSWERS)
    prof = build_profile_interactively()

    assert prof.name == "ml-us"
    assert prof.titles == ["Machine Learning Engineer", "ML Engineer"]
    assert prof.title_exclude == ["Intern"]
    assert prof.posted_within_days == 14
    assert prof.locations.countries == ["US"]
    assert prof.locations.patterns == ["San Francisco"]
    assert prof.remote is True
    assert prof.seniority == ["senior"]
    assert prof.min_salary_usd == 150000
    assert prof.companies_exclude == ["Staffing Co"]
    assert prof.sources.linkedin_only is True
    assert prof.limit == 25


def test_blank_answers_mean_no_filter(monkeypatch):
    _scripted(monkeypatch, ["just-a-name"] + [""] * (NUM_PROMPTS - 1))
    prof = build_profile_interactively()

    assert prof.titles == []
    assert prof.remote is None, "blank must mean 'do not filter', not False"
    assert prof.min_salary_usd is None


def test_written_profile_round_trips(monkeypatch, tmp_path):
    _scripted(monkeypatch, ANSWERS)
    prof = build_profile_interactively()

    path = write_profile(prof, tmp_path / "p.yml")
    reloaded = SearchProfile.from_yaml(str(path))

    assert reloaded == prof, "a written profile must load back identically"


def test_yaml_omits_empty_filters(monkeypatch):
    _scripted(monkeypatch, ["sparse", "", "", "7", "", "", "", "", "", "", "", "y", "10"])
    prof = build_profile_interactively()
    data = yaml.safe_load(profile_to_yaml(prof))

    assert "titles" not in data
    assert "min_salary_usd" not in data
    assert data["posted_within_days"] == 7
    assert data["name"] == "sparse"


def test_edit_seeds_defaults_and_keeps_unprompted_fields(monkeypatch, tmp_path):
    base = SearchProfile.model_validate(
        {
            "name": "base",
            "titles": ["Data Scientist"],
            "posted_within_days": 30,
            "description_contains": ["pytorch"],
            "extra": {"some_raw_filter": 1},
            "limit": 40,
        }
    )
    # All blank -> every prompt keeps its seeded default.
    _scripted(monkeypatch, [""] * NUM_PROMPTS)
    edited = build_profile_interactively(base)

    assert edited.name == "base"
    assert edited.titles == ["Data Scientist"], "existing values must survive a blank Enter"
    assert edited.posted_within_days == 30
    assert edited.limit == 40
    assert edited.description_contains == ["pytorch"], "unprompted filters must not be dropped"
    assert edited.extra == {"some_raw_filter": 1}
