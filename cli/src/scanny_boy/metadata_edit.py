"""`metadata set` / `metadata values`: the extended-metadata editing entry
points.

**Metadata lives in the database; only export touches TIFFs.** `metadata
set` writes the roll-level fallbacks and the negatives' explicit values
into the library database — it never opens a TIFF. Each change to a
capture date recomputes every intended timestamp via
`roll_sequence.apply_intended_times`, so the stored intent is always the
rank-based formula's current answer (noon + rank − 1 seconds on the
negative's effective date, ranked within that date in roll order).

The extended metadata uses *live fallback* semantics: the roll-level value
is never copied onto the negatives. A negative's effective value is its own
explicit value when it has one, else the roll's — so changing a roll value
instantly covers every negative without an explicit one, and a negative
added later inherits the roll's values without a write.

A whole payload is validated before anything is written, so a batch either
lands or fails without partial effects (the same rule the edit commands
follow).
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scanny_boy.events import Code
from scanny_boy.library import repo
from scanny_boy.library.repo import RollNotRegisteredError
from scanny_boy.manifest import BadManifestError
from scanny_boy.roll_manifest import (
    METADATA_FIELDS,
    load_roll_manifest,
    write_roll_manifest,
)
from scanny_boy.roll_sequence import apply_intended_times

if TYPE_CHECKING:
    from scanny_boy.roll_manifest import NegativeRecord, RollManifest

# The fields the catalog remembers — every extended-metadata field except
# `caption`, which is prose rather than a canonical value.
CATALOG_FIELDS = ("city", "state", "camera", "lens")

# The payload's roll-level keys. `capture_date` is the roll_capture_date;
# the rest are the extended-metadata fallbacks.
ROLL_KEYS = ("capture_date", "city", "state", "camera", "lens", "caption")
# The payload's per-negative keys, plus `capture_date` (the negative's
# `date_override`).
NEGATIVE_KEYS = ("capture_date",) + METADATA_FIELDS


class MetadataEditFailure(Exception):
    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _clean_value(value: Any, what: str) -> str | None:
    """Normalizes one payload value: `None` or an empty (after stripping)
    string means *clear*; anything else must be a string. Metadata values
    are never copied verbatim with stray whitespace around them."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise MetadataEditFailure(
            Code.INVALID_METADATA, f"{what} must be a string or null"
        )
    cleaned = value.strip()
    return cleaned or None


def _clean_date(value: Any, what: str) -> str | None:
    """Normalizes one date payload value: `None`/empty clears; otherwise a
    strict `YYYY-MM-DD` calendar date is required — the same shape the
    manifest schema's pattern pins."""
    cleaned = _clean_value(value, what)
    if cleaned is None:
        return None
    try:
        datetime.date.fromisoformat(cleaned)
    except ValueError:
        raise MetadataEditFailure(
            Code.INVALID_METADATA,
            f"{what} must be a YYYY-MM-DD date, got {cleaned!r}",
        ) from None
    return cleaned


def _validate_field_keys(payload: dict[str, Any], allowed: tuple[str, ...], what: str) -> None:
    unknown = sorted(set(payload) - set(allowed))
    if unknown:
        raise MetadataEditFailure(
            Code.INVALID_METADATA,
            f"unknown {what} field(s) {', '.join(unknown)}; expected "
            f"{', '.join(allowed)}",
        )


def _validated_roll(roll_dir: Path) -> RollManifest:
    if not repo.roll_registered(roll_dir):
        raise MetadataEditFailure(
            Code.ROLL_NOT_FOUND,
            f"{roll_dir} is not a registered roll; create the roll first",
        )
    try:
        return load_roll_manifest(roll_dir)
    except (BadManifestError, RollNotRegisteredError) as exc:
        raise MetadataEditFailure(exc.code, exc.message) from exc


def _apply_roll_fields(roll: RollManifest, fields: dict[str, Any]) -> None:
    """Mutates the roll's metadata with the payload's roll-level fields.
    Validation has already normalized every value."""
    for key, value in fields.items():
        if key == "capture_date":
            roll.metadata.roll_capture_date = value
        else:
            setattr(roll.metadata, key, value)


def _apply_negative_fields(
    roll: RollManifest,
    by_id: dict[str, NegativeRecord],
    negative_fields: dict[str, dict[str, Any]],
) -> None:
    for negative_id, fields in negative_fields.items():
        negative = by_id.get(negative_id)
        if negative is None:
            raise MetadataEditFailure(
                Code.NEGATIVE_NOT_FOUND,
                f"{negative_id} is not a negative of this roll",
            )
        for key, value in fields.items():
            if key == "capture_date":
                negative.capture_time.date_override = value
            else:
                setattr(negative.metadata, key, value)


def run_metadata_set(
    roll_dir: Path,
    payload: dict[str, Any],
    *,
    emit: Any = None,
) -> dict[str, Any]:
    """Applies one metadata-editing payload and returns the updated
    manifest as a dict — the `metadata_updated` event's `manifest` field.

    The payload has two optional maps:

      {"roll": {"capture_date": "YYYY-MM-DD"|null, "city": ...},
       "negatives": {"<negative_id>": {"capture_date": ..., "city": ...}}}

    A key that is absent leaves that field untouched; a key present with
    `null` or an empty string clears the field (a cleared negative field
    then inherits the roll's fallback again; a cleared capture date removes
    the override so the roll's date applies). Empty strings are accepted
    because the app commits whatever a blurred text field holds, and an
    empty field *means* cleared.

    Raises `MetadataEditFailure` without writing anything when the roll,
    any negative id, field name, or date is no good.
    """
    if not isinstance(payload, dict):
        raise MetadataEditFailure(
            Code.INVALID_METADATA, "the metadata payload must be a JSON object"
        )
    roll_fields = payload.get("roll") or {}
    negative_fields = payload.get("negatives") or {}
    if not isinstance(roll_fields, dict) or not isinstance(negative_fields, dict):
        raise MetadataEditFailure(
            Code.INVALID_METADATA,
            "payload 'roll' and 'negatives' must be JSON objects",
        )
    _validate_field_keys(roll_fields, ROLL_KEYS, "roll")
    for negative_id, fields in negative_fields.items():
        if not isinstance(fields, dict):
            raise MetadataEditFailure(
                Code.INVALID_METADATA,
                f"the fields for negative {negative_id!r} must be a JSON object",
            )
        _validate_field_keys(fields, NEGATIVE_KEYS, f"negative {negative_id!r}")

    # Normalize and validate the whole payload before touching the manifest.
    clean_roll_fields = {
        key: (
            _clean_date(value, f"roll {key}")
            if key == "capture_date"
            else _clean_value(value, f"roll {key}")
        )
        for key, value in roll_fields.items()
    }
    clean_negative_fields: dict[str, dict[str, Any]] = {}
    for negative_id, fields in negative_fields.items():
        clean_negative_fields[negative_id] = {
            key: (
                _clean_date(value, f"negative {negative_id!r} {key}")
                if key == "capture_date"
                else _clean_value(value, f"negative {negative_id!r} {key}")
            )
            for key, value in fields.items()
        }

    roll = _validated_roll(roll_dir)
    by_id = {negative.negative_id: negative for negative in roll.negatives}
    _apply_roll_fields(roll, clean_roll_fields)
    _apply_negative_fields(roll, by_id, clean_negative_fields)

    # The intended timestamps are pure derived state — always the formula's
    # current answer after any date change (and re-derived identically when
    # no date changed).
    apply_intended_times(roll)
    write_roll_manifest(roll_dir, roll)

    _remember_catalog_values(clean_roll_fields, clean_negative_fields)

    return roll.to_dict()


def _remember_catalog_values(
    roll_fields: dict[str, Any], negative_fields: dict[str, dict[str, Any]]
) -> None:
    """Upserts the payload's non-empty extended-metadata values into the
    catalog so the typeahead offers them later. Failures are deliberately
    silent at the call site's level of strictness: the edit is already
    durable, and the catalog is a convenience — but `upsert` only fails on
    a closed database, which would have failed the whole command earlier."""
    for field in CATALOG_FIELDS:
        values = []
        if field in roll_fields and roll_fields[field] is not None:
            values.append(roll_fields[field])
        for fields in negative_fields.values():
            if fields.get(field) is not None:
                values.append(fields[field])
        if values:
            repo.upsert_metadata_values(field, values)


def run_metadata_values(field: str) -> list[str]:
    """The catalog's values for one field, most-recently-used first — the
    `metadata_values` event's list. Raises `MetadataEditFailure` for a
    field the catalog does not track (`caption` included: prose is never
    offered back)."""
    if field not in CATALOG_FIELDS:
        raise MetadataEditFailure(
            Code.INVALID_METADATA,
            f"--field must be one of {', '.join(CATALOG_FIELDS)}, got {field!r}",
        )
    return repo.list_metadata_values(field)
