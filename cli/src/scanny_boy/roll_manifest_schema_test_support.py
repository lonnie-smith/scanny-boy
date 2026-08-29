"""Shared helper for validating a written roll manifest dict against
`shared/contract/roll-manifest.schema.json`.

Mirrors `manifest_schema_test_support.py`'s approach for Phase 1's manifest:
driving the test from the schema file itself, independent of any hand-written
structural checks a production module may add, so a drift between the two
would still be caught here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[3] / "shared" / "contract" / "roll-manifest.schema.json"
)


def load_roll_manifest_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text())


def _require_keys(data: dict[str, Any], keys: list[str]) -> None:
    missing = set(keys) - data.keys()
    assert not missing, f"missing required fields: {missing}"


def assert_matches_roll_manifest_schema(data: dict[str, Any], schema: dict[str, Any]) -> None:
    defs = schema["definitions"]
    _require_keys(data, schema["required"])

    assert (
        data["manifest_format_version"]
        == schema["properties"]["manifest_format_version"]["const"]
    )
    assert data["manifest_kind"] == schema["properties"]["manifest_kind"]["const"]
    assert data["status"] in defs["status"]["enum"]
    assert re.match(schema["properties"]["film_date"]["pattern"], data["film_date"])

    _require_keys(data["icc_profile"], defs["iccProfile"]["required"])
    sha256_pattern = defs["sha256"]["pattern"]
    assert re.match(sha256_pattern, data["icc_profile"]["sha256"])

    for source in data["sources"]:
        _require_keys(source, defs["source"]["required"])
        assert re.match(sha256_pattern, source["sha256"])

    for negative in data["negatives"]:
        _require_keys(negative, defs["negative"]["required"])
        assert negative["status"] in defs["negativeStatus"]["enum"]
        assert len(negative["members"]) >= 1
        if negative["output"] is not None:
            _require_keys(negative["output"], defs["output"]["required"])
            assert re.match(sha256_pattern, negative["output"]["sha256"])
        for frame in negative["frames"]:
            _require_keys(frame, defs["frame"]["required"])
            assert len(frame["translation"]) == 2
        for pair in negative["pairs"]:
            _require_keys(pair, defs["pair"]["required"])
        assert len(negative["fill_color"]) == 3
