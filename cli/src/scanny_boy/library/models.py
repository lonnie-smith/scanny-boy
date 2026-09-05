"""SQLAlchemy ORM models for the library database.

One row per domain record, mirroring `roll_manifest.py`'s dataclasses:
`rolls`/`runs`/`sources`/`negatives` carry the fields the app queries or
constrains as real columns, and everything with manifest-schema structure
(`processing_params`, `frames`, `pairs`, `output`, ...) is stored as JSON
text — it is only ever read back whole, never queried piecemeal.

`edits` is the nondestructive editing ops log: an ordered list of operations
per negative, replayed at export time. `negative_id` is the published,
stable identifier from `roll_manifest.format_negative_id`, so an edit
survives re-stitching the negative it belongs to.
"""

from __future__ import annotations

import json
import typing

from sqlalchemy import (
    Float as SQLFloat,
)
from sqlalchemy import (
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

# The extended metadata fields (docs/ARCHITECTURE.md, "extended metadata
# editing"): present on both `rolls` (the roll-level fallback) and
# `negatives` (the explicit per-image value that wins). Everything except
# `caption` is also a `metadata_values` catalog field the typeahead offers.
METADATA_FIELDS = ("city", "state", "camera", "lens", "caption")


class JSONText(TypeDecorator):
    """A TEXT column that transparently serialises JSON values."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: typing.Any, dialect: typing.Any) -> str | None:
        return None if value is None else json.dumps(value, sort_keys=True)

    def process_result_value(
        self, value: str | None, dialect: typing.Any
    ) -> typing.Any:
        return None if value is None else json.loads(value)


class Base(DeclarativeBase):
    pass


class RollRow(Base):
    __tablename__ = "rolls"

    roll_id: Mapped[str] = mapped_column(Text, primary_key=True)
    # The folder the roll was last written to. `roll list` filters on this;
    # `roll rename` updates it when the folder moves.
    folder_path: Mapped[str] = mapped_column(Text, unique=True, index=True)
    roll_name: Mapped[str] = mapped_column(Text)
    scanny_boy_version: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)
    processing_params: Mapped[dict] = mapped_column(JSONText)
    icc_profile: Mapped[dict] = mapped_column(JSONText)
    # The density profile the published TIFFs carry (section 3.12's
    # second-profile split); a roll invariant beside `icc_profile`.
    published_icc_profile: Mapped[dict] = mapped_column(JSONText)
    stitch_params: Mapped[dict] = mapped_column(JSONText)
    # `metadata`: two nullable strings rather than a nested object.
    roll_capture_date: Mapped[str | None] = mapped_column(Text)
    last_applied_at: Mapped[str | None] = mapped_column(Text)
    # The roll-level extended-metadata fallbacks: what every negative
    # without its own explicit value displays and exports. Nullable
    # throughout — pre-0006 rows (and a roll nothing was typed into) are
    # all-NULL.
    city: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(Text)
    camera: Mapped[str | None] = mapped_column(Text)
    lens: Mapped[str | None] = mapped_column(Text)
    caption: Mapped[str | None] = mapped_column(Text)


class RunRow(Base):
    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    roll_id: Mapped[str] = mapped_column(
        Text, ForeignKey("rolls.roll_id", ondelete="CASCADE"), index=True
    )
    # Position in the manifest's `runs` list; restores list order on load.
    ordinal: Mapped[int] = mapped_column(Integer)
    short_id: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    convert_run_id: Mapped[str | None] = mapped_column(Text)
    input_folder: Mapped[str | None] = mapped_column(Text)
    source_order: Mapped[list] = mapped_column(JSONText)
    work_dir: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[str] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)
    # D-4: the per-channel median of the run's negatives' bounds; recorded,
    # nothing reads it yet.
    normalization_aggregate: Mapped[dict | None] = mapped_column(JSONText)


class SourceRow(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    roll_id: Mapped[str] = mapped_column(
        Text, ForeignKey("rolls.roll_id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(Text)
    absolute_path: Mapped[str] = mapped_column(Text)
    size: Mapped[int] = mapped_column(Integer)
    mtime: Mapped[float] = mapped_column(SQLFloat)
    sha256: Mapped[str] = mapped_column(Text)
    run_id: Mapped[str] = mapped_column(Text)
    # Per-channel fraction of pixels at or above sensor white, measured in
    # the prepare stage at decode; null when the contributing run predates
    # the measurement.
    scan_clip_fractions: Mapped[list | None] = mapped_column(JSONText)


class NegativeRow(Base):
    __tablename__ = "negatives"

    negative_id: Mapped[str] = mapped_column(Text, primary_key=True)
    roll_id: Mapped[str] = mapped_column(
        Text, ForeignKey("rolls.roll_id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    run_id: Mapped[str] = mapped_column(Text)
    sequence: Mapped[int | None] = mapped_column(Integer)
    members: Mapped[list] = mapped_column(JSONText)
    expected_output: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    output: Mapped[dict | None] = mapped_column(JSONText)
    frames: Mapped[list] = mapped_column(JSONText)
    pairs: Mapped[list] = mapped_column(JSONText)
    global_rms_px: Mapped[float | None] = mapped_column(SQLFloat)
    canvas: Mapped[dict | None] = mapped_column(JSONText)
    valid_rect: Mapped[list | None] = mapped_column(JSONText)
    fill_color: Mapped[list] = mapped_column(JSONText)
    # The per-negative normalization record and section 3.14's fill value
    # (docs/DECISIONS.md, "Normalization decisions"); null/None when this
    # build predates normalization or the negative never published.
    normalization: Mapped[dict | None] = mapped_column(JSONText)
    normalized_fill: Mapped[float | None] = mapped_column(SQLFloat)
    rebate_deviation_px: Mapped[float | None] = mapped_column(SQLFloat)
    used_clahe_fallback: Mapped[bool] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    capture_time: Mapped[dict] = mapped_column(JSONText)
    # The negative's explicit extended-metadata values. A NULL (or missing,
    # for pre-0006 rows) means "inherit the roll's fallback" — the effective
    # value is the negative's own, else the roll's, per the live-fallback
    # semantics of the extended metadata feature.
    city: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(Text)
    camera: Mapped[str | None] = mapped_column(Text)
    lens: Mapped[str | None] = mapped_column(Text)
    caption: Mapped[str | None] = mapped_column(Text)
    # Set by the preview generator once a small preview of the published
    # TIFF exists; null until then.
    preview_path: Mapped[str | None] = mapped_column(Text)
    # 2D grid stitching (docs/GRID_STITCH_PLAN.md sections 2.4 and 4);
    # null/None for pre-grid rows and for failed assignments.
    grid: Mapped[dict | None] = mapped_column(JSONText)
    grid_cells: Mapped[dict | None] = mapped_column(JSONText)
    grid_pitch_ratio: Mapped[float | None] = mapped_column(SQLFloat)
    grid_alignment_ratio: Mapped[float | None] = mapped_column(SQLFloat)


class MetadataValueRow(Base):
    __tablename__ = "metadata_values"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Which metadata field the value belongs to (`city`, `state`, `camera`,
    # `lens` — never `caption`, which is prose rather than a canonical
    # value). The (field, value) pair is unique: the catalog remembers that
    # a value was used, not how many times.
    field: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    # ISO timestamp of the most recent use — the typeahead's
    # most-recently-used ordering key.
    last_used_at: Mapped[str] = mapped_column(Text, nullable=False)


class EditRow(Base):
    __tablename__ = "edits"
    __table_args__ = (UniqueConstraint("negative_id", "position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    negative_id: Mapped[str] = mapped_column(
        Text, ForeignKey("negatives.negative_id", ondelete="CASCADE"), index=True
    )
    # 1-based position in the negative's ordered ops log; appended, never
    # reordered.
    position: Mapped[int] = mapped_column(Integer)
    op: Mapped[str] = mapped_column(Text)
    params: Mapped[dict] = mapped_column(JSONText)
    created_at: Mapped[str] = mapped_column(Text)


class FlatFieldProfileRow(Base):
    __tablename__ = "flatfield_profiles"

    profile_id: Mapped[str] = mapped_column(Text, primary_key=True)
    # Unique so the app's profile dropdown is unambiguous.
    name: Mapped[str] = mapped_column(Text, unique=True, index=True)
    # The `.npz` beside the library database; provenance only.
    gain_map_path: Mapped[str] = mapped_column(Text)
    gain_map_sha256: Mapped[str] = mapped_column(Text)
    source_path: Mapped[str | None] = mapped_column(Text)
    reference_width: Mapped[int] = mapped_column(Integer)
    reference_height: Mapped[int] = mapped_column(Integer)
    # How the map was built (`flatfield.build_params`).
    params: Mapped[dict] = mapped_column(JSONText)
    scanny_boy_version: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    # Geometric calibration (docs/GEOMETRIC_PLAN.md section 3): all four
    # nullable, and rows from migration 0003 and earlier read back with
    # four Nones and behave exactly as they did before.
    board_key: Mapped[str | None] = mapped_column(Text)
    geometry: Mapped[dict | None] = mapped_column(JSONText)
    chromatic_aberration: Mapped[dict | None] = mapped_column(JSONText)
    calibration_report: Mapped[dict | None] = mapped_column(JSONText)
