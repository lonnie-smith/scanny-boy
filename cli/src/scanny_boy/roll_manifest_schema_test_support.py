"""Shared helper for validating a written roll manifest dict against
`shared/contract/roll-manifest.schema.json`.

Mirrors `manifest_schema_test_support.py`'s approach for Phase 1's manifest:
driving the test from the schema file itself, independent of any hand-written
structural checks a production module may add, so a drift between the two
would still be caught here.

Phase 3 section 0: there is no migration, so this validates the current
format version and nothing else. The v2 rules P3-2 carried through the
contract chunk are gone with the supersession-tombstone removal; v4 added
per-frame solved photometric gains and per-pair pre-gain overlap MAD; v5
dropped the roll-level `shots_per_negative`; v6 added a per-frame solved
scale (docs/STITCH_QUALITY_PLAN.md section 2); v7 added the per-negative
rig-tilt rectification record (docs/RECTIFICATION_PLAN.md section 7);
v8 added the top-level `film_base` block (docs/REBATE_ANCHORING.md §3.1)
and the per-negative `normalization.base_check` sub-block (§6).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[3]
    / "shared"
    / "contract"
    / "roll-manifest.schema.json"
)


def load_roll_manifest_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text())


def empty_v5_manifest(
    *,
    roll_id: str = "00000000-0000-4000-8000-000000000001",
    roll_name: str = "Test Roll",
) -> dict[str, Any]:
    """Minimal empty v5 manifest matching §3.3."""
    now = "2026-08-30T12:00:00+00:00"
    sha = "a" * 64
    return {
        "manifest_format_version": 5,
        "manifest_kind": "roll",
        "scanny_boy_version": "0.3.0",
        "roll_id": roll_id,
        "roll_name": roll_name,
        "created_at": now,
        "updated_at": now,
        "processing_params": {"gamma": [1.8, 16]},
        "icc_profile": {"name": "ProPhoto-v4.icc", "sha256": sha},
        "stitch_params": {"detector": "AKAZE"},
        "runs": [],
        "sources": [],
        "negatives": [],
        "metadata": {
            "roll_capture_date": None,
            "last_applied_at": None,
            "film": None,
            "iso": None,
            "city": None,
            "state": None,
            "camera": None,
            "lens": None,
            "caption": None,
        },
    }


def _require_keys(data: dict[str, Any], keys: list[str]) -> None:
    missing = set(keys) - data.keys()
    assert not missing, f"missing required fields: {missing}"


def _assert_matches_v5_roll_manifest_schema(
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
            assert len(frame["gain"]) == 3
        for pair in negative["pairs"]:
            _require_keys(pair, defs["pair"]["required"])
        assert len(negative["fill_color"]) == 3
        if negative["rectification"] is not None:
            block = negative["rectification"]
            _require_keys(block, defs["rectification"]["required"])
            assert len(block["l"]) == 2
            assert len(block["centre"]) == 2
            assert len(block["frame_size"]) == 2
            assert 0.0 <= block["relative_improvement"] <= 1.0
            assert block["pair_count"] >= 1

    if data.get("film") is not None:
        _require_keys(data["film"], defs["filmDecision"]["required"])
        assert data["film"]["kind"] in defs["filmDecision"]["properties"]["kind"]["enum"]
        if "source" in data["film"]:
            assert data["film"]["source"] in defs["filmDecision"]["properties"]["source"]["enum"]

    if data.get("film_base") is not None:
        # REBATE_ANCHORING §3.1.
        block = data["film_base"]
        _require_keys(block, defs["filmBase"]["required"])
        assert len(block["density"]) == 3
        assert block["locked_at"] is None or isinstance(block["locked_at"], str)
        assert len(block["clipped_fractions"]) == 3
        assert 0 <= block["chosen_index"] < len(block["populations"])
        for population in block["populations"]:
            _require_keys(population, defs["filmBase"]["properties"]["populations"]["items"]["required"])
            assert len(population["density"]) == 3

    for negative in data["negatives"]:
        normalization = negative.get("normalization")
        if normalization is not None and normalization.get("base_check") is not None:
            # REBATE_ANCHORING §6.
            _require_keys(
                normalization["base_check"], defs["baseCheck"]["required"]
            )
            assert normalization["base_check"]["shape_residual"] >= 0.0


def assert_matches_roll_manifest_schema(
    data: dict[str, Any], schema: dict[str, Any]
) -> None:
    _assert_matches_v5_roll_manifest_schema(data, schema)
