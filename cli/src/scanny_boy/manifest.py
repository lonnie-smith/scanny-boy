"""`scanny-boy-manifest.json`: data model, atomic read/write, structural
validation, and rerun-mismatch comparison.

See `docs/IMPLEMENTATION_PLAN.md` section 3.6 (output folder, overwriting,
and grouping) and section 3.7 (manifest). `shared/contract/manifest.schema.json`
is the authoritative shape; this module's structural checks are hand-written
(not schema-driven) so the packaged CLI never needs to load a file outside
`cli/src/scanny_boy/` at runtime — the schema file itself is read only by
tests, the same split `schema_test_support.py` already uses for event
validation.
"""

from __future__ import annotations

import dataclasses
import importlib.metadata
import json
import os
from pathlib import Path
from typing import Any

from scanny_boy.events import Code

MANIFEST_FILENAME = "scanny-boy-manifest.json"
MANIFEST_FORMAT_VERSION = 1

STATUSES = {"running", "partial", "cancelled", "complete"}
GROUP_STATUSES = {"pending", "completed", "failed"}


class BadManifestError(Exception):
    """Maps to `BAD_MANIFEST`: the manifest could not be read or does not
    match its schema. Distinct from `ManifestMismatchError`, which means the
    manifest is well-formed but describes a different run."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = Code.BAD_MANIFEST
        self.message = message


class ManifestMismatchError(Exception):
    """Maps to `MANIFEST_MISMATCH`: a valid manifest exists, but this run's
    sources, order, grouping, film date, processing settings, or ICC hash
    differ from it (section 3.6)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = Code.MANIFEST_MISMATCH
        self.message = message


@dataclasses.dataclass(frozen=True)
class SourceRecord:
    filename: str
    absolute_path: str
    size: int
    mtime: float
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class OutputRecord:
    name: str
    size: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class CuratedMetadata:
    exposure_time: str
    f_number: str
    iso: int
    focal_length: str
    lens_model: str | None
    orientation: int
    camera_whitebalance: tuple[float, float, float, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "exposure_time": self.exposure_time,
            "f_number": self.f_number,
            "iso": self.iso,
            "focal_length": self.focal_length,
            "lens_model": self.lens_model,
            "orientation": self.orientation,
            "camera_whitebalance": list(self.camera_whitebalance),
        }


@dataclasses.dataclass
class GroupRecord:
    group_id: str
    members: list[str]
    expected_outputs: list[str]
    status: str = "pending"
    outputs: list[OutputRecord] = dataclasses.field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "members": self.members,
            "expected_outputs": self.expected_outputs,
            "status": self.status,
            "outputs": [o.to_dict() for o in self.outputs],
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


@dataclasses.dataclass
class Manifest:
    scanny_boy_version: str
    run_id: str
    status: str
    input_folder: str
    film_date: str
    shots_per_negative: int
    processing_params: dict[str, Any]
    icc_profile: dict[str, str]
    source_order: list[str]
    sources: list[SourceRecord]
    curated_metadata: CuratedMetadata
    groups: list[GroupRecord]
    started_at: str
    finished_at: str | None = None
    manifest_format_version: int = MANIFEST_FORMAT_VERSION

    def group(self, group_id: str) -> GroupRecord:
        for g in self.groups:
            if g.group_id == group_id:
                return g
        raise KeyError(group_id)

    def all_expected_outputs(self) -> list[str]:
        return [name for g in self.groups for name in g.expected_outputs]

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_format_version": self.manifest_format_version,
            "scanny_boy_version": self.scanny_boy_version,
            "run_id": self.run_id,
            "status": self.status,
            "input_folder": self.input_folder,
            "film_date": self.film_date,
            "shots_per_negative": self.shots_per_negative,
            "processing_params": self.processing_params,
            "icc_profile": self.icc_profile,
            "source_order": self.source_order,
            "sources": [s.to_dict() for s in self.sources],
            "curated_metadata": self.curated_metadata.to_dict(),
            "groups": [g.to_dict() for g in self.groups],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def current_scanny_boy_version() -> str:
    return importlib.metadata.version("scanny-boy")


def manifest_path(output_dir: Path) -> Path:
    return output_dir / MANIFEST_FILENAME


def write_manifest(output_dir: Path, manifest: Manifest) -> None:
    """Write to a temporary file, `fsync` it, then rename it into place
    (section 3.7) so readers never see a half-written manifest. `fsync`s the
    directory afterward where the platform permits it."""
    final_path = manifest_path(output_dir)
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


def estimate_manifest_size(manifest: Manifest) -> int:
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
    _require(isinstance(data["absolute_path"], str), "source absolute_path is not a string")
    _require(isinstance(data["size"], int) and data["size"] >= 0, "source size is invalid")
    _require(isinstance(data["mtime"], int | float), "source mtime is invalid")
    _require(_looks_like_sha256(data["sha256"]), "source sha256 is invalid")


def _validate_output_dict(data: Any) -> None:
    _require(isinstance(data, dict), "output entry is not an object")
    for key in ("name", "size", "sha256"):
        _require(key in data, f"output entry missing {key!r}")
    _require(isinstance(data["name"], str), "output name is not a string")
    _require(isinstance(data["size"], int) and data["size"] >= 0, "output size is invalid")
    _require(_looks_like_sha256(data["sha256"]), "output sha256 is invalid")


def _validate_group_dict(data: Any) -> None:
    _require(isinstance(data, dict), "group entry is not an object")
    for key in ("group_id", "members", "expected_outputs", "status", "outputs"):
        _require(key in data, f"group entry missing {key!r}")
    _require(isinstance(data["group_id"], str), "group_id is not a string")
    _require(isinstance(data["members"], list) and data["members"], "group members is invalid")
    _require(
        isinstance(data["expected_outputs"], list) and data["expected_outputs"],
        "group expected_outputs is invalid",
    )
    _require(data["status"] in GROUP_STATUSES, f"invalid group status {data['status']!r}")
    _require(isinstance(data["outputs"], list), "group outputs is not a list")
    for output in data["outputs"]:
        _validate_output_dict(output)


def _looks_like_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


_TOP_LEVEL_REQUIRED = (
    "manifest_format_version",
    "scanny_boy_version",
    "run_id",
    "status",
    "input_folder",
    "film_date",
    "shots_per_negative",
    "processing_params",
    "icc_profile",
    "source_order",
    "sources",
    "curated_metadata",
    "groups",
    "started_at",
    "finished_at",
)


def validate_manifest_dict(data: Any) -> None:
    """Structural checks mirroring `manifest.schema.json`. Raises
    `BadManifestError` on the first problem found."""
    _require(isinstance(data, dict), "manifest is not a JSON object")
    for key in _TOP_LEVEL_REQUIRED:
        _require(key in data, f"manifest missing required field {key!r}")

    _require(
        data["manifest_format_version"] == MANIFEST_FORMAT_VERSION,
        f"unsupported manifest_format_version {data['manifest_format_version']!r}",
    )
    _require(data["status"] in STATUSES, f"invalid manifest status {data['status']!r}")
    _require(isinstance(data["shots_per_negative"], int), "shots_per_negative is not an integer")
    _require(isinstance(data["processing_params"], dict), "processing_params is not an object")
    icc = data["icc_profile"]
    _require(isinstance(icc, dict) and "name" in icc and "sha256" in icc, "icc_profile is invalid")
    _require(_looks_like_sha256(icc["sha256"]), "icc_profile sha256 is invalid")
    _require(isinstance(data["source_order"], list), "source_order is not a list")
    _require(isinstance(data["sources"], list), "sources is not a list")
    for source in data["sources"]:
        _validate_source_dict(source)
    _require(isinstance(data["curated_metadata"], dict), "curated_metadata is not an object")
    _require(isinstance(data["groups"], list), "groups is not a list")
    for group in data["groups"]:
        _validate_group_dict(group)


def resolve_within(output_dir: Path, name: str) -> Path:
    """Resolve a manifest-recorded relative output `name` against
    `output_dir`, rejecting any escape via an absolute path, `..`, or a
    symlink (section 3.6: "A valid manifest contains only relative output
    names without .., absolute components, or symlink escapes. Every
    resolved output must remain inside the chosen output folder."). Raises
    `ValueError` on escape."""
    candidate = Path(name)
    if candidate.is_absolute():
        raise ValueError(f"output name {name!r} is an absolute path")
    if ".." in candidate.parts:
        raise ValueError(f"output name {name!r} contains '..'")

    root = output_dir.resolve()
    resolved = (output_dir / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"output name {name!r} escapes the output folder")
    return resolved


def _validate_output_paths_within(output_dir: Path, manifest: Manifest) -> None:
    for group in manifest.groups:
        for name in group.expected_outputs:
            try:
                resolve_within(output_dir, name)
            except ValueError as exc:
                raise BadManifestError(str(exc)) from exc
        for output in group.outputs:
            try:
                resolve_within(output_dir, output.name)
            except ValueError as exc:
                raise BadManifestError(str(exc)) from exc


def load_manifest(output_dir: Path) -> Manifest:
    """Read and structurally validate the manifest in `output_dir`. Raises
    `BadManifestError` if it is missing, unreadable, not valid JSON, fails
    the structural checks above, or names an output that escapes
    `output_dir`."""
    path = manifest_path(output_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BadManifestError(f"could not read {path}: {exc}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BadManifestError(f"{path} is not valid JSON: {exc}") from exc

    validate_manifest_dict(data)
    manifest = _manifest_from_dict(data)
    _validate_output_paths_within(output_dir, manifest)
    return manifest


def _manifest_from_dict(data: dict[str, Any]) -> Manifest:
    curated = data["curated_metadata"]
    wb = curated["camera_whitebalance"]
    return Manifest(
        manifest_format_version=data["manifest_format_version"],
        scanny_boy_version=data["scanny_boy_version"],
        run_id=data["run_id"],
        status=data["status"],
        input_folder=data["input_folder"],
        film_date=data["film_date"],
        shots_per_negative=data["shots_per_negative"],
        processing_params=data["processing_params"],
        icc_profile=data["icc_profile"],
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
        curated_metadata=CuratedMetadata(
            exposure_time=curated["exposure_time"],
            f_number=curated["f_number"],
            iso=curated["iso"],
            focal_length=curated["focal_length"],
            lens_model=curated["lens_model"],
            orientation=curated["orientation"],
            camera_whitebalance=(wb[0], wb[1], wb[2], wb[3]),
        ),
        groups=[
            GroupRecord(
                group_id=g["group_id"],
                members=list(g["members"]),
                expected_outputs=list(g["expected_outputs"]),
                status=g["status"],
                outputs=[
                    OutputRecord(name=o["name"], size=o["size"], sha256=o["sha256"])
                    for o in g["outputs"]
                ],
                error_code=g.get("error_code"),
                error_message=g.get("error_message"),
            )
            for g in data["groups"]
        ],
        started_at=data["started_at"],
        finished_at=data["finished_at"],
    )


# --- Rerun-mismatch comparison -------------------------------------------


def _source_hash_map(sources: list[SourceRecord]) -> dict[str, str]:
    return {s.filename: s.sha256 for s in sources}


def check_rerun_compatible(
    existing: Manifest,
    *,
    source_order: list[str],
    source_hashes: dict[str, str],
    shots_per_negative: int,
    groups: list[tuple[str, list[str]]],
    icc_sha256: str | None,
) -> None:
    """The subset of `check_rerun_matches`'s comparison available before a
    film date is known: source order and hashes, grouping, and the ICC
    profile. `probe --out` uses this for its overwrite-conflict preview
    (section 4.1); `convert` still runs the complete `check_rerun_matches`
    below before it writes anything, so a film date entered differently from
    what was previewed is still caught."""
    if existing.source_order != source_order:
        raise ManifestMismatchError(
            "the selection's source order differs from the previous run "
            f"recorded in {MANIFEST_FILENAME}"
        )

    if _source_hash_map(existing.sources) != source_hashes:
        raise ManifestMismatchError(
            "one or more source files' hashes differ from the previous run "
            f"recorded in {MANIFEST_FILENAME}"
        )

    if existing.shots_per_negative != shots_per_negative:
        raise ManifestMismatchError(
            f"shots per negative changed from {existing.shots_per_negative} "
            f"to {shots_per_negative} since the previous run"
        )

    existing_groups = [(g.group_id, g.members) for g in existing.groups]
    if existing_groups != groups:
        raise ManifestMismatchError("negative grouping differs from the previous run")

    if existing.icc_profile.get("sha256") != icc_sha256:
        raise ManifestMismatchError("the ICC profile differs from the previous run")


def check_rerun_matches(existing: Manifest, candidate: Manifest) -> None:
    """Section 3.6: "A rerun in the same folder must match the previous
    source filenames and hashes, order, grouping, film date, processing
    settings, and ICC hash." Raises `ManifestMismatchError` naming the first
    field that differs; `run_id`, `status`, and timing fields are expected
    to differ and are not compared."""
    check_rerun_compatible(
        existing,
        source_order=candidate.source_order,
        source_hashes=_source_hash_map(candidate.sources),
        shots_per_negative=candidate.shots_per_negative,
        groups=[(g.group_id, g.members) for g in candidate.groups],
        icc_sha256=candidate.icc_profile.get("sha256"),
    )

    if existing.film_date != candidate.film_date:
        raise ManifestMismatchError(
            f"film date changed from {existing.film_date} to {candidate.film_date} "
            "since the previous run"
        )

    if existing.processing_params != candidate.processing_params:
        raise ManifestMismatchError("processing settings differ from the previous run")
