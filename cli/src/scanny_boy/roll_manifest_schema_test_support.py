"""Shared helper for validating a written roll manifest dict against
`shared/contract/roll-manifest.schema.json`.

Mirrors `manifest_schema_test_support.py`'s approach for Phase 1's manifest:
driving the test from the schema file itself, independent of any hand-written
structural checks a production module may add, so a drift between the two
would still be caught here.

Phase 3 section 0: there is no migration, so this validates format version 3
and nothing else. The v2 rules P3-2 carried through the contract chunk are
gone with the supersession-tombstone removal.
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


def empty_v3_manifest(
    *,
    roll_id: str = "00000000-0000-4000-8000-000000000001",
    roll_name: str = "Test Roll",
    shots_per_negative: int = 3,
) -> dict[str, Any]:
    """Minimal empty v3 manifest matching §3.3."""
    now = "2026-08-30T12:00:00+00:00"
    sha = "a" * 64
    return {
        "manifest_format_version": 3,
        "manifest_kind": "roll",
        "scanny_boy_version": "0.3.0",
        "roll_id": roll_id,
        "roll_name": roll_name,
        "shots_per_negative": shots_per_negative,
        "created_at": now,
        "updated_at": now,
        "processing_params": {"gamma": [1.8, 16]},
        "icc_profile": {"name": "ProPhoto-v4.icc", "sha256": sha},
        "stitch_params": {"detector": "AKAZE"},
        "runs": [],
        "sources": [],
        "negatives": [],
        "metadata": {"roll_capture_date": None, "last_applied_at": None},
    }


def _require_keys(data: dict[str, Any], keys: list[str]) -> None:
    missing = set(keys) - data.keys()
    assert not missing, f"missing required fields: {missing}"


def _assert_matches_v3_roll_manifest_schema(
    data: dict[str, Any], schema: dict[str, Any]
) -> None:
    defs = schema["definitions"]
    _require_keys(data, schema["required"])

    assert (
        data["manifest_format_version"]
        == schema["properties"]["manifest_format_version"]["const"]
    )
    assert data["manifest_kind"] == schema["properties"]["manifest_kind"]["const"]

    _require_keys(data["icc_profile"], defs["iccProfile"]["required"])
    sha256_pattern = defs["sha256"]["pattern"]
    assert re.match(sha256_pattern, data["icc_profile"]["sha256"])

    _require_keys(data["metadata"], defs["metadata"]["required"])

    for run in data["runs"]:
        _require_keys(run, defs["run"]["required"])
        assert run["kind"] in defs["runKind"]["enum"]
        assert run["status"] in defs["runStatus"]["enum"]

    for source in data["sources"]:
        _require_keys(source, defs["source"]["required"])
        assert re.match(sha256_pattern, source["sha256"])

    for negative in data["negatives"]:
        _require_keys(negative, defs["negative"]["required"])
        assert negative["status"] in defs["negativeStatus"]["enum"]
        assert len(negative["members"]) >= 1
        _require_keys(negative["capture_time"], defs["captureTime"]["required"])
        if negative["output"] is not None:
            _require_keys(negative["output"], defs["output"]["required"])
            assert re.match(sha256_pattern, negative["output"]["sha256"])
        for frame in negative["frames"]:
            _require_keys(frame, defs["frame"]["required"])
            assert len(frame["translation"]) == 2
        for pair in negative["pairs"]:
            _require_keys(pair, defs["pair"]["required"])
        assert len(negative["fill_color"]) == 3


def assert_matches_roll_manifest_schema(data: dict[str, Any], schema: dict[str, Any]) -> None:
    _assert_matches_v3_roll_manifest_schema(data, schema)
