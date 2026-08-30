"""Shared helper for validating a written roll manifest dict against
`shared/contract/roll-manifest.schema.json`.

Mirrors `manifest_schema_test_support.py`'s approach for Phase 1's manifest:
driving the test from the schema file itself, independent of any hand-written
structural checks a production module may add, so a drift between the two
would still be caught here.

Phase 2 manifests (`manifest_format_version: 1`) are validated with the
embedded v1 rules below until P3-2 retires that writer. The schema file
itself describes format version 2 only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[3] / "shared" / "contract" / "roll-manifest.schema.json"
)

_V1_REQUIRED = [
    "manifest_format_version",
    "manifest_kind",
    "scanny_boy_version",
    "run_id",
    "status",
    "input_folder",
    "film_date",
    "shots_per_negative",
    "convert_run_id",
    "processing_params",
    "icc_profile",
    "stitch_params",
    "source_order",
    "sources",
    "negatives",
    "started_at",
    "finished_at",
]

_V1_STATUSES = {"running", "partial", "cancelled", "complete"}
_V1_NEGATIVE_STATUSES = {"pending", "completed", "failed"}


def load_roll_manifest_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text())


def empty_v2_manifest(
    *,
    roll_id: str = "00000000-0000-4000-8000-000000000001",
    roll_name: str = "Test Roll",
    shots_per_negative: int = 3,
) -> dict[str, Any]:
    """Minimal empty v2 manifest matching §3.3."""
    now = "2026-08-30T12:00:00+00:00"
    sha = "a" * 64
    return {
        "manifest_format_version": 2,
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


def _assert_matches_v1_roll_manifest_schema(data: dict[str, Any]) -> None:
    _require_keys(data, _V1_REQUIRED)

    assert data["manifest_format_version"] == 1
    assert data["manifest_kind"] == "stitch"
    assert data["status"] in _V1_STATUSES
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", data["film_date"])

    _require_keys(data["icc_profile"], ["name", "sha256"])
    sha256_pattern = r"^[0-9a-f]{64}$"
    assert re.match(sha256_pattern, data["icc_profile"]["sha256"])

    for source in data["sources"]:
        _require_keys(source, ["filename", "absolute_path", "size", "mtime", "sha256"])
        assert re.match(sha256_pattern, source["sha256"])

    for negative in data["negatives"]:
        _require_keys(
            negative,
            [
                "negative_id",
                "members",
                "expected_output",
                "status",
                "output",
                "frames",
                "pairs",
                "global_rms_px",
                "canvas",
                "valid_rect",
                "fill_color",
                "rebate_deviation_px",
                "error_code",
                "error_message",
            ],
        )
        assert negative["status"] in _V1_NEGATIVE_STATUSES
        assert len(negative["members"]) >= 1
        if negative["output"] is not None:
            _require_keys(negative["output"], ["name", "size", "sha256", "width", "height"])
            assert re.match(sha256_pattern, negative["output"]["sha256"])
        for frame in negative["frames"]:
            _require_keys(frame, ["name", "rotation_deg", "translation"])
            assert len(frame["translation"]) == 2
        for pair in negative["pairs"]:
            _require_keys(
                pair,
                [
                    "a",
                    "b",
                    "inliers",
                    "good_matches",
                    "inlier_ratio",
                    "rms_residual_px",
                    "scale_drift",
                    "overlap_fraction",
                    "overlap_mad",
                    "accepted",
                ],
            )
        assert len(negative["fill_color"]) == 3


def _assert_matches_v2_roll_manifest_schema(
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
    if data.get("manifest_format_version") == 1:
        _assert_matches_v1_roll_manifest_schema(data)
        return
    _assert_matches_v2_roll_manifest_schema(data, schema)
