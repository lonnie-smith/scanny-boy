"""`scanny-boy-roll.json`: one roll's durable record, format version 3.

A *roll* is a named folder the user returns to, holding the stitched TIFFs of
one roll of film across many runs. That is the whole reason this file is a
break rather than a patch: Phase 2's version 1 carried one `run_id`, one
`input_folder`, and one `film_date`, and refused any rerun that changed them,
and version 2's supersession tombstones are gone in version 3 — a rerun
adopts the covered negative in place instead of publishing a replacement.
See `docs/PHASE3_IMPLEMENTATION_PLAN.md` section 3.3 for the shape and
section 3.4 for the invariants and naming rules this module enforces. There
is no migration: nothing here reads `manifest_format_version: 1` or `2`.

Structurally still a mirror of `manifest.py` — same temp-file / `fsync` /
rename discipline, same hand-written structural validation.
`shared/contract/roll-manifest.schema.json` is the authoritative shape; the
checks here are hand-written for the same reason Phase 1's are: the packaged
CLI must never load a file outside `cli/src/scanny_boy/` at runtime. The
schema file itself is read only by `roll_manifest_schema_test_support.py`.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import os
from pathlib import Path
from typing import Any

from scanny_boy.events import Code
from scanny_boy.icc_profile import PROFILE_FILENAME, PROFILE_SHA256
from scanny_boy.manifest import (
    BadManifestError,
    SourceRecord,
    _looks_like_sha256,
    resolve_within,
)

ROLL_MANIFEST_FILENAME = "scanny-boy-roll.json"
ROLL_MANIFEST_FORMAT_VERSION = 3
ROLL_MANIFEST_KIND = "roll"

STATUSES = {"running", "partial", "cancelled", "complete"}
NEGATIVE_STATUSES = {"pending", "completed", "failed"}
RUN_KINDS = {"run", "stitch"}

# Section 3.4: `short_id` starts at six characters of the run's UUID and
# lengthens until it is free within the roll.
SHORT_ID_LENGTHS = (6, 8, 10)


class RollManifestUnsupportedError(Exception):
    """Maps to `ROLL_MANIFEST_UNSUPPORTED` (section 3.12): the file is a roll
    manifest, but not `manifest_format_version: 3`. Distinct from
    `BadManifestError`, which means unreadable or malformed, so a caller can
    tell "written by an older Scanny Boy" from "corrupt"."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = Code.ROLL_MANIFEST_UNSUPPORTED
        self.message = message


class RollInvariantMismatchError(Exception):
    """Maps to `ROLL_INVARIANT_MISMATCH` (section 3.12): this run's
    parameters differ from the ones the roll already established. Section 3.4
    keeps `MANIFEST_MISMATCH` for the Phase 1 work manifest, so the two never
    share a code."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = Code.ROLL_INVARIANT_MISMATCH
        self.message = message


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None).isoformat() + "Z"


@dataclasses.dataclass(frozen=True)
class PairRecord:
    a: str
    b: str
    inliers: int
    good_matches: int
    inlier_ratio: float
    rms_residual_px: float
    scale_drift: float
    overlap_fraction: float | None
    overlap_mad: float | None
    accepted: bool

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class FrameRecord:
    name: str
    rotation_deg: float
    translation: tuple[float, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "rotation_deg": self.rotation_deg,
            "translation": list(self.translation),
        }


@dataclasses.dataclass(frozen=True)
class RollSourceRecord:
    """Section 3.3: "as Phase 2, plus `run_id` naming the run that first
    contributed it". Phase 1's `SourceRecord` is shared with the work
    manifest and must not grow a field, so the roll keeps its own record
    (section 5.4)."""

    filename: str
    absolute_path: str
    size: int
    mtime: float
    sha256: str
    run_id: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class CaptureTime:
    """Section 3.3. `source_datetime_original` is what the negative's first
    frame actually carries; `intended_` is what the metadata stage wants;
    `applied_` is what was last written into the published TIFF. A negative
    is *dirty* when the last two differ (section 3.8)."""

    source_datetime_original: str | None = None
    intended_datetime_original: str | None = None
    applied_datetime_original: str | None = None
    date_override: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class RunRecord:
    """Section 3.3. One invocation of `run` or `stitch` that added negatives
    to this roll. `short_id` is assigned by `append_run` and never
    recomputed, so `negative_id`s are stable for the life of the roll."""

    run_id: str
    kind: str
    status: str
    started_at: str
    short_id: str = ""
    convert_run_id: str | None = None
    input_folder: str | None = None
    source_order: list[str] = dataclasses.field(default_factory=list)
    work_dir: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "short_id": self.short_id,
            "kind": self.kind,
            "status": self.status,
            "convert_run_id": self.convert_run_id,
            "input_folder": self.input_folder,
            "source_order": self.source_order,
            "work_dir": self.work_dir,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclasses.dataclass
class NegativeRecord:
    negative_id: str
    run_id: str
    members: list[str]
    expected_output: str
    fill_color: tuple[int, int, int]
    status: str = "pending"
    # Section 3.7: the 1-based position in the roll, recomputed by
    # `roll_sequence.py` (Chunk P3-6). Null while unranked (pending/failed).
    sequence: int | None = None
    capture_time: CaptureTime = dataclasses.field(default_factory=CaptureTime)
    # `{name, size, sha256, width, height}`; a plain dict rather than a
    # dataclass because Phase 1's `OutputRecord` has no dimensions and this
    # module's dataclass list is fixed by the plan.
    output: dict[str, Any] | None = None
    frames: list[FrameRecord] = dataclasses.field(default_factory=list)
    pairs: list[PairRecord] = dataclasses.field(default_factory=list)
    global_rms_px: float | None = None
    canvas: tuple[int, int] | None = None  # (width, height)
    valid_rect: tuple[int, int, int, int] | None = None
    # Phase 2 section 3.12.2: never set, because Chunk P2-1 found the rebate
    # is not cleanly detectable with a generic straight-edge finder. The
    # field stays in the contract; its value is always null.
    rebate_deviation_px: float | None = None
    # Whether registration needed the CLAHE retry (stitch_pipeline.py's
    # `_solve_negative`) to solve this negative's layout — section 3.7's
    # "every threshold in force" promise extended to a per-negative choice,
    # since the roll-level `stitch_params` records the fallback as a fixed
    # policy, not which negatives actually used it.
    used_clahe_fallback: bool = False
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "negative_id": self.negative_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "members": self.members,
            "expected_output": self.expected_output,
            "status": self.status,
            "output": self.output,
            "frames": [f.to_dict() for f in self.frames],
            "pairs": [p.to_dict() for p in self.pairs],
            "global_rms_px": self.global_rms_px,
            "canvas": (
                None
                if self.canvas is None
                else {"width": self.canvas[0], "height": self.canvas[1]}
            ),
            "valid_rect": None if self.valid_rect is None else list(self.valid_rect),
            "fill_color": list(self.fill_color),
            "rebate_deviation_px": self.rebate_deviation_px,
            "used_clahe_fallback": self.used_clahe_fallback,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "capture_time": self.capture_time.to_dict(),
        }


@dataclasses.dataclass
class RollMetadata:
    roll_capture_date: str | None = None
    last_applied_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class RollInvariants:
    """Section 3.4's roll-invariant set, and section 5.4's name for it.
    Everything else — input folder, source list, order, grouping — is
    expected to differ between runs and is never compared."""

    shots_per_negative: int
    processing_params: dict[str, Any]
    icc_profile_sha256: str
    stitch_params: dict[str, Any]


@dataclasses.dataclass
class RollManifest:
    scanny_boy_version: str
    roll_id: str
    roll_name: str
    shots_per_negative: int
    created_at: str
    updated_at: str
    processing_params: dict[str, Any] = dataclasses.field(default_factory=dict)
    icc_profile: dict[str, str] = dataclasses.field(default_factory=dict)
    stitch_params: dict[str, Any] = dataclasses.field(default_factory=dict)
    runs: list[RunRecord] = dataclasses.field(default_factory=list)
    sources: list[RollSourceRecord] = dataclasses.field(default_factory=list)
    negatives: list[NegativeRecord] = dataclasses.field(default_factory=list)
    metadata: RollMetadata = dataclasses.field(default_factory=RollMetadata)
    manifest_format_version: int = ROLL_MANIFEST_FORMAT_VERSION
    manifest_kind: str = ROLL_MANIFEST_KIND

    def negative(self, negative_id: str) -> NegativeRecord:
        for n in self.negatives:
            if n.negative_id == negative_id:
                return n
        raise KeyError(negative_id)

    def run(self, run_id: str) -> RunRecord:
        for r in self.runs:
            if r.run_id == run_id:
                return r
        raise KeyError(run_id)

    def all_expected_outputs(self) -> list[str]:
        return [n.expected_output for n in self.negatives]

    def invariants(self) -> RollInvariants:
        return RollInvariants(
            shots_per_negative=self.shots_per_negative,
            processing_params=self.processing_params,
            icc_profile_sha256=self.icc_profile.get("sha256", ""),
            stitch_params=self.stitch_params,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_format_version": self.manifest_format_version,
            "manifest_kind": self.manifest_kind,
            "scanny_boy_version": self.scanny_boy_version,
            "roll_id": self.roll_id,
            "roll_name": self.roll_name,
            "shots_per_negative": self.shots_per_negative,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "processing_params": self.processing_params,
            "icc_profile": self.icc_profile,
            "stitch_params": self.stitch_params,
            "runs": [r.to_dict() for r in self.runs],
            "sources": [s.to_dict() for s in self.sources],
            "negatives": [n.to_dict() for n in self.negatives],
            "metadata": self.metadata.to_dict(),
        }


def new_roll_manifest(
    *, roll_id: str, roll_name: str, shots_per_negative: int
) -> RollManifest:
    """Section 5.4 decision 1: the one constructor of an empty roll. No runs,
    no sources, no negatives.

    `icc_profile` is seeded from the bundled profile's compile-time
    constants, because there is exactly one profile and section 3.4 makes its
    hash a roll invariant. `processing_params` and `stitch_params` stay empty
    — they are established by the first run, and `check_roll_invariants`
    knows not to compare them until then.
    """
    from scanny_boy.manifest import current_scanny_boy_version

    now = _now_iso()
    return RollManifest(
        scanny_boy_version=current_scanny_boy_version(),
        roll_id=roll_id,
        roll_name=roll_name,
        shots_per_negative=shots_per_negative,
        created_at=now,
        updated_at=now,
        processing_params={},
        icc_profile={"name": PROFILE_FILENAME, "sha256": PROFILE_SHA256},
        stitch_params={},
    )


def current_roll_manifest_path(output_dir: Path) -> Path:
    return output_dir / ROLL_MANIFEST_FILENAME


def write_roll_manifest(output_dir: Path, manifest: RollManifest) -> None:
    """Write to a temporary file, `fsync` it, then rename it into place, so
    readers never see a half-written manifest. `fsync`s the directory
    afterward where the platform permits it. Identical discipline to
    `manifest.write_manifest`.

    Section 3.3/3.7: `updated_at` is rewritten and every negative's
    `sequence` is recomputed on every write, so this mutates the manifest
    it is given. The import is local to avoid a circular import: this
    module builds `RollManifest`, and `roll_sequence` reads it."""
    from scanny_boy.roll_sequence import sequence_negatives

    manifest.updated_at = _now_iso()
    rank_by_id = {
        negative_id: rank for rank, negative_id in enumerate(sequence_negatives(manifest), start=1)
    }
    for negative in manifest.negatives:
        negative.sequence = rank_by_id.get(negative.negative_id)

    final_path = current_roll_manifest_path(output_dir)
    tmp_path = final_path.with_suffix(final_path.suffix + ".tmp")
    data = json.dumps(manifest.to_dict(), indent=2, sort_keys=True)

    with tmp_path.open("w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp_path, final_path)

    try:
        dir_fd = os.open(output_dir, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def estimate_roll_manifest_size(manifest: RollManifest) -> int:
    return len(json.dumps(manifest.to_dict()).encode("utf-8"))


# --- Structural (schema) validation -------------------------------------


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BadManifestError(message)


def _validate_source_dict(data: Any) -> None:
    _require(isinstance(data, dict), "source entry is not an object")
    for key in ("filename", "absolute_path", "size", "mtime", "sha256", "run_id"):
        _require(key in data, f"source entry missing {key!r}")
    _require(isinstance(data["filename"], str), "source filename is not a string")
    _require(
        isinstance(data["absolute_path"], str), "source absolute_path is not a string"
    )
    _require(isinstance(data["size"], int) and data["size"] >= 0, "source size is invalid")
    _require(isinstance(data["mtime"], int | float), "source mtime is invalid")
    _require(_looks_like_sha256(data["sha256"]), "source sha256 is invalid")
    _require(isinstance(data["run_id"], str), "source run_id is not a string")


def _validate_stitched_output_dict(data: Any) -> None:
    _require(isinstance(data, dict), "output entry is not an object")
    for key in ("name", "size", "sha256", "width", "height"):
        _require(key in data, f"output entry missing {key!r}")
    _require(isinstance(data["name"], str), "output name is not a string")
    _require(isinstance(data["size"], int) and data["size"] >= 0, "output size is invalid")
    _require(_looks_like_sha256(data["sha256"]), "output sha256 is invalid")
    _require(isinstance(data["width"], int) and data["width"] >= 1, "output width is invalid")
    _require(
        isinstance(data["height"], int) and data["height"] >= 1, "output height is invalid"
    )


def _validate_frame_dict(data: Any) -> None:
    _require(isinstance(data, dict), "frame entry is not an object")
    for key in ("name", "rotation_deg", "translation"):
        _require(key in data, f"frame entry missing {key!r}")
    _require(isinstance(data["name"], str), "frame name is not a string")
    _require(isinstance(data["rotation_deg"], int | float), "frame rotation_deg is invalid")
    translation = data["translation"]
    _require(
        isinstance(translation, list)
        and len(translation) == 2
        and all(isinstance(v, int | float) for v in translation),
        "frame translation is invalid",
    )


def _validate_pair_dict(data: Any) -> None:
    _require(isinstance(data, dict), "pair entry is not an object")
    for key in (
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
    ):
        _require(key in data, f"pair entry missing {key!r}")
    _require(isinstance(data["a"], str) and isinstance(data["b"], str), "pair names invalid")
    _require(
        isinstance(data["inliers"], int) and data["inliers"] >= 0, "pair inliers is invalid"
    )
    _require(
        isinstance(data["good_matches"], int) and data["good_matches"] >= 0,
        "pair good_matches is invalid",
    )
    for key in ("inlier_ratio", "rms_residual_px", "scale_drift"):
        _require(isinstance(data[key], int | float), f"pair {key} is invalid")
    for key in ("overlap_fraction", "overlap_mad"):
        _require(
            data[key] is None or isinstance(data[key], int | float),
            f"pair {key} is invalid",
        )
    _require(isinstance(data["accepted"], bool), "pair accepted is not a boolean")


def _validate_capture_time_dict(data: Any) -> None:
    _require(isinstance(data, dict), "capture_time is not an object")
    for key in (
        "source_datetime_original",
        "intended_datetime_original",
        "applied_datetime_original",
        "date_override",
    ):
        _require(key in data, f"capture_time missing {key!r}")
        _require(
            data[key] is None or isinstance(data[key], str),
            f"capture_time {key} is invalid",
        )


def _validate_run_dict(data: Any) -> None:
    _require(isinstance(data, dict), "run entry is not an object")
    for key in (
        "run_id",
        "short_id",
        "kind",
        "status",
        "convert_run_id",
        "input_folder",
        "source_order",
        "work_dir",
        "started_at",
        "finished_at",
    ):
        _require(key in data, f"run entry missing {key!r}")
    _require(isinstance(data["run_id"], str), "run run_id is not a string")
    _require(
        isinstance(data["short_id"], str) and data["short_id"], "run short_id is invalid"
    )
    _require(data["kind"] in RUN_KINDS, f"invalid run kind {data['kind']!r}")
    _require(data["status"] in STATUSES, f"invalid run status {data['status']!r}")
    for key in ("convert_run_id", "input_folder", "work_dir", "finished_at"):
        _require(
            data[key] is None or isinstance(data[key], str), f"run {key} is invalid"
        )
    _require(isinstance(data["started_at"], str), "run started_at is not a string")
    _require(
        isinstance(data["source_order"], list)
        and all(isinstance(v, str) for v in data["source_order"]),
        "run source_order is invalid",
    )


def _validate_negative_dict(data: Any) -> None:
    _require(isinstance(data, dict), "negative entry is not an object")
    for key in (
        "negative_id",
        "run_id",
        "sequence",
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
        "used_clahe_fallback",
        "error_code",
        "error_message",
        "capture_time",
    ):
        _require(key in data, f"negative entry missing {key!r}")

    _require(isinstance(data["negative_id"], str), "negative_id is not a string")
    _require(isinstance(data["run_id"], str), "negative run_id is not a string")
    _require(
        data["sequence"] is None
        or (isinstance(data["sequence"], int) and data["sequence"] >= 1),
        "negative sequence is invalid",
    )
    _require(
        isinstance(data["members"], list) and data["members"], "negative members is invalid"
    )
    _require(isinstance(data["expected_output"], str), "expected_output is not a string")
    _require(
        data["status"] in NEGATIVE_STATUSES, f"invalid negative status {data['status']!r}"
    )

    if data["output"] is not None:
        _validate_stitched_output_dict(data["output"])

    _require(isinstance(data["frames"], list), "negative frames is not a list")
    for frame in data["frames"]:
        _validate_frame_dict(frame)

    _require(isinstance(data["pairs"], list), "negative pairs is not a list")
    for pair in data["pairs"]:
        _validate_pair_dict(pair)

    _require(
        data["global_rms_px"] is None or isinstance(data["global_rms_px"], int | float),
        "global_rms_px is invalid",
    )

    canvas = data["canvas"]
    if canvas is not None:
        _require(isinstance(canvas, dict), "canvas is not an object")
        for key in ("width", "height"):
            _require(key in canvas, f"canvas missing {key!r}")
            _require(
                isinstance(canvas[key], int) and canvas[key] >= 1, f"canvas {key} is invalid"
            )

    valid_rect = data["valid_rect"]
    if valid_rect is not None:
        _require(
            isinstance(valid_rect, list)
            and len(valid_rect) == 4
            and all(isinstance(v, int) for v in valid_rect),
            "valid_rect is invalid",
        )

    fill_color = data["fill_color"]
    _require(
        isinstance(fill_color, list)
        and len(fill_color) == 3
        and all(isinstance(v, int) and 0 <= v <= 255 for v in fill_color),
        "fill_color is invalid",
    )

    _require(
        data["rebate_deviation_px"] is None
        or isinstance(data["rebate_deviation_px"], int | float),
        "rebate_deviation_px is invalid",
    )

    _require(
        isinstance(data["used_clahe_fallback"], bool), "used_clahe_fallback is invalid"
    )

    _validate_capture_time_dict(data["capture_time"])


_TOP_LEVEL_REQUIRED = (
    "manifest_format_version",
    "manifest_kind",
    "scanny_boy_version",
    "roll_id",
    "roll_name",
    "shots_per_negative",
    "created_at",
    "updated_at",
    "processing_params",
    "icc_profile",
    "stitch_params",
    "runs",
    "sources",
    "negatives",
    "metadata",
)


def validate_roll_manifest_dict(data: Any) -> None:
    """Structural checks mirroring `roll-manifest.schema.json`. Raises
    `BadManifestError` on the first problem found, or
    `RollManifestUnsupportedError` for a manifest that is well-formed but not
    format version 3 — section 0 is explicit that there is no migration."""
    _require(isinstance(data, dict), "roll manifest is not a JSON object")
    for key in _TOP_LEVEL_REQUIRED:
        _require(key in data, f"roll manifest missing required field {key!r}")

    if data["manifest_format_version"] != ROLL_MANIFEST_FORMAT_VERSION:
        raise RollManifestUnsupportedError(
            f"unsupported manifest_format_version "
            f"{data['manifest_format_version']!r}; this build reads only "
            f"version {ROLL_MANIFEST_FORMAT_VERSION}"
        )
    _require(
        data["manifest_kind"] == ROLL_MANIFEST_KIND,
        f"unsupported manifest_kind {data['manifest_kind']!r}",
    )
    _require(isinstance(data["roll_id"], str) and data["roll_id"], "roll_id is invalid")
    _require(isinstance(data["roll_name"], str), "roll_name is not a string")
    _require(
        isinstance(data["shots_per_negative"], int)
        and 1 <= data["shots_per_negative"] <= 12,
        "shots_per_negative is invalid",
    )
    for key in ("created_at", "updated_at", "scanny_boy_version"):
        _require(isinstance(data[key], str), f"{key} is not a string")
    _require(isinstance(data["processing_params"], dict), "processing_params is not an object")
    _require(isinstance(data["stitch_params"], dict), "stitch_params is not an object")
    icc = data["icc_profile"]
    _require(
        isinstance(icc, dict) and "name" in icc and "sha256" in icc, "icc_profile is invalid"
    )
    _require(_looks_like_sha256(icc["sha256"]), "icc_profile sha256 is invalid")

    _require(isinstance(data["runs"], list), "runs is not a list")
    for run in data["runs"]:
        _validate_run_dict(run)

    _require(isinstance(data["sources"], list), "sources is not a list")
    for source in data["sources"]:
        _validate_source_dict(source)

    _require(isinstance(data["negatives"], list), "negatives is not a list")
    for negative in data["negatives"]:
        _validate_negative_dict(negative)

    metadata = data["metadata"]
    _require(isinstance(metadata, dict), "metadata is not an object")
    for key in ("roll_capture_date", "last_applied_at"):
        _require(key in metadata, f"metadata missing {key!r}")
        _require(
            metadata[key] is None or isinstance(metadata[key], str),
            f"metadata {key} is invalid",
        )


def _validate_output_paths_within(output_dir: Path, manifest: RollManifest) -> None:
    for negative in manifest.negatives:
        names = [negative.expected_output]
        if negative.output is not None:
            names.append(negative.output["name"])
        for name in names:
            try:
                resolve_within(output_dir, name)
            except ValueError as exc:
                raise BadManifestError(str(exc)) from exc


def load_roll_manifest(output_dir: Path) -> RollManifest:
    """Read and structurally validate the roll manifest in `output_dir`.
    Raises `BadManifestError` if it is missing, unreadable, not valid JSON,
    fails the structural checks above, or names an output that escapes
    `output_dir`; `RollManifestUnsupportedError` if it is not version 3."""
    path = current_roll_manifest_path(output_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BadManifestError(f"could not read {path}: {exc}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BadManifestError(f"{path} is not valid JSON: {exc}") from exc

    validate_roll_manifest_dict(data)
    manifest = _roll_manifest_from_dict(data)
    _validate_output_paths_within(output_dir, manifest)
    return manifest


def _roll_manifest_from_dict(data: dict[str, Any]) -> RollManifest:
    return RollManifest(
        manifest_format_version=data["manifest_format_version"],
        manifest_kind=data["manifest_kind"],
        scanny_boy_version=data["scanny_boy_version"],
        roll_id=data["roll_id"],
        roll_name=data["roll_name"],
        shots_per_negative=data["shots_per_negative"],
        created_at=data["created_at"],
        updated_at=data["updated_at"],
        processing_params=data["processing_params"],
        icc_profile=data["icc_profile"],
        stitch_params=data["stitch_params"],
        runs=[
            RunRecord(
                run_id=r["run_id"],
                short_id=r["short_id"],
                kind=r["kind"],
                status=r["status"],
                convert_run_id=r["convert_run_id"],
                input_folder=r["input_folder"],
                source_order=list(r["source_order"]),
                work_dir=r["work_dir"],
                started_at=r["started_at"],
                finished_at=r["finished_at"],
            )
            for r in data["runs"]
        ],
        sources=[
            RollSourceRecord(
                filename=s["filename"],
                absolute_path=s["absolute_path"],
                size=s["size"],
                mtime=s["mtime"],
                sha256=s["sha256"],
                run_id=s["run_id"],
            )
            for s in data["sources"]
        ],
        negatives=[
            NegativeRecord(
                negative_id=n["negative_id"],
                run_id=n["run_id"],
                sequence=n["sequence"],
                members=list(n["members"]),
                expected_output=n["expected_output"],
                fill_color=tuple(n["fill_color"]),
                status=n["status"],
                output=n["output"],
                frames=[
                    FrameRecord(
                        name=f["name"],
                        rotation_deg=f["rotation_deg"],
                        translation=(f["translation"][0], f["translation"][1]),
                    )
                    for f in n["frames"]
                ],
                pairs=[
                    PairRecord(
                        a=p["a"],
                        b=p["b"],
                        inliers=p["inliers"],
                        good_matches=p["good_matches"],
                        inlier_ratio=p["inlier_ratio"],
                        rms_residual_px=p["rms_residual_px"],
                        scale_drift=p["scale_drift"],
                        overlap_fraction=p["overlap_fraction"],
                        overlap_mad=p["overlap_mad"],
                        accepted=p["accepted"],
                    )
                    for p in n["pairs"]
                ],
                global_rms_px=n["global_rms_px"],
                canvas=(
                    None
                    if n["canvas"] is None
                    else (n["canvas"]["width"], n["canvas"]["height"])
                ),
                valid_rect=(
                    None if n["valid_rect"] is None else tuple(n["valid_rect"])
                ),
                rebate_deviation_px=n["rebate_deviation_px"],
                used_clahe_fallback=n["used_clahe_fallback"],
                error_code=n["error_code"],
                error_message=n["error_message"],
                capture_time=CaptureTime(**n["capture_time"]),
            )
            for n in data["negatives"]
        ],
        metadata=RollMetadata(**data["metadata"]),
    )


# --- Section 3.4: invariants, additive runs, naming -----------------------


def check_roll_invariants(
    manifest: RollManifest, candidate_params: RollInvariants
) -> None:
    """Section 3.4's roll-invariant check, replacing Phase 2's
    `check_roll_rerun_matches` entirely. Input folder, source list, order,
    and grouping are *expected* to differ between runs and are never
    compared.

    Section 5.4: `shots_per_negative` is set at roll creation and is always
    compared. The other three are established by the first run, so a roll
    with no runs yet is unseeded and passes; the caller then assigns them.
    This function never mutates. Raises `RollInvariantMismatchError`.
    """
    if manifest.shots_per_negative != candidate_params.shots_per_negative:
        raise RollInvariantMismatchError(
            f"this run stitches {candidate_params.shots_per_negative} shots per "
            f"negative, but the roll is {manifest.shots_per_negative}"
        )

    if not manifest.runs:
        return

    if manifest.processing_params != candidate_params.processing_params:
        raise RollInvariantMismatchError(
            "this run's processing settings differ from the roll's"
        )
    if manifest.icc_profile.get("sha256") != candidate_params.icc_profile_sha256:
        raise RollInvariantMismatchError(
            "this run's ICC profile differs from the roll's"
        )
    if manifest.stitch_params != candidate_params.stitch_params:
        raise RollInvariantMismatchError(
            "this run's stitch settings differ from the roll's"
        )


def append_run(manifest: RollManifest, run: RunRecord) -> None:
    """Append `run` to the roll, assigning its `short_id` per section 3.4.

    `run_id` is a UUID, so six hex characters can collide between two runs
    of one roll. Lengthen to eight, then ten, then the whole `run_id`, until
    the value is free. The chosen value is stored on the record and never
    recomputed, so `negative_id`s are stable for the life of the roll.
    """
    taken = {r.short_id for r in manifest.runs}
    for length in SHORT_ID_LENGTHS:
        candidate = run.run_id[:length]
        if candidate not in taken:
            run.short_id = candidate
            break
    else:
        run.short_id = run.run_id
    manifest.runs.append(run)


def merge_sources(
    manifest: RollManifest, sources: list[SourceRecord], run_id: str
) -> None:
    """Section 3.3: `sources` is keyed by `sha256`. A file already present is
    never appended twice, even from a different folder or under a different
    name, and keeps the `run_id` of the run that *first* contributed it."""
    known = {s.sha256 for s in manifest.sources}
    for source in sources:
        if source.sha256 in known:
            continue
        known.add(source.sha256)
        manifest.sources.append(
            RollSourceRecord(
                filename=source.filename,
                absolute_path=source.absolute_path,
                size=source.size,
                mtime=source.mtime,
                sha256=source.sha256,
                run_id=run_id,
            )
        )


def format_negative_id(short_id: str, index: int) -> str:
    """Section 3.4: `<run.short_id>-negative-NN`, `NN` being the existing
    per-run two-digit index."""
    return f"{short_id}-negative-{index:02d}"


def _claimed_output_names(
    manifest: RollManifest, negative_id: str, adoptable: set[str] | None = None
) -> set[str]:
    """Every name held by some *other* negative, minus `adoptable` — names
    the current group is about to adopt or free, so they are available."""
    claimed: set[str] = set()
    for n in manifest.negatives:
        if n.negative_id == negative_id:
            continue
        claimed.add(n.expected_output)
        if n.output is not None:
            claimed.add(n.output["name"])
    return claimed - (adoptable or set())


def allocate_output_name(
    manifest: RollManifest, first_member: str, negative_id: str, adoptable: set[str] | None = None
) -> str:
    """Section 3.4's output-naming rule, and the **only** place a published
    name is chosen.

    Phase 2's rule unchanged — the stem of the group's first member in
    canonical order, plus `.tif` — with one addition: if that name is already
    claimed by a *different* `negative_id`, append `-2`, `-3`, … until free.
    `adoptable` names names of negatives the current group covers, which this
    run is about to adopt or remove, so they count as free. Re-stitching an
    adopted negative keeps its existing name, which the pipeline reuses
    rather than re-allocating.
    """
    claimed = _claimed_output_names(manifest, negative_id, adoptable)
    stem = Path(first_member).stem
    candidate = f"{stem}.tif"
    suffix = 1
    while candidate in claimed:
        suffix += 1
        candidate = f"{stem}-{suffix}.tif"
    return candidate
