import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_response() -> dict:
    return json.loads((FIXTURES / "sample_response.json").read_text())


@pytest.fixture
def profile_dict() -> dict:
    return {
        "name": "test-profile",
        "titles": ["Machine Learning Engineer"],
        "posted_within_days": 7,
        "locations": {"countries": ["US"]},
        "limit": 10,
        "page_size": 5,
    }
