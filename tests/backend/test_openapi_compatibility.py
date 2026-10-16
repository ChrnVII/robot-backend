import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.no_database


def test_rest_contract_matches_pre_refactor_snapshot():
    from app.main import app
    baseline = Path(__file__).parents[1] / "contracts" / "openapi.json"
    assert app.openapi() == json.loads(baseline.read_text(encoding="utf-8"))
