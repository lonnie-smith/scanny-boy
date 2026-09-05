"""The conversion pipeline, in two parts.

Chunk 5's list comes first: manifest writing/fsync ordering, staging and
publish mechanics, disk checks, overwrite/rerun handling, and crash
recovery. Chunk 6's list follows, under its own banner: `--jobs`, the
thread-worker path, the memory budget, and cooperative cancellation.

All of these need real sample NEFs to get past setup-consistency validation
(`read_camera_whitebalance` opens the file with real `rawpy`, and section 7
says not to mock that — see `fake_nef_support.py`'s docstring). Real
`rawpy.postprocess()` and the full two-pass TIFF write are already proven
correct by Chunk 3/4's own test suites and by this chunk's one true
end-to-end test in `cli_test.py`
(`test_convert_with_real_samples_writes_six_tiffs_and_completes`), so most
tests here patch `raw_decode.decode_raw` — a function in *our own* code,
not `rawpy` itself — to return a tiny synthetic frame, keeping the
metadata/whitebalance reading real while cutting each frame's processing
from ~2.5s to well under a second.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import threading
from pathlib import Path

import numpy as np
import pytest
import tifffile

from scanny_boy import concurrency, disk_check, flatfield, pipeline, raw_decode
from scanny_boy.cancellation import CancellationToken
from scanny_boy.events import Code, WarningEvent
from scanny_boy.hashing import sha256_file
from scanny_boy.manifest import MANIFEST_FILENAME, load_manifest
from scanny_boy.manifest_schema_test_support import (
    assert_matches_manifest_schema,
    load_manifest_schema,
)
from scanny_boy.metadata import UnsupportedRawError
from scanny_boy.output_folder import STAGING_SUFFIX
from scanny_boy.pipeline import ConvertFailure, run_convert
from scanny_boy.sample_nef_support import (
    FIXTURES_DIR,
    NEGATIVE_1,
    NEGATIVE_2,
    REAL_SAMPLE_FILES,
    requires_real_samples,
    stage_samples,
)
from scanny_boy.selection import GridSpec
from scanny_boy.tiff_fingerprint_support import tiff_fingerprint

MANIFEST_SCHEMA = load_manifest_schema()


def _fast_frame() -> raw_decode.DecodedFrame:
    # Small gradient-plus-noise, per section 7: not pure random noise, so
    # Deflate compresses it quickly.
    rng = np.random.default_rng(0)
    height, width = 8, 12
    y = np.linspace(0, 65535, height)[:, None]
    x = np.linspace(0, 65535, width)[None, :]
    base = (y + x) / 2
    noise = rng.normal(scale=200, size=(height, width))
    r = np.clip(base + noise, 0, 65535)
    g = np.clip(base * 0.8 + noise, 0, 65535)
    b = np.clip(base * 0.6 + noise, 0, 65535)
    pixels = np.stack([r, g, b], axis=-1).astype(np.uint16)
    return raw_decode.DecodedFrame(pixels=pixels, width=width, height=height)


def _install_fast_decode(monkeypatch, *, fail_for: set[str] = frozenset()) -> None:
    def _fake_decode(path: Path, **_kwargs) -> raw_decode.DecodedFrame:
        if path.name in fail_for:
            raise UnsupportedRawError(str(path))
        return _fast_frame()

    monkeypatch.setattr(raw_decode, "decode_raw", _fake_decode)


def _copy_samples(tmp_path: Path, names: list[str]) -> Path:
    """A scratch copy of real sample NEFs for tests that must mutate a
    source file — never touch the shared fixtures directory itself."""
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    for name in names:
        shutil.copy2(FIXTURES_DIR / name, input_dir / name)
    return input_dir


def _stem(name: str) -> str:
    return Path(name).stem


# --- manifest fsync ordering -------------------------------------------


@requires_real_samples
def test_running_manifest_is_written_before_the_first_output_appears(monkeypatch, tmp_path):
    _install_fast_decode(monkeypatch)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    seen_before_first_publish = {}

    real_replace = os.replace

    def _checking_replace(src, dst):
        if str(dst).endswith(".tif") and "manifest_checked" not in seen_before_first_publish:
            seen_before_first_publish["manifest_checked"] = True
            manifest = load_manifest(out_dir)
            seen_before_first_publish["status"] = manifest.status
            assert not Path(dst).exists()  # nothing published yet
        real_replace(src, dst)

    monkeypatch.setattr("scanny_boy.pipeline.os.replace", _checking_replace)

    outcome = run_convert(
        FIXTURES_DIR, list(NEGATIVE_1), out_dir, 3, run_id="r1", emit=lambda e: None
    )

    assert outcome.status == "complete"
    assert seen_before_first_publish["manifest_checked"] is True
    assert seen_before_first_publish["status"] == "running"


# --- schema validation, hashes, and output records -----------------------


@requires_real_samples
def test_manifest_validates_against_schema_and_records_correct_hashes(monkeypatch, tmp_path):
    _install_fast_decode(monkeypatch)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    outcome = run_convert(
        FIXTURES_DIR, list(NEGATIVE_1), out_dir, 3, run_id="r1", emit=lambda e: None
    )

    assert outcome.status == "complete"
    data = json.loads((out_dir / MANIFEST_FILENAME).read_text())
    assert_matches_manifest_schema(data, MANIFEST_SCHEMA)

    manifest = load_manifest(out_dir)
    assert manifest.source_order == list(NEGATIVE_1)
    for source in manifest.sources:
        real_path = FIXTURES_DIR / source.filename
        assert source.sha256 == sha256_file(real_path)
        assert source.size == real_path.stat().st_size

    group = manifest.groups[0]
    assert group.status == "completed"
    assert {o.name for o in group.outputs} == {f"{_stem(n)}.tif" for n in NEGATIVE_1}
    for output in group.outputs:
        published = out_dir / output.name
        assert output.size == published.stat().st_size
        assert output.sha256 == sha256_file(published)


# --- group failure isolates the group, later groups continue -------------


@requires_real_samples
def test_a_failed_frame_removes_only_its_own_group_staging_and_later_groups_continue(
    monkeypatch, tmp_path
):
    bad_file = REAL_SAMPLE_FILES[1]  # second member of negative-01
    _install_fast_decode(monkeypatch, fail_for={bad_file})
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    events = []

    # Staged, so the six-file selection is contiguous in its catalogue: the
    # shared fixtures directory also holds the gate-B stitching scans and
    # later sessions between and around these frames.
    input_dir = stage_samples(tmp_path, list(REAL_SAMPLE_FILES))
    outcome = run_convert(
        input_dir,
        list(REAL_SAMPLE_FILES),
        out_dir,
        3,
        run_id="r1",
        emit=events.append,
    )

    assert outcome.status == "partial"
    group_1, group_2 = outcome.manifest.groups
    assert group_1.status == "failed"
    assert group_1.error_code == "UNSUPPORTED_RAW"
    assert group_2.status == "completed"

    # No item_done for the failed group's files; the second group's do exist.
    item_done_outputs = {e.output for e in events if type(e).__name__ == "ItemDone"}
    assert item_done_outputs == {f"{_stem(n)}.tif" for n in REAL_SAMPLE_FILES[3:]}
    group_failed = [e for e in events if type(e).__name__ == "GroupFailed"]
    assert len(group_failed) == 1
    assert group_failed[0].group_id == group_1.group_id
    group_done = [e for e in events if type(e).__name__ == "GroupDone"]
    assert [e.group_id for e in group_done] == [group_2.group_id]

    for name in REAL_SAMPLE_FILES[:3]:
        assert not (out_dir / f"{_stem(name)}.tif").exists()
    for name in REAL_SAMPLE_FILES[3:]:
        assert (out_dir / f"{_stem(name)}.tif").exists()


@requires_real_samples
def test_no_staging_directory_survives_success_or_handled_failure(monkeypatch, tmp_path):
    bad_file = REAL_SAMPLE_FILES[1]
    _install_fast_decode(monkeypatch, fail_for={bad_file})
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    input_dir = stage_samples(tmp_path, list(REAL_SAMPLE_FILES))

    run_convert(
        input_dir,
        list(REAL_SAMPLE_FILES),
        out_dir,
        3,
        run_id="r1",
        emit=lambda e: None,
    )

    staging_dirs = [p for p in out_dir.iterdir() if p.name.endswith(STAGING_SUFFIX)]
    assert staging_dirs == []


# --- disk checks fail before any decode -----------------------------------


@requires_real_samples
def test_insufficient_disk_space_fails_before_any_decode(monkeypatch, tmp_path):
    decode_calls: list[Path] = []
    monkeypatch.setattr(raw_decode, "decode_raw", lambda p: decode_calls.append(p) or _fast_frame())

    def _always_insufficient(output_dir, required_bytes):
        raise disk_check.DiskCheckError(required_bytes, available_bytes=0)

    monkeypatch.setattr("scanny_boy.pipeline.disk_check.check_disk_space", _always_insufficient)

    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with pytest.raises(ConvertFailure) as excinfo:
        run_convert(
            FIXTURES_DIR, list(NEGATIVE_1), out_dir, 3, run_id="r1", emit=lambda e: None
        )

    assert excinfo.value.code.value == "INSUFFICIENT_DISK"
    assert decode_calls == []


# --- OUTPUT_CONFLICT / --overwrite -----------------------------------------


@requires_real_samples
def test_existing_outputs_fail_without_overwrite_and_succeed_with_it(monkeypatch, tmp_path):
    _install_fast_decode(monkeypatch)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    first = run_convert(
        FIXTURES_DIR, list(NEGATIVE_1), out_dir, 3, run_id="r1", emit=lambda e: None
    )
    assert first.status == "complete"

    with pytest.raises(ConvertFailure) as excinfo:
        run_convert(
            FIXTURES_DIR, list(NEGATIVE_1), out_dir, 3, run_id="r2", emit=lambda e: None
        )
    assert excinfo.value.code.value == "OUTPUT_CONFLICT"
    for name in NEGATIVE_1:
        assert _stem(name) in excinfo.value.message

    third = run_convert(
        FIXTURES_DIR,
        list(NEGATIVE_1),
        out_dir,
        3,
        run_id="r3",
        overwrite=True,
        emit=lambda e: None,
    )
    assert third.status == "complete"
    assert third.run_id == "r3"
    manifest = load_manifest(out_dir)
    assert manifest.run_id == "r3"
    for name in NEGATIVE_1:
        assert (out_dir / f"{_stem(name)}.tif").exists()


# --- MANIFEST_MISMATCH / BAD_MANIFEST --------------------------------------
#
# Phase 3 section 3.5: `--film-date` no longer exists, and `film_date` is now
# derived from the selection's own real capture times rather than supplied
# by the caller, so two runs of the *same* selection always derive the same
# value — there is no longer a way to construct "a rerun with a different
# film date" independent of also changing the selection itself (which fails
# earlier, on the source-hash comparison). The dimension this used to prove
# is retired along with the CLI argument that made it constructible.


@requires_real_samples
def test_unreadable_manifest_is_bad_manifest(monkeypatch, tmp_path):
    _install_fast_decode(monkeypatch)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    run_convert(FIXTURES_DIR, list(NEGATIVE_1), out_dir, 3, run_id="r1", emit=lambda e: None)

    (out_dir / MANIFEST_FILENAME).write_text("not valid json")

    with pytest.raises(ConvertFailure) as excinfo:
        run_convert(
            FIXTURES_DIR, list(NEGATIVE_1), out_dir, 3, run_id="r2", emit=lambda e: None
        )
    assert excinfo.value.code.value == "BAD_MANIFEST"


@requires_real_samples
def test_unrelated_nonempty_output_folder_is_rejected(monkeypatch, tmp_path):
    _install_fast_decode(monkeypatch)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "unrelated.jpg").touch()

    with pytest.raises(ConvertFailure) as excinfo:
        run_convert(
            FIXTURES_DIR, list(NEGATIVE_1), out_dir, 3, run_id="r1", emit=lambda e: None
        )
    assert excinfo.value.code.value == "OUTPUT_NOT_EMPTY"


@requires_real_samples
def test_output_folder_equal_to_input_folder_is_rejected(monkeypatch, tmp_path):
    _install_fast_decode(monkeypatch)
    with pytest.raises(ConvertFailure) as excinfo:
        run_convert(
            FIXTURES_DIR, list(NEGATIVE_1), FIXTURES_DIR, 3, run_id="r1", emit=lambda e: None
        )
    assert excinfo.value.code.value == "OUTPUT_SAME_AS_INPUT"


# --- source changed after hashing ------------------------------------------


@requires_real_samples
def test_source_changed_after_hashing_stops_its_group(monkeypatch, tmp_path):
    input_dir = _copy_samples(tmp_path, list(NEGATIVE_1))
    mutated = input_dir / NEGATIVE_1[1]

    def _decode_then_mutate(path: Path, **_kwargs) -> raw_decode.DecodedFrame:
        frame = _fast_frame()
        if path == mutated:
            # Simulate the file changing between hashing and the
            # "immediately after decoding" re-check (section 3.7).
            os.utime(path, (path.stat().st_mtime + 100, path.stat().st_mtime + 100))
        return frame

    monkeypatch.setattr(raw_decode, "decode_raw", _decode_then_mutate)

    out_dir = tmp_path / "out"
    out_dir.mkdir()

    outcome = run_convert(
        input_dir, list(NEGATIVE_1), out_dir, 3, run_id="r1", emit=lambda e: None
    )

    assert outcome.status == "partial"
    assert outcome.manifest.groups[0].status == "failed"
    assert "changed since it was hashed" in outcome.manifest.groups[0].error_message
    for name in NEGATIVE_1:
        assert not (out_dir / f"{_stem(name)}.tif").exists()


# --- crash recovery: publish interrupted mid-group -------------------------


@requires_real_samples
def test_recovery_after_a_publish_crash_replaces_every_output_in_the_group(monkeypatch, tmp_path):
    _install_fast_decode(monkeypatch)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    real_replace = os.replace
    tif_calls = {"count": 0}

    def _flaky_replace(src, dst):
        if Path(dst).suffix == ".tif":
            tif_calls["count"] += 1
            if tif_calls["count"] == 2:
                raise RuntimeError("simulated crash while publishing the second frame")
        real_replace(src, dst)

    monkeypatch.setattr("scanny_boy.pipeline.os.replace", _flaky_replace)

    with pytest.raises(RuntimeError, match="simulated crash"):
        run_convert(
            FIXTURES_DIR, list(NEGATIVE_1), out_dir, 3, run_id="run-a", emit=lambda e: None
        )

    # Exactly one file was published before the crash; the manifest was
    # never updated past its initial "running" write for this group.
    published = [name for name in NEGATIVE_1 if (out_dir / f"{_stem(name)}.tif").exists()]
    assert len(published) == 1
    crashed_manifest = load_manifest(out_dir)
    assert crashed_manifest.run_id == "run-a"
    assert crashed_manifest.status == "running"
    assert crashed_manifest.groups[0].status == "pending"
    staging_dirs = [p for p in out_dir.iterdir() if p.name.endswith(STAGING_SUFFIX)]
    assert len(staging_dirs) == 1

    monkeypatch.setattr("scanny_boy.pipeline.os.replace", real_replace)

    outcome = run_convert(
        FIXTURES_DIR, list(NEGATIVE_1), out_dir, 3, run_id="run-b", emit=lambda e: None
    )

    assert outcome.status == "complete"
    recovered_manifest = load_manifest(out_dir)
    assert recovered_manifest.run_id == "run-b"
    assert recovered_manifest.groups[0].status == "completed"
    for name in NEGATIVE_1:
        published_path = out_dir / f"{_stem(name)}.tif"
        assert published_path.exists()
    assert [p for p in out_dir.iterdir() if p.name.endswith(STAGING_SUFFIX)] == []


# =========================================================================
# Chunk 6: threaded conversion and cancellation (section 3.8)
# =========================================================================
#
# The tests below exercise `--jobs`, the thread-worker path, and
# cooperative cancellation. None of them sleeps for a fixed interval to
# "let work start": every one gates on a `threading.Event` or a
# `threading.Barrier` that only opens once the pipeline has demonstrably
# reached the point being tested, per the chunk's "do not use a race-prone
# fixed sleep".


@pytest.mark.slow
@requires_real_samples
def test_jobs_1_and_jobs_4_produce_identical_pixels_and_metadata(tmp_path):
    """The headline guarantee of this chunk: turning on threads changes
    nothing about the files produced. Real `rawpy` decoding and the real
    two-pass TIFF write, deliberately unpatched."""
    serial_dir = tmp_path / "serial"
    threaded_dir = tmp_path / "threaded"
    serial_dir.mkdir()
    threaded_dir.mkdir()
    input_dir = stage_samples(tmp_path, list(REAL_SAMPLE_FILES))

    serial = run_convert(
        input_dir,
        list(REAL_SAMPLE_FILES),
        serial_dir,
        3,
        run_id="serial",
        jobs=1,
        emit=lambda e: None,
    )
    threaded = run_convert(
        input_dir,
        list(REAL_SAMPLE_FILES),
        threaded_dir,
        3,
        run_id="threaded",
        jobs=4,
        emit=lambda e: None,
    )

    assert serial.status == "complete"
    assert threaded.status == "complete"
    assert serial.workers == 1
    assert threaded.workers == 4

    for name in REAL_SAMPLE_FILES:
        output = f"{_stem(name)}.tif"
        assert tiff_fingerprint(serial_dir / output) == tiff_fingerprint(
            threaded_dir / output
        ), f"{output} differs between --jobs 1 and --jobs 4"


@requires_real_samples
def test_jobs_1_never_constructs_a_thread_pool(monkeypatch, tmp_path):
    """Section 3.8: "`--jobs 1` uses the serial path." Not "a pool of one"
    — no executor at all, so a serial run cannot inherit a thread-pool
    bug."""
    _install_fast_decode(monkeypatch)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def _forbidden(*args, **kwargs):
        raise AssertionError("--jobs 1 must not construct a ThreadPoolExecutor")

    monkeypatch.setattr("scanny_boy.pipeline.ThreadPoolExecutor", _forbidden)

    outcome = run_convert(
        FIXTURES_DIR,
        list(NEGATIVE_1),
        out_dir,
        3,
        run_id="r1",
        jobs=1,
        emit=lambda e: None,
    )

    assert outcome.status == "complete"
    assert outcome.workers == 1


# --- out-of-order completion still reports one result per source ---------


def _install_reverse_order_decode(
    monkeypatch, members: list[str]
) -> tuple[list[str], pipeline.EmitFn]:
    """Force the frames of one group to finish decoding in exactly reverse
    order, with no sleeps.

    A `Barrier` first proves every worker is genuinely running at once.
    Then each worker waits on its own gate; only the last member's gate
    starts open. The caller must feed every emitted event through the
    returned `release_predecessor`, which opens a frame's predecessor's
    gate once that frame's own decode-progress event has been emitted —
    not when `decode_raw` merely returns. `_stage_one_frame` does real
    work (a `stat` via `_verify_source_unchanged`, a lock acquisition)
    between the two, so gating on the return of `decode_raw` only orders
    the decode calls; it leaves the order of the progress events — the
    thing the test actually asserts on — a genuine race. Gating on the
    event closes that gap, making the order deterministic rather than
    merely likely.
    """
    decode_order: list[str] = []
    order_lock = threading.Lock()
    barrier = threading.Barrier(len(members))
    gates = {name: threading.Event() for name in members}
    gates[members[-1]].set()
    predecessor = {members[i]: members[i - 1] for i in range(1, len(members))}
    name_by_index = dict(enumerate(members))

    def _fake_decode(path: Path, **_kwargs) -> raw_decode.DecodedFrame:
        barrier.wait(timeout=30)
        assert gates[path.name].wait(timeout=30), f"gate for {path.name} never opened"
        with order_lock:
            decode_order.append(path.name)
        return _fast_frame()

    def release_predecessor(event: object) -> None:
        if type(event).__name__ != "Progress" or event.step.value != "decode":
            return
        name = name_by_index[event.source_index]
        if name in predecessor:
            gates[predecessor[name]].set()

    monkeypatch.setattr(raw_decode, "decode_raw", _fake_decode)
    return decode_order, release_predecessor


@requires_real_samples
def test_every_source_index_reports_one_final_result_despite_out_of_order_completion(
    monkeypatch, tmp_path
):
    decode_order, release_predecessor = _install_reverse_order_decode(
        monkeypatch, list(NEGATIVE_1)
    )
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    events: list = []

    def emit(event: object) -> None:
        events.append(event)
        release_predecessor(event)

    outcome = run_convert(
        FIXTURES_DIR,
        list(NEGATIVE_1),
        out_dir,
        3,
        run_id="r1",
        jobs=3,
        emit=emit,
    )

    assert outcome.status == "complete"
    # The premise: work really did complete out of source order.
    assert decode_order == list(reversed(NEGATIVE_1))

    progress = [e for e in events if type(e).__name__ == "Progress"]
    decode_progress = [e for e in progress if e.step.value == "decode"]
    # Each predecessor's gate opens only once its successor's own
    # decode-progress event has been emitted (see release_predecessor),
    # so the decode-progress events themselves — not just the decode
    # calls — are ordered strictly in reverse.
    assert [e.source_index for e in decode_progress] == [2, 1, 0]

    # ...and yet every source index reports exactly one final result, in
    # canonical order.
    item_done = [e for e in events if type(e).__name__ == "ItemDone"]
    assert [e.source_index for e in item_done] == [0, 1, 2]
    assert [e.output for e in item_done] == [f"{_stem(n)}.tif" for n in NEGATIVE_1]

    # Each source reports each of its three pipeline steps exactly once,
    # and the shared counter never repeats or skips a value.
    steps_by_index: dict[int, list[str]] = {}
    for event in progress:
        steps_by_index.setdefault(event.source_index, []).append(event.step.value)
    assert {i: sorted(v) for i, v in steps_by_index.items()} == {
        i: ["add_metadata", "decode", "write_tiff"] for i in range(3)
    }
    assert [e.completed for e in progress] == list(range(1, 10))
    assert {e.total for e in progress} == {9}


# --- workers return status and paths, never pixels ------------------------


@requires_real_samples
def test_thread_workers_never_return_image_arrays_to_the_parent(monkeypatch, tmp_path):
    """Section 3.8: "returns only status and paths. Do not return full
    image arrays to the parent." Checked on the values actually handed
    back by a threaded run, not just on the type's declaration."""
    _install_fast_decode(monkeypatch)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    returned: list = []
    real_stage = pipeline._stage_one_frame

    def _capturing_stage(member, ctx):
        result = real_stage(member, ctx)
        returned.append(result)
        return result

    monkeypatch.setattr(pipeline, "_stage_one_frame", _capturing_stage)

    outcome = run_convert(
        FIXTURES_DIR,
        list(NEGATIVE_1),
        out_dir,
        3,
        run_id="r1",
        jobs=3,
        emit=lambda e: None,
    )

    assert outcome.status == "complete"
    assert len(returned) == len(NEGATIVE_1)
    assert {f.name for f in dataclasses.fields(pipeline._StagedFrame)} == {
        "member",
        "source_index",
        "final_path",
        "scan_clip_fractions",
    }
    for frame in returned:
        assert isinstance(frame, pipeline._StagedFrame)
        for field in dataclasses.fields(frame):
            value = getattr(frame, field.name)
            assert not isinstance(value, np.ndarray)
            assert isinstance(value, (str, int, Path, tuple))


# --- inner TIFF compression stays at one worker ---------------------------


@requires_real_samples
def test_tiff_compression_uses_one_inner_worker_even_when_outer_threads_run(
    monkeypatch, tmp_path
):
    """Section 3.4/3.8: "Set `tifffile`'s compression `maxworkers=1`; outer
    RAW threads own concurrency." Asserted during a genuinely threaded run,
    which is where a regression would matter."""
    _install_fast_decode(monkeypatch)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    seen_maxworkers: list = []
    real_imwrite = tifffile.imwrite

    def _recording_imwrite(*args, **kwargs):
        seen_maxworkers.append(kwargs.get("maxworkers"))
        return real_imwrite(*args, **kwargs)

    monkeypatch.setattr("scanny_boy.tiff_writer.tifffile.imwrite", _recording_imwrite)

    outcome = run_convert(
        FIXTURES_DIR,
        list(NEGATIVE_1),
        out_dir,
        3,
        run_id="r1",
        jobs=3,
        emit=lambda e: None,
    )

    assert outcome.status == "complete"
    assert len(seen_maxworkers) == len(NEGATIVE_1)
    assert set(seen_maxworkers) == {1}


# --- the memory budget reaches run_convert --------------------------------


@requires_real_samples
def test_an_explicit_jobs_over_the_memory_budget_fails_before_any_work(
    monkeypatch, tmp_path
):
    decode_calls: list[Path] = []
    monkeypatch.setattr(
        raw_decode, "decode_raw", lambda p: decode_calls.append(p) or _fast_frame()
    )
    monkeypatch.setattr(
        concurrency, "physical_memory_bytes", lambda: 2 * concurrency.WORKER_MEMORY_BUDGET_BYTES
    )
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with pytest.raises(ConvertFailure) as excinfo:
        run_convert(
            FIXTURES_DIR,
            list(NEGATIVE_1),
            out_dir,
            3,
            run_id="r1",
            jobs=12,
            emit=lambda e: None,
        )

    assert excinfo.value.code.value == "INSUFFICIENT_MEMORY"
    assert decode_calls == []
    # Nothing was written: the check happens before the output folder is
    # even touched.
    assert list(out_dir.iterdir()) == []


@requires_real_samples
def test_the_default_worker_count_is_reduced_rather_than_rejected(monkeypatch, tmp_path):
    _install_fast_decode(monkeypatch)
    # A machine whose half-RAM budget holds exactly one worker.
    monkeypatch.setattr(
        concurrency, "physical_memory_bytes", lambda: 2 * concurrency.WORKER_MEMORY_BUDGET_BYTES
    )
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    outcome = run_convert(
        FIXTURES_DIR,
        list(NEGATIVE_1),
        out_dir,
        3,
        run_id="r1",
        jobs=None,
        emit=lambda e: None,
    )

    assert outcome.status == "complete"
    assert outcome.workers == 1


# --- cancellation ---------------------------------------------------------


def _install_gated_decode(
    monkeypatch, gate_on: str | set[str], *, parked_target: int = 1
) -> tuple[threading.Event, threading.Event, list[str]]:
    """Block the decode of every frame named in `gate_on` until the test
    releases it.

    Returns `(started, release, decoded)`: `started` is set once
    `parked_target` gated frames are simultaneously parked inside decode
    — the signal a cancellation test waits on instead of sleeping — and
    `release` lets them all return.

    Waiting for a *count* rather than the first arrival is what makes the
    threaded tests deterministic. Setting the flag on the first worker
    would leave every other worker racing the test's `token.cancel()`,
    and a worker that lost that race would stop at its first step
    boundary instead of reaching decode at all.
    """
    gated = {gate_on} if isinstance(gate_on, str) else set(gate_on)
    started = threading.Event()
    release = threading.Event()
    decoded: list[str] = []
    state = {"parked": 0}
    lock = threading.Lock()

    def _fake_decode(path: Path, **_kwargs) -> raw_decode.DecodedFrame:
        with lock:
            decoded.append(path.name)
        if path.name in gated:
            with lock:
                state["parked"] += 1
                if state["parked"] >= parked_target:
                    started.set()
            assert release.wait(timeout=30), "the gated decode was never released"
        return _fast_frame()

    monkeypatch.setattr(raw_decode, "decode_raw", _fake_decode)
    return started, release, decoded


def _run_convert_in_thread(**kwargs) -> tuple[threading.Thread, dict]:
    result: dict = {}

    def _target() -> None:
        try:
            result["outcome"] = run_convert(**kwargs)
        except Exception as exc:  # noqa: BLE001 - surfaced by the caller's assertions
            result["error"] = exc

    thread = threading.Thread(target=_target, name="convert-under-test")
    thread.start()
    return thread, result


@requires_real_samples
def test_cancellation_discards_the_current_group_and_keeps_completed_ones(
    monkeypatch, tmp_path
):
    """Section 3.6: "Completed groups remain after cancellation. The group
    being processed is not published." Cancellation is requested only once
    the second negative's first frame has demonstrably begun decoding."""
    started, release, _decoded = _install_gated_decode(monkeypatch, gate_on=NEGATIVE_2[0])
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    input_dir = stage_samples(tmp_path, list(REAL_SAMPLE_FILES))
    events: list = []
    token = CancellationToken()

    thread, result = _run_convert_in_thread(
        input_dir=input_dir,
        files=list(REAL_SAMPLE_FILES),
        output_dir=out_dir,
        per_negative=3,
        run_id="r1",
        jobs=1,
        cancel=token,
        emit=events.append,
    )

    assert started.wait(timeout=30), "the second negative never started decoding"
    token.cancel()
    release.set()
    thread.join(timeout=60)
    assert not thread.is_alive()
    assert "error" not in result, result.get("error")

    outcome = result["outcome"]
    assert outcome.status == "cancelled"

    # The first negative survives; the second is gone entirely.
    for name in NEGATIVE_1:
        assert (out_dir / f"{_stem(name)}.tif").exists()
    for name in NEGATIVE_2:
        assert not (out_dir / f"{_stem(name)}.tif").exists()

    # The manifest records the cancellation and no staging work remains.
    manifest = load_manifest(out_dir)
    assert manifest.status == "cancelled"
    assert manifest.finished_at is not None
    assert manifest.groups[0].status == "completed"
    assert manifest.groups[1].status == "pending"
    assert [p for p in out_dir.iterdir() if p.name.endswith(STAGING_SUFFIX)] == []

    # No item_done or group_done for the cancelled negative, and no
    # group_failed either: a cancelled group is abandoned, not failed.
    item_done = {e.output for e in events if type(e).__name__ == "ItemDone"}
    assert item_done == {f"{_stem(n)}.tif" for n in NEGATIVE_1}
    assert [e.group_id for e in events if type(e).__name__ == "GroupDone"] == [
        "negative-01"
    ]
    assert [e for e in events if type(e).__name__ == "GroupFailed"] == []


@requires_real_samples
def test_a_group_staged_but_not_yet_published_when_cancelled_is_still_discarded(
    monkeypatch, tmp_path
):
    """The narrow window between the last frame staging and the first
    `os.replace`. Section 3.6 calls a group that has not been published
    "the group being processed", so it must be dropped."""
    _install_fast_decode(monkeypatch)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    token = CancellationToken()

    real_replace = os.replace

    def _cancel_on_first_publish(src, dst):
        if Path(dst).suffix == ".tif":
            raise AssertionError("nothing may be published after cancellation")
        real_replace(src, dst)

    # Cancel the instant the group finishes staging, by hooking the last
    # step every frame performs.
    real_finalize = pipeline.finalize_tiff
    staged = {"count": 0}

    def _finalize_then_maybe_cancel(base_path, final_path, fields):
        real_finalize(base_path, final_path, fields)
        staged["count"] += 1
        if staged["count"] == len(NEGATIVE_1):
            token.cancel()

    monkeypatch.setattr(pipeline, "finalize_tiff", _finalize_then_maybe_cancel)
    monkeypatch.setattr("scanny_boy.pipeline.os.replace", _cancel_on_first_publish)

    outcome = run_convert(
        FIXTURES_DIR,
        list(NEGATIVE_1),
        out_dir,
        3,
        run_id="r1",
        jobs=1,
        cancel=token,
        emit=lambda e: None,
    )

    assert outcome.status == "cancelled"
    for name in NEGATIVE_1:
        assert not (out_dir / f"{_stem(name)}.tif").exists()
    assert [p for p in out_dir.iterdir() if p.name.endswith(STAGING_SUFFIX)] == []


@requires_real_samples
def test_queued_work_is_cancelled_and_workers_stop_before_staging_is_deleted(
    monkeypatch, tmp_path
):
    """Section 3.8: "Wait for every running worker to stop before deleting
    the current staging directory... Never delete a directory while a
    worker may still write to it."

    One group of six frames with two workers: two frames run, four sit in
    the queue. Both running frames are blocked in decode until the test
    cancels, so the queued four can be proven never to have decoded.
    """
    started, release, decoded = _install_gated_decode(
        monkeypatch, gate_on=set(REAL_SAMPLE_FILES), parked_target=2
    )
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    input_dir = stage_samples(tmp_path, list(REAL_SAMPLE_FILES))
    token = CancellationToken()

    active = {"count": 0}
    active_lock = threading.Lock()
    active_at_delete: list[int] = []
    real_stage = pipeline._stage_one_frame

    def _counting_stage(member, ctx):
        with active_lock:
            active["count"] += 1
        try:
            return real_stage(member, ctx)
        finally:
            with active_lock:
                active["count"] -= 1

    real_rmtree = shutil.rmtree

    def _recording_rmtree(path, *args, **kwargs):
        with active_lock:
            active_at_delete.append(active["count"])
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(pipeline, "_stage_one_frame", _counting_stage)
    monkeypatch.setattr("scanny_boy.pipeline.shutil.rmtree", _recording_rmtree)

    thread, result = _run_convert_in_thread(
        input_dir=input_dir,
        files=list(REAL_SAMPLE_FILES),
        output_dir=out_dir,
        per_negative=6,
        run_id="r1",
        jobs=2,
        cancel=token,
        emit=lambda e: None,
    )

    assert started.wait(timeout=30), "both workers never parked inside decode"
    token.cancel()
    release.set()
    thread.join(timeout=60)
    assert not thread.is_alive()
    assert "error" not in result, result.get("error")
    assert result["outcome"].status == "cancelled"

    # Queued frames were cancelled: only the two the pool was already
    # running ever reached the decode step, and they are the first two in
    # submission order. A frame the pool picks up after cancellation
    # raises at its first step boundary, before decode.
    assert sorted(decoded) == sorted(REAL_SAMPLE_FILES[:2]), decoded

    # Every worker had stopped before the staging directory was removed.
    assert active_at_delete, "the staging directory was never deleted"
    assert set(active_at_delete) == {0}
    assert [p for p in out_dir.iterdir() if p.name.endswith(STAGING_SUFFIX)] == []
    assert list(out_dir.glob("*.tif")) == []


@requires_real_samples
def test_cancelling_before_the_first_group_publishes_nothing(monkeypatch, tmp_path):
    _install_fast_decode(monkeypatch)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    token = CancellationToken()
    token.cancel()

    outcome = run_convert(
        FIXTURES_DIR,
        list(NEGATIVE_1),
        out_dir,
        3,
        run_id="r1",
        cancel=token,
        emit=lambda e: None,
    )

    assert outcome.status == "cancelled"
    assert list(out_dir.glob("*.tif")) == []
    manifest = load_manifest(out_dir)
    assert manifest.status == "cancelled"
    assert all(g.status == "pending" for g in manifest.groups)


# --- concurrency spans the whole run, not just one group -------------------


@requires_real_samples
def test_frames_from_different_groups_stage_concurrently(monkeypatch, tmp_path):
    """The headline fix: with one shot per negative, the old per-group pool
    could never run more than one frame at a time no matter how many
    workers were requested, because each group only ever had one frame to
    give it. A shared run-wide pool lets frames from *different* groups run
    at once instead."""
    started, release, _decoded = _install_gated_decode(
        monkeypatch, gate_on=set(REAL_SAMPLE_FILES[:3]), parked_target=3
    )
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    input_dir = stage_samples(tmp_path, list(REAL_SAMPLE_FILES))
    token = CancellationToken()

    thread, result = _run_convert_in_thread(
        input_dir=input_dir,
        files=list(REAL_SAMPLE_FILES),
        output_dir=out_dir,
        per_negative=1,
        run_id="r1",
        jobs=3,
        cancel=token,
        emit=lambda e: None,
    )

    # Three separate one-frame groups genuinely decoding at once: impossible
    # under the old "workers capped by this group's own frame count" policy.
    assert started.wait(timeout=30), (
        "three different groups' frames never decoded simultaneously"
    )
    release.set()
    thread.join(timeout=60)
    assert not thread.is_alive()
    assert "error" not in result, result.get("error")
    assert result["outcome"].status == "complete"


@requires_real_samples
def test_cancellation_discards_every_group_the_pool_had_already_raced_ahead_on(
    monkeypatch, tmp_path
):
    """With a shared run-wide pool, later groups can finish staging in the
    background before the main loop -- which still publishes one group at a
    time, in canonical order -- reaches them. Cancelling while the loop is
    blocked on an earlier group must discard every one of those
    already-staged-but-unpublished later groups too, not just the one the
    loop was waiting on."""
    gate_file = REAL_SAMPLE_FILES[1]  # negative-02, the group the loop blocks on
    last_file = REAL_SAMPLE_FILES[-1]  # negative-06, races ahead in the background
    started, release, _decoded = _install_gated_decode(monkeypatch, gate_on=gate_file)

    finished_last = threading.Event()
    real_finalize = pipeline.finalize_tiff

    def _finalize_and_flag(base_path, final_path, fields):
        real_finalize(base_path, final_path, fields)
        if final_path.name == f"{_stem(last_file)}.tif":
            finished_last.set()

    monkeypatch.setattr(pipeline, "finalize_tiff", _finalize_and_flag)

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    input_dir = stage_samples(tmp_path, list(REAL_SAMPLE_FILES))
    token = CancellationToken()
    events: list = []

    thread, result = _run_convert_in_thread(
        input_dir=input_dir,
        files=list(REAL_SAMPLE_FILES),
        output_dir=out_dir,
        per_negative=1,
        run_id="r1",
        jobs=3,
        cancel=token,
        emit=events.append,
    )

    assert started.wait(timeout=30), "negative-02 never started decoding"
    assert finished_last.wait(timeout=30), (
        "negative-06 never finished staging in the background while the "
        "loop was still blocked on negative-02"
    )
    # It finished staging into its own directory, but the loop has not
    # reached it yet, so it must not be published yet.
    assert not (out_dir / f"{_stem(last_file)}.tif").exists()

    token.cancel()
    release.set()
    thread.join(timeout=60)
    assert not thread.is_alive()
    assert "error" not in result, result.get("error")

    outcome = result["outcome"]
    assert outcome.status == "cancelled"

    assert (out_dir / f"{_stem(REAL_SAMPLE_FILES[0])}.tif").exists()
    for name in REAL_SAMPLE_FILES[1:]:
        assert not (out_dir / f"{_stem(name)}.tif").exists()
    assert [p for p in out_dir.iterdir() if p.name.endswith(STAGING_SUFFIX)] == []

    manifest = load_manifest(out_dir)
    assert manifest.status == "cancelled"
    assert manifest.groups[0].status == "completed"
    assert all(g.status == "pending" for g in manifest.groups[1:])
    assert [e.group_id for e in events if type(e).__name__ == "GroupDone"] == [
        "negative-01"
    ]


# --- flat-field correction ------------------------------------------------


def _save_flatfield_profile(tmp_path, *, name: str, width: int, height: int, value: float = 1.5):
    from scanny_boy import flatfield
    from scanny_boy.library import repo

    gain_map = np.full((8, 8, 3), value, dtype=np.float32)
    path, sha256 = flatfield.save_gain_map(f"pid-{name}", gain_map)
    profile = flatfield.FlatFieldProfile(
        profile_id=f"pid-{name}",
        name=name,
        gain_map_path=str(path),
        gain_map_sha256=sha256,
        source_path="/refs/bare.NEF",
        reference_width=width,
        reference_height=height,
        params=flatfield.build_params(),
        scanny_boy_version="0.3.0",
        created_at="2026-09-01T00:00:00Z",
    )
    repo.save_flatfield_profile(profile)
    return profile


@requires_real_samples
def test_convert_with_flatfield_applies_the_gain_and_tokens_the_manifest(monkeypatch, tmp_path):
    _install_fast_decode(monkeypatch)
    # The fake decode returns 12x8 frames; the run plans for the size
    # `read_active_size` reports, so keep the two consistent.
    monkeypatch.setattr(raw_decode, "read_active_size", lambda p: (12, 8))
    profile = _save_flatfield_profile(tmp_path, name="Boost", width=12, height=8)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    outcome = run_convert(
        FIXTURES_DIR, list(NEGATIVE_1), out_dir, 3, run_id="r1",
        emit=lambda e: None, flatfield_profile_id=profile.profile_id,
    )
    corrected_out = tmp_path / "out-plain"
    corrected_out.mkdir()
    run_convert(FIXTURES_DIR, list(NEGATIVE_1), corrected_out, 3, run_id="r2", emit=lambda e: None)

    assert outcome.status == "complete"
    manifest = load_manifest(out_dir)
    assert manifest.processing_params["flat_field"] == flatfield.profile_token(profile)

    for name in manifest.all_expected_outputs():
        corrected = tifffile.imread(out_dir / name)
        plain = tifffile.imread(corrected_out / name)
        # A constant 1.5x gain is multiplicative only in linear light, so the
        # gamma-encoded output rises everywhere but not linearly.
        assert corrected.mean() > plain.mean()


@requires_real_samples
def test_convert_without_flatfield_carries_no_flat_field_key(monkeypatch, tmp_path):
    _install_fast_decode(monkeypatch)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    run_convert(FIXTURES_DIR, list(NEGATIVE_1), out_dir, 3, run_id="r1", emit=lambda e: None)

    manifest = load_manifest(out_dir)
    # Absent, not null (section 2.4): a no-profile run still compares equal
    # to a pre-flat-field roll.
    assert "flat_field" not in manifest.processing_params


@requires_real_samples
def test_convert_with_an_unknown_profile_fails_before_the_output_is_touched(monkeypatch, tmp_path):
    _install_fast_decode(monkeypatch)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with pytest.raises(ConvertFailure) as excinfo:
        run_convert(
            FIXTURES_DIR, list(NEGATIVE_1), out_dir, 3, run_id="r1",
            emit=lambda e: None, flatfield_profile_id="nope",
        )

    assert excinfo.value.code == Code.FLATFIELD_PROFILE_NOT_FOUND
    assert list(out_dir.iterdir()) == []


@requires_real_samples
def test_convert_warns_when_the_reference_aspect_differs(monkeypatch, tmp_path):
    _install_fast_decode(monkeypatch)
    monkeypatch.setattr(raw_decode, "read_active_size", lambda p: (12, 8))
    # 4x3 is 12.5% off 12x8's 1.5 ratio — past the 1% gate.
    profile = _save_flatfield_profile(tmp_path, name="Portrait", width=4, height=3)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    warnings: list[str] = []

    outcome = run_convert(
        FIXTURES_DIR, list(NEGATIVE_1), out_dir, 3, run_id="r1",
        emit=lambda e: warnings.append(e.code.value) if isinstance(e, WarningEvent) else None,
        flatfield_profile_id=profile.profile_id,
    )

    assert outcome.status == "complete"
    assert "FLATFIELD_ASPECT_MISMATCH" in warnings


@requires_real_samples
def test_convert_warns_when_the_correction_clips_highlights(monkeypatch, tmp_path):
    _install_fast_decode(monkeypatch)
    monkeypatch.setattr(raw_decode, "read_active_size", lambda p: (12, 8))
    # _fast_frame's top rows sit near full scale; GAIN_MAX pushes them past it.
    profile = _save_flatfield_profile(tmp_path, name="Hot", width=12, height=8, value=4.0)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    warnings: list[str] = []

    outcome = run_convert(
        FIXTURES_DIR, list(NEGATIVE_1), out_dir, 3, run_id="r1",
        emit=lambda e: warnings.append(e.code.value) if isinstance(e, WarningEvent) else None,
        flatfield_profile_id=profile.profile_id,
    )

    assert outcome.status == "complete"
    assert "FLATFIELD_HIGHLIGHT_CLIPPED" in warnings


def test_missing_lens_model_emits_the_consistency_warning(monkeypatch, tmp_path):
    """Optional-tag warnings reach `probe`'s stream; `run_convert` must emit
    them too, so the same selection never warns under one command and stays
    silent under another."""
    from fractions import Fraction

    from scanny_boy.metadata import SourceSettings

    def fake_settings(path: str):
        return SourceSettings(
            filename=Path(path).name,
            exposure_time=Fraction(1, 30),
            f_number=Fraction(8, 1),
            iso=100,
            focal_length=Fraction(55, 1),
            lens_model=None,
            orientation=1,
            camera_whitebalance=(1.69, 1.0, 1.38, 1.0),
            make="NIKON CORPORATION",
            model="NIKON Z f",
        )

    monkeypatch.setattr(pipeline, "read_source_settings", fake_settings)

    events: list = []
    pipeline._read_settings_and_check_consistency(
        tmp_path, ["a.NEF", "b.NEF"], emit=events.append, run_id="run-1"
    )

    warnings = [e for e in events if isinstance(e, WarningEvent)]
    assert [w.code for w in warnings] == [Code.CAPTURE_METADATA_MISSING] * 2
    assert all("lens" in w.message.lower() for w in warnings)
    assert all(w.run_id == "run-1" for w in warnings)


# --- grid plumbing (docs/GRID_STITCH_PLAN.md section 2.2) -----------------


@requires_real_samples
def test_run_convert_records_the_declared_grid_on_the_work_manifest(
    monkeypatch, tmp_path
):
    _install_fast_decode(monkeypatch)
    input_dir = stage_samples(tmp_path, list(REAL_SAMPLE_FILES))
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    outcome = run_convert(
        input_dir,
        list(REAL_SAMPLE_FILES),
        out_dir,
        6,
        run_id="r1",
        emit=lambda e: None,
        grid=GridSpec(across=3, down=2),
    )

    assert outcome.status == "complete"
    manifest = load_manifest(out_dir)
    assert manifest.grid == {"across": 3, "down": 2}
    assert manifest.shots_per_negative == 6
    assert manifest.grid_spec == GridSpec(across=3, down=2)


@requires_real_samples
def test_run_convert_rejects_a_grid_whose_product_is_not_per_negative(
    monkeypatch, tmp_path
):
    _install_fast_decode(monkeypatch)
    input_dir = stage_samples(tmp_path, list(NEGATIVE_1))
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with pytest.raises(ConvertFailure) as exc_info:
        run_convert(
            input_dir,
            list(NEGATIVE_1),
            out_dir,
            3,
            run_id="r1",
            emit=lambda e: None,
            grid=GridSpec(across=3, down=2),
        )

    assert exc_info.value.code is Code.INVALID_GRID


@requires_real_samples
def test_run_convert_without_grid_records_a_none_grid_and_a_strip_spec(
    monkeypatch, tmp_path
):
    """Without `grid`, the manifest's `grid` key stays None — the pre-grid
    shape — and `grid_spec` falls back to the strip `across=per_negative,
    down=1`."""
    _install_fast_decode(monkeypatch)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    outcome = run_convert(
        FIXTURES_DIR, list(NEGATIVE_1), out_dir, 3, run_id="r1", emit=lambda e: None
    )

    assert outcome.status == "complete"
    manifest = load_manifest(out_dir)
    assert manifest.grid is None
    assert manifest.grid_spec == GridSpec(across=3, down=1)
