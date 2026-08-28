"""Shared helper for validating emitted events against schema.json.

This is a small hand-rolled check rather than a full JSON Schema engine, so
that the dev dependency set stays exactly what section 5.1 of the
implementation plan lists (pytest, pytest-cov, ruff, PyInstaller). It reads
its enums and required-field rules straight out of schema.json, so it stays
correct as the schema grows.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_SCHEMA_PATH = Path(__file__).resolve().parents[3] / "shared" / "contract" / "schema.json"


def load_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text())


def assert_matches_schema(event: dict[str, Any], schema: dict[str, Any]) -> None:
    """Assert `event` satisfies the base envelope and its event-specific
    `allOf`/`if`/`then` branch in schema.json."""

    assert event.keys() >= set(schema["required"]), (
        f"missing required base fields: {set(schema['required']) - event.keys()}"
    )
    assert event["protocol_version"] == schema["properties"]["protocol_version"]["const"]
    event_types = schema["definitions"]["eventType"]["enum"]
    assert event["event"] in event_types, f"{event['event']!r} not in {event_types}"
    if "run_id" in event:
        assert isinstance(event["run_id"], str)

    for branch in schema["allOf"]:
        if branch["if"]["properties"]["event"]["const"] != event["event"]:
            continue
        then = branch["then"]
        required = set(then.get("required", []))
        assert event.keys() >= required, (
            f"event {event['event']!r} missing fields: {required - event.keys()}"
        )
        properties = then.get("properties", {})
        if "code" in properties:
            codes = schema["definitions"]["code"]["enum"]
            assert event["code"] in codes, f"{event['code']!r} not in {codes}"
        if "step" in properties:
            steps = schema["definitions"]["pipelineStep"]["enum"]
            assert event["step"] in steps, f"{event['step']!r} not in {steps}"
        if "command" in properties:
            commands = properties["command"]["enum"]
            assert event["command"] in commands, f"{event['command']!r} not in {commands}"
