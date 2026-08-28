"""Shared helper for validating a written manifest dict against
`shared/contract/manifest.schema.json`.

This is deliberately independent of `manifest.py`'s own hand-written
structural checks (`validate_manifest_dict`), which exist so the packaged
CLI never has to load a file outside `cli/src/scanny_boy/` at runtime (see
that module's docstring). Driving this test helper from the schema file
itself — the same approach `schema_test_support.py` uses for events — means
a manifest.py/schema.json drift would still be caught here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[3] / "shared" / "contract" / "manifest.schema.json"
)


def load_manifest_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text())


def _require_keys(data: dict[str, Any], keys: list[str]) -> None:
    missing = set(keys) - data.keys()
    assert not missing, f"missing required fields: {missing}"


def assert_matches_manifest_schema(data: dict[str, Any], schema: dict[str, Any]) -> None:
    defs = schema["definitions"]
    _require_keys(data, schema["required"])

    assert (
        data["manifest_format_version"]
        == schema["properties"]["manifest_format_version"]["const"]
    )
    assert data["status"] in defs["status"]["enum"]
    assert re.match(schema["properties"]["film_date"]["pattern"], data["film_date"])

    _require_keys(data["icc_profile"], defs["iccProfile"]["required"])
    sha256_pattern = defs["sha256"]["pattern"]
    assert re.match(sha256_pattern, data["icc_profile"]["sha256"])

    for source in data["sources"]:
        _require_keys(source, defs["source"]["required"])
        assert re.match(sha256_pattern, source["sha256"])

    _require_keys(data["curated_metadata"], defs["curatedMetadata"]["required"])
    assert len(data["curated_metadata"]["camera_whitebalance"]) == 4

    for group in data["groups"]:
        _require_keys(group, defs["group"]["required"])
        assert group["status"] in defs["groupStatus"]["enum"]
        assert len(group["members"]) >= 1
        assert len(group["expected_outputs"]) >= 1
        for output in group["outputs"]:
            _require_keys(output, defs["output"]["required"])
            assert re.match(sha256_pattern, output["sha256"])
