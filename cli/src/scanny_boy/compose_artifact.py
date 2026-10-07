"""The compose artifact: one negative's order-independent stitch work, saved
beside its work folder so a later `stitch` can skip it
(docs/PARALLEL_STITCH_PLAN.md §3.2).

`stitch --compose-only` solves a negative's layout and runs
`composite.compose_negative`, then writes the result to
`<work>/.composed/<group_id>/`:

- `log.npy`, `covered.npy`, `grid.npy`, `keep.npy`: the four
  `ComposedNegative` arrays, plain `np.save` (no compression, so `log.npy`
  loads with `mmap_mode`).
- `composed.pkl`: a pickled `ComposedMeta` — every non-array
  `ComposedNegative` field plus the solve's products (`SolveProducts`).
- `inputs.json`: the fingerprint of everything the artifact was computed
  from. A reader recomputes it and treats any difference as "stale".

A group whose solve or compose failed is written instead as `failure.json`
(`{"code", "message"}`), `record_fields.pkl` (the fields the solve had set on
the negative's record before it raised, and the warnings it had emitted) and
the same `inputs.json`.

Pickle is acceptable here: the artifact is a local cache written and read by
this same program, `inputs.json` pins the format version and the program
version, and any failure to read it — a truncated file, an unpicklable
class after a refactor — makes the reader report it unusable, which the
caller turns into a full recompute. Correctness never depends on the
artifact; it only saves time.

The directory is built under a hidden temporary name and `os.replace`d into
place, so a reader never sees a partial artifact.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pickle
import shutil
import uuid
from pathlib import Path
from typing import Any

import numpy as np

from scanny_boy.composite import ComposedNegative

COMPOSE_FORMAT_VERSION = 1
COMPOSED_DIRNAME = ".composed"

_ARRAY_FIELDS = ("img_log", "covered", "grid", "keep")
_INPUTS_FILE = "inputs.json"
_COMPOSED_FILE = "composed.pkl"
_FAILURE_FILE = "failure.json"
_FAILURE_RECORD_FILE = "record_fields.pkl"
_OVERHEAD_BYTES = 1024 * 1024


class ArtifactUnusableError(Exception):
    """The artifact is stale (its fingerprint differs) or unreadable."""


@dataclasses.dataclass
class SolveProducts:
    """What `stitch_pipeline._solve_negative` and its caller produce that the
    publish needs: the entry's solve products, the record fields the solve
    set (name -> value, only the fields it actually assigned), and the
    warnings it emitted (`(code value, message)`, replayed by the commit)."""

    layout: Any
    frame_size: tuple[int, int]
    ca_maps: dict | None
    pairs: list
    rectification: Any
    valid_rect: tuple[int, int, int, int]
    record_fields: dict[str, Any]
    warnings: list[tuple[str, str]]


@dataclasses.dataclass
class ComposedMeta:
    """The `composed.pkl` payload: every non-array `ComposedNegative` field
    (generated from the dataclass, so a field added there is carried
    automatically) and the solve's products."""

    composed_fields: dict[str, Any]
    products: SolveProducts


@dataclasses.dataclass
class LoadedArtifact:
    """A valid artifact: either the success pair (`composed`, `products`) or
    a failure (`failure`, with `products` carrying just the record fields and
    warnings, every other product left None)."""

    composed: ComposedNegative | None
    products: SolveProducts
    failure: tuple[str, str] | None


def artifact_dir(work_dir: Path, group_id: str) -> Path:
    return Path(work_dir) / COMPOSED_DIRNAME / group_id


def estimate_artifact_bytes(
    canvas_size: tuple[int, int], channels: int, *, passthrough: bool
) -> int:
    """An upper-bound estimate of an artifact's size from the solved canvas
    `(width, height)`: the log canvas and coverage mask, plus (for canvases
    small enough that the analysis grid passes the image through unchanged)
    a second image-sized grid and keep mask, plus a fixed overhead."""
    width, height = canvas_size
    pixels = width * height
    total = pixels * (channels * 4 + 1)
    if passthrough:
        total += pixels * (channels * 4 + 1)
    return total + _OVERHEAD_BYTES


def _non_array_fields(composed: ComposedNegative) -> dict[str, Any]:
    return {
        field.name: getattr(composed, field.name)
        for field in dataclasses.fields(ComposedNegative)
        if field.name not in _ARRAY_FIELDS
    }


def _dir_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.iterdir() if p.is_file())


def _sweep_temporaries(parent: Path, group_id: str) -> None:
    for leftover in parent.glob(f".{group_id}.tmp-*"):
        shutil.rmtree(leftover, ignore_errors=True)


def _publish_directory(
    work_dir: Path, group_id: str, fingerprint: dict, fill
) -> tuple[Path, int]:
    """Build the artifact directory via `fill(tmp_dir)`, write `inputs.json`
    last, and rename it into place. Returns the final path and its size."""
    parent = Path(work_dir) / COMPOSED_DIRNAME
    parent.mkdir(parents=True, exist_ok=True)
    _sweep_temporaries(parent, group_id)
    tmp = parent / f".{group_id}.tmp-{uuid.uuid4().hex}"
    tmp.mkdir()
    try:
        fill(tmp)
        (tmp / _INPUTS_FILE).write_text(
            json.dumps(fingerprint, sort_keys=True, indent=2)
        )
        size = _dir_bytes(tmp)
        final = parent / group_id
        shutil.rmtree(final, ignore_errors=True)
        os.replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return final, size


def write_artifact(
    work_dir: Path,
    group_id: str,
    fingerprint: dict,
    composed: ComposedNegative,
    products: SolveProducts,
) -> int:
    """Write a success artifact atomically; returns its size in bytes."""

    def fill(tmp: Path) -> None:
        for name in _ARRAY_FIELDS:
            np.save(tmp / f"{_array_stem(name)}.npy", getattr(composed, name))
        with (tmp / _COMPOSED_FILE).open("wb") as f:
            pickle.dump(
                ComposedMeta(_non_array_fields(composed), products),
                f,
                protocol=pickle.HIGHEST_PROTOCOL,
            )

    _final, size = _publish_directory(work_dir, group_id, fingerprint, fill)
    return size


def write_failure(
    work_dir: Path,
    group_id: str,
    fingerprint: dict,
    *,
    code: str,
    message: str,
    record_fields: dict[str, Any],
    warnings: list[tuple[str, str]],
) -> None:
    """Write a failure artifact atomically."""

    def fill(tmp: Path) -> None:
        (tmp / _FAILURE_FILE).write_text(json.dumps({"code": code, "message": message}))
        with (tmp / _FAILURE_RECORD_FILE).open("wb") as f:
            pickle.dump(
                {"record_fields": record_fields, "warnings": warnings},
                f,
                protocol=pickle.HIGHEST_PROTOCOL,
            )

    _publish_directory(work_dir, group_id, fingerprint, fill)


def _array_stem(field_name: str) -> str:
    return "log" if field_name == "img_log" else field_name


def load_artifact(
    work_dir: Path, group_id: str, fingerprint: dict
) -> LoadedArtifact | None:
    """The valid artifact for `group_id`, or `None` when there is no
    artifact directory at all. Raises `ArtifactUnusableError` for one that is
    stale or cannot be read — the caller recomputes in full.

    `log.npy` is memory-mapped read-only; the other arrays load normally
    (`finish_negative` never writes into any of them, but only `img_log` is
    large)."""
    directory = artifact_dir(work_dir, group_id)
    if not directory.is_dir():
        return None
    try:
        stored = json.loads((directory / _INPUTS_FILE).read_text())
        if stored != json.loads(json.dumps(fingerprint)):
            raise ArtifactUnusableError("its inputs have changed")
        if (directory / _FAILURE_FILE).exists():
            failure = json.loads((directory / _FAILURE_FILE).read_text())
            with (directory / _FAILURE_RECORD_FILE).open("rb") as f:
                payload = pickle.load(f)
            products = SolveProducts(
                layout=None,
                frame_size=(0, 0),
                ca_maps=None,
                pairs=[],
                rectification=None,
                valid_rect=(0, 0, 0, 0),
                record_fields=payload["record_fields"],
                warnings=[tuple(w) for w in payload["warnings"]],
            )
            return LoadedArtifact(
                composed=None,
                products=products,
                failure=(str(failure["code"]), str(failure["message"])),
            )
        with (directory / _COMPOSED_FILE).open("rb") as f:
            meta = pickle.load(f)
        if not isinstance(meta, ComposedMeta):
            raise ArtifactUnusableError("composed.pkl holds an unexpected object")
        arrays: dict[str, np.ndarray] = {}
        for name in _ARRAY_FIELDS:
            path = directory / f"{_array_stem(name)}.npy"
            arrays[name] = np.load(
                path,
                mmap_mode="r" if name == "img_log" else None,
                allow_pickle=False,
            )
        composed = ComposedNegative(**arrays, **meta.composed_fields)
        return LoadedArtifact(composed=composed, products=meta.products, failure=None)
    except ArtifactUnusableError:
        raise
    except Exception as exc:
        raise ArtifactUnusableError(f"it could not be read ({exc})") from exc


def remove_artifact(work_dir: Path, group_id: str) -> None:
    """Best effort: a leftover artifact is only wasted disk. The `composed`
    folder itself goes too once it is empty, so a finished work folder holds
    exactly what it did before the compose."""
    shutil.rmtree(artifact_dir(work_dir, group_id), ignore_errors=True)
    try:
        (Path(work_dir) / COMPOSED_DIRNAME).rmdir()
    except OSError:
        pass
