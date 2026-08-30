"""`scanny-boy-roll.json`: the output folder's record of one stitched roll.

Structurally a mirror of `manifest.py` — same temp-file / `fsync` / rename
discipline, same hand-written structural validation, same rerun-mismatch
comparison. `shared/contract/roll-manifest.schema.json` is the authoritative
shape; the checks here are hand-written rather than schema-driven for the
same reason Phase 1's are: the packaged CLI must never load a file outside
`cli/src/scanny_boy/` at runtime. The schema file itself is read only by
`roll_manifest_schema_test_support.py`.

See `docs/PHASE2_IMPLEMENTATION_PLAN.md` section 3.7. Phase 1's
`scanny-boy-manifest.json` is neither renamed nor changed; it simply now
lives in the work directory, and this is the file the user actually keeps.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any

from scanny_boy.manifest import (
    BadManifestError,
    ManifestMismatchError,
    SourceRecord,
    _looks_like_sha256,
    resolve_within,
)

ROLL_MANIFEST_FILENAME = "scanny-boy-roll.json"
ROLL_MANIFEST_FORMAT_VERSION = 1
ROLL_MANIFEST_KIND = "stitch"

STATUSES = {"running", "partial", "cancelled", "complete"}
NEGATIVE_STATUSES = {"pending", "completed", "failed"}


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


@dataclasses.dataclass
class NegativeRecord:
    negative_id: str
    members: list[str]
    expected_output: str
    fill_color: tuple[int, int, int]
    status: str = "pending"
    # `{name, size, sha256, width, height}`; a plain dict rather than a
    # dataclass because Phase 1's `OutputRecord` has no dimensions and this
    # module's dataclass list is fixed by the plan.
    output: dict[str, Any] | None = None
    frames: list[FrameRecord] = dataclasses.field(default_factory=list)
    pairs: list[PairRecord] = dataclasses.field(default_factory=list)
    global_rms_px: float | None = None
    canvas: tuple[int, int] | None = None  # (width, height)
    valid_rect: tuple[int, int, int, int] | None = None
    # Section 3.12.2: never set in Phase 2, because Chunk P2-1 found the
    # rebate is not cleanly detectable with a generic straight-edge finder.
    # The field stays in the contract; its value is always null.
    rebate_deviation_px: float | None = None
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "negative_id": self.negative_id,
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
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


@dataclasses.dataclass
class RollManifest:
    scanny_boy_version: str
    run_id: str
    status: str
    input_folder: str
    film_date: str
    shots_per_negative: int
    convert_run_id: str
    processing_params: dict[str, Any]
    icc_profile: dict[str, str]
    stitch_params: dict[str, Any]
    source_order: list[str]
    sources: list[SourceRecord]
    negatives: list[NegativeRecord]
    started_at: str
    finished_at: str | None = None
    manifest_format_version: int = ROLL_MANIFEST_FORMAT_VERSION
    manifest_kind: str = ROLL_MANIFEST_KIND

    def negative(self, negative_id: str) -> NegativeRecord:
        for n in self.negatives:
            if n.negative_id == negative_id:
                return n
        raise KeyError(negative_id)

    def all_expected_outputs(self) -> list[str]:
        return [n.expected_output for n in self.negatives]

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_format_version": self.manifest_format_version,
            "manifest_kind": self.manifest_kind,
            "scanny_boy_version": self.scanny_boy_version,
            "run_id": self.run_id,
            "status": self.status,
            "input_folder": self.input_folder,
            "film_date": self.film_date,
            "shots_per_negative": self.shots_per_negative,
            "convert_run_id": self.convert_run_id,
            "processing_params": self.processing_params,
            "icc_profile": self.icc_profile,
            "stitch_params": self.stitch_params,
            "source_order": self.source_order,
            "sources": [s.to_dict() for s in self.sources],
            "negatives": [n.to_dict() for n in self.negatives],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def current_roll_manifest_path(output_dir: Path) -> Path:
    return output_dir / ROLL_MANIFEST_FILENAME


def write_roll_manifest(output_dir: Path, manifest: RollManifest) -> None:
    """Write to a temporary file, `fsync` it, then rename it into place, so
    readers never see a half-written manifest. `fsync`s the directory
    afterward where the platform permits it. Identical discipline to
    `manifest.write_manifest`."""
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
    for key in ("filename", "absolute_path", "size", "mtime", "sha256"):
        _require(key in data, f"source entry missing {key!r}")
    _require(isinstance(data["filename"], str), "source filename is not a string")
    _require(
        isinstance(data["absolute_path"], str), "source absolute_path is not a string"
    )
    _require(isinstance(data["size"], int) and data["size"] >= 0, "source size is invalid")
    _require(isinstance(data["mtime"], int | float), "source mtime is invalid")
    _require(_looks_like_sha256(data["sha256"]), "source sha256 is invalid")


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


def _validate_negative_dict(data: Any) -> None:
    _require(isinstance(data, dict), "negative entry is not an object")
    for key in (
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
    ):
        _require(key in data, f"negative entry missing {key!r}")

    _require(isinstance(data["negative_id"], str), "negative_id is not a string")
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


_TOP_LEVEL_REQUIRED = (
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
)


def validate_roll_manifest_dict(data: Any) -> None:
    """Structural checks mirroring `roll-manifest.schema.json`. Raises
    `BadManifestError` on the first problem found."""
    _require(isinstance(data, dict), "roll manifest is not a JSON object")
    for key in _TOP_LEVEL_REQUIRED:
        _require(key in data, f"roll manifest missing required field {key!r}")

    _require(
        data["manifest_format_version"] == ROLL_MANIFEST_FORMAT_VERSION,
        f"unsupported manifest_format_version {data['manifest_format_version']!r}",
    )
    _require(
        data["manifest_kind"] == ROLL_MANIFEST_KIND,
        f"unsupported manifest_kind {data['manifest_kind']!r}",
    )
    _require(data["status"] in STATUSES, f"invalid roll manifest status {data['status']!r}")
    _require(
        isinstance(data["shots_per_negative"], int), "shots_per_negative is not an integer"
    )
    _require(isinstance(data["convert_run_id"], str), "convert_run_id is not a string")
    _require(isinstance(data["processing_params"], dict), "processing_params is not an object")
    _require(isinstance(data["stitch_params"], dict), "stitch_params is not an object")
    icc = data["icc_profile"]
    _require(
        isinstance(icc, dict) and "name" in icc and "sha256" in icc, "icc_profile is invalid"
    )
    _require(_looks_like_sha256(icc["sha256"]), "icc_profile sha256 is invalid")
    _require(isinstance(data["source_order"], list), "source_order is not a list")
    _require(isinstance(data["sources"], list), "sources is not a list")
    for source in data["sources"]:
        _validate_source_dict(source)
    _require(isinstance(data["negatives"], list), "negatives is not a list")
    for negative in data["negatives"]:
        _validate_negative_dict(negative)


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
    `output_dir`."""
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
        run_id=data["run_id"],
        status=data["status"],
        input_folder=data["input_folder"],
        film_date=data["film_date"],
        shots_per_negative=data["shots_per_negative"],
        convert_run_id=data["convert_run_id"],
        processing_params=data["processing_params"],
        icc_profile=data["icc_profile"],
        stitch_params=data["stitch_params"],
        source_order=list(data["source_order"]),
        sources=[
            SourceRecord(
                filename=s["filename"],
                absolute_path=s["absolute_path"],
                size=s["size"],
                mtime=s["mtime"],
                sha256=s["sha256"],
            )
            for s in data["sources"]
        ],
        negatives=[
            NegativeRecord(
                negative_id=n["negative_id"],
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
                error_code=n["error_code"],
                error_message=n["error_message"],
            )
            for n in data["negatives"]
        ],
        started_at=data["started_at"],
        finished_at=data["finished_at"],
    )


# --- Rerun-mismatch comparison -------------------------------------------


def check_roll_rerun_matches(existing: RollManifest, candidate: RollManifest) -> None:
    """Section 3.7: output-folder rules are Phase 1's rules applied to the
    roll manifest. Mirrors `manifest.check_rerun_matches` field for field,
    over the roll manifest's own shape. Raises `ManifestMismatchError`
    naming the first field that differs; `run_id`, `status`, and timing
    fields are expected to differ and are not compared."""
    if existing.source_order != candidate.source_order:
        raise ManifestMismatchError(
            "the selection's source order differs from the previous run "
            f"recorded in {ROLL_MANIFEST_FILENAME}"
        )

    existing_hashes = {s.filename: s.sha256 for s in existing.sources}
    candidate_hashes = {s.filename: s.sha256 for s in candidate.sources}
    if existing_hashes != candidate_hashes:
        raise ManifestMismatchError(
            "one or more source files' hashes differ from the previous run "
            f"recorded in {ROLL_MANIFEST_FILENAME}"
        )

    if existing.shots_per_negative != candidate.shots_per_negative:
        raise ManifestMismatchError(
            f"shots per negative changed from {existing.shots_per_negative} "
            f"to {candidate.shots_per_negative} since the previous run"
        )

    existing_negatives = [(n.negative_id, n.members) for n in existing.negatives]
    candidate_negatives = [(n.negative_id, n.members) for n in candidate.negatives]
    if existing_negatives != candidate_negatives:
        raise ManifestMismatchError("negative grouping differs from the previous run")

    if existing.icc_profile.get("sha256") != candidate.icc_profile.get("sha256"):
        raise ManifestMismatchError("the ICC profile differs from the previous run")

    if existing.film_date != candidate.film_date:
        raise ManifestMismatchError(
            f"film date changed from {existing.film_date} to {candidate.film_date} "
            "since the previous run"
        )

    if existing.processing_params != candidate.processing_params:
        raise ManifestMismatchError("processing settings differ from the previous run")
