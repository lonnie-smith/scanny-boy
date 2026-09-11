"""The Phase 1 work directory the stitch tests run against, and the roll,
stitch, and intermediate helpers that go with it.

These lived in `stitch_pipeline_test.py`, and seven other test modules reached
into it to borrow them — `from scanny_boy.stitch_pipeline_test import ...`.
Importing one test module from another means collecting any of those seven also
collects the whole stitch suite's module-level work, and it defeats pytest-xdist
`--dist loadfile`, which schedules by file. This is the pattern the rest of the
suite already uses (`sample_nef_support`, `schema_test_support`,
`synthetic_scene_support`): shared fixtures live in a support module that holds
no tests of its own.

`make_work_dir` is the expensive one — three synthetic scenes, encoded, written
out as three real Phase 1 TIFFs through `write_base_tiff` + `finalize_tiff`, at
about 2.1 s a call. It was called 85 times across the suite, and 74 of those
calls passed the identical default configuration and so produced a
byte-identical tree. Those callers should take the `work_dir` fixture in
`conftest.py`, which builds one template per session and hands each test a
`shutil.copytree` copy of it — about 3 ms. Call `make_work_dir` directly only
when you need a shape the default does not cover (more negatives, a
non-complete status, injected gains, a frame hook).
"""

from __future__ import annotations

import datetime
from fractions import Fraction
from pathlib import Path

import numpy as np

from scanny_boy import hashing
from scanny_boy.cancellation import CancellationToken
from scanny_boy.icc_profile import ProfileKind, load_icc_profile, profile_record
from scanny_boy.linear import encode_from_linear
from scanny_boy.manifest import (
    CuratedMetadata,
    GroupRecord,
    Manifest,
    OutputRecord,
    SourceRecord,
    current_scanny_boy_version,
    load_manifest,
    write_manifest,
)
from scanny_boy.roll_manifest import (
    RollInvariants,
    new_roll_manifest,
    write_roll_manifest,
)
from scanny_boy.stitch_pipeline import run_stitch
from scanny_boy.synthetic_scene_support import cut_frames, synthetic_scene
from scanny_boy.tiff_exif import NestedExifFields, finalize_tiff
from scanny_boy.tiff_writer import (
    BaseTiffTags,
    image_description,
    software_tag_value,
    write_base_tiff,
)

# Small enough to keep the suite fast, large enough that AKAZE clears
# MIN_PAIR_INLIERS on a film-like synthetic scene: measured 68-101 inliers
# per overlapping pair at this size and overlap.
FRAME_SIZE = (700, 900)  # (height, width)
SCENE_SIZE = (900, 2000)
OVERLAP = 0.35
FILM_DATE = "2026-08-02"


def write_intermediate(path: Path, pixels: np.ndarray, source_name: str) -> None:
    """Write one intermediate exactly as Phase 1's pipeline does, so
    `run_stitch` reads a genuine Phase 1 output rather than a stand-in."""
    base_path = path.with_name(f"{path.stem}.base.tif")
    write_base_tiff(
        base_path,
        pixels,
        BaseTiffTags(
            description=image_description(source_name),
            software=software_tag_value(),
            conversion_time=datetime.datetime(2026, 8, 28, 10, 0, 0),  # noqa: DTZ001
            icc_profile=load_icc_profile(),
            make="NIKON CORPORATION",
            model="NIKON Z f",
        ),
    )
    finalize_tiff(
        base_path,
        path,
        NestedExifFields(
            date_time_original=datetime.datetime(2026, 8, 2, 12, 33, 41, 450000),  # noqa: DTZ001
            exposure_time=Fraction(1, 30),
            f_number=Fraction(8, 1),
            iso=100,
            focal_length=Fraction(55, 1),
            lens_model="55mm f/2.8",
            date_time_digitized="2026:08:02 12:33:41",
            subsec_time_digitized="45",
            offset_time_digitized="-05:00",
        ),
    )


def negative_frames(
    *, overlapping: bool, seed: int, count: int = 3, frame_gains=None
) -> list[np.ndarray]:
    """`count` uint16 frames. Overlapping frames come from one scene and
    register; non-overlapping ones come from unrelated scenes and must be
    refused. `frame_gains`, when given, scales each frame's linear values
    per channel (downward only, so nothing clips) — lamp drift between
    shots."""
    if overlapping:
        scene = synthetic_scene(*SCENE_SIZE, seed=seed)
        frames, _ = cut_frames(
            scene,
            frame_size=FRAME_SIZE,
            count=count,
            overlap=OVERLAP,
            rotations_deg=[0.0, 2.0, -1.5][:count],
            seed=seed,
        )
    else:
        frames = [
            synthetic_scene(*FRAME_SIZE, seed=seed * 100 + i) for i in range(count)
        ]
    stacked = [np.stack([f, f, f], axis=-1) for f in frames]
    if frame_gains is not None:
        stacked = [
            frame * np.asarray(gain, dtype=np.float32)
            for frame, gain in zip(stacked, frame_gains, strict=True)
        ]
    return [encode_from_linear(frame.astype(np.float32)) for frame in stacked]


def make_work_dir(
    tmp_path: Path,
    *,
    negatives: int = 1,
    overlapping: bool = True,
    status: str = "complete",
    group_statuses: list[str] | None = None,
    film_date: str = FILM_DATE,
    shots_per_negative: int = 3,
    frame_gains: list[tuple[float, float, float]] | None = None,
    frame_hook=None,
) -> Path:
    """A work directory holding real Phase 1 intermediates and a real
    Phase 1 manifest, built without paying for RAW decoding.

    `frame_hook`, when given, is applied to every uint16 frame after the
    gains — the tilt-injection tests use it to warp each frame through a
    known W, making the true inter-frame map `W⁻¹·S·W`."""
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    sources: list[SourceRecord] = []
    groups: list[GroupRecord] = []
    source_order: list[str] = []

    for negative_index in range(negatives):
        frames = negative_frames(
            overlapping=overlapping,
            seed=11 + negative_index * 7,
            frame_gains=frame_gains,
        )
        if frame_hook is not None:
            frames = [frame_hook(frame) for frame in frames]
        members: list[str] = []
        outputs: list[OutputRecord] = []
        for frame_index, pixels in enumerate(frames):
            source_name = f"IMG_{negative_index}{frame_index}.NEF"
            output_name = f"IMG_{negative_index}{frame_index}.tif"
            write_intermediate(work_dir / output_name, pixels, source_name)
            path = work_dir / output_name
            outputs.append(
                OutputRecord(
                    name=output_name,
                    size=path.stat().st_size,
                    sha256=hashing.sha256_file(path),
                )
            )
            members.append(source_name)
            source_order.append(source_name)
            sources.append(
                SourceRecord(
                    filename=source_name,
                    absolute_path=f"/tmp/in/{source_name}",
                    size=1000 + frame_index,
                    mtime=1.0,
                    sha256=f"{negative_index}{frame_index}".ljust(64, "c"),
                    # Matches base_frame_block()'s "exposure" and this
                    # manifest's own curated_metadata below — a work_dir
                    # fixture negative is exposure-matched to
                    # attach_base_frame()'s default base frame by default.
                    exposure_time="1/30",
                    f_number="8",
                    iso=100,
                )
            )

        group_status = group_statuses[negative_index] if group_statuses else "completed"
        groups.append(
            GroupRecord(
                group_id=f"negative-{negative_index + 1:02d}",
                members=members,
                expected_outputs=[f"{Path(m).stem}.tif" for m in members],
                status=group_status,
                outputs=outputs if group_status == "completed" else [],
            )
        )

    write_manifest(
        work_dir,
        Manifest(
            scanny_boy_version=current_scanny_boy_version(),
            run_id="convert-run",
            status=status,
            input_folder="/tmp/in",
            film_date=film_date,
            shots_per_negative=shots_per_negative,
            processing_params={"gamma": [1.8, 16]},
            icc_profile=profile_record(ProfileKind.LINEAR),
            source_order=source_order,
            sources=sources,
            curated_metadata=CuratedMetadata(
                exposure_time="1/30",
                f_number="8",
                iso=100,
                focal_length="55",
                lens_model="55mm f/2.8",
                orientation=1,
                camera_whitebalance=(1.69, 1.0, 1.38, 1.0),
            ),
            groups=groups,
            started_at="2026-08-02T00:00:00Z",
            finished_at="2026-08-02T00:01:00Z",
        ),
    )
    return work_dir


def make_out_dir(tmp_path: Path, name: str = "out") -> Path:
    out = tmp_path / name
    out.mkdir()
    return out


def make_roll_dir(tmp_path: Path, name: str = "out", *, film_kind: str = "colour") -> Path:
    """A real, empty roll, written through P3-2's own writer.

    Section 5.4 decision 1: `stitch` never creates a roll, so every stitch
    test needs one to exist first. `roll init` does not arrive until P3-4, so
    this is `new_roll_manifest` — the same constructor `roll init` will call —
    and not hand-authored JSON. REBATE_ANCHORING §3.2 rule 4: a roll with no
    film-base reference refuses to stitch, so the tests that do stitch get
    one attached; `attach_base_frame` customises or removes it."""
    roll = make_out_dir(tmp_path, name)
    manifest = new_roll_manifest(
        roll_id=f"00000000-0000-4000-8000-0000000000{len(name):02d}",
        roll_name=name,
        film_kind=film_kind,
    )
    attach_base_frame(manifest)
    write_roll_manifest(roll, manifest)
    return roll


def base_frame_block(**overrides) -> dict:
    """A film_base block a gate would accept, for the tests that only need
    the run-time state machine — the measurement itself is film_base_test's
    subject. The profile id and camera model are None so no run conflicts
    with them by default."""
    block = {
        "density": [-0.42, -0.12, -0.99],
        "locked_at": None,
        "attached_at": "2026-09-06T18:04:11Z",
        "source_name": "_DSC5012.NEF",
        "source_sha256": "3" * 64,
        "flat_field_profile_id": None,
        "camera_model": None,
        "chosen_index": 0,
        "populations": [
            {
                "density": [-0.42, -0.12, -0.99],
                "luma": -0.25,
                "area_fraction": 0.44,
                "cells": 34100,
                "spread": 0.012,
            }
        ],
        "clipped_fractions": [0.0, 0.0, 0.0],
        "grid_cells": 786432,
        "measure_version": 1,
        "exposure": {"exposure_time": "1/30", "f_number": "8", "iso": 100},
    }
    block.update(overrides)
    return block


def attach_base_frame(roll, **overrides) -> None:
    roll.film_base = base_frame_block(**overrides)


def roll_invariants(work_dir: Path) -> RollInvariants:
    """The invariants a stitch of `work_dir` would present, for the tests
    that drive `plan_rerun` directly."""
    work = load_manifest(work_dir)
    return RollInvariants(
        processing_params=work.processing_params,
        icc_profile_sha256=work.icc_profile["sha256"],
        published_icc_profile_sha256=work.icc_profile["sha256"],
        stitch_params={},
    )


def run_stitch_with_defaults(work_dir, out_dir, *, events=None, cancel=None, **kwargs):
    defaults = {
        "run_id": "stitch-run",
        "overwrite": False,
        "allow_partial": False,
        "jobs": 1,
    }
    defaults.update(kwargs)
    return run_stitch(
        work_dir,
        out_dir,
        cancel=cancel if cancel is not None else CancellationToken(),
        emit=(events.append if events is not None else (lambda event: None)),
        **defaults,
    )
