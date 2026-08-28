"""Chunk 5's own test list: manifest writing/fsync ordering, staging and
publish mechanics, disk checks, overwrite/rerun handling, and crash
recovery.

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

import datetime
import json
import os
import shutil
from pathlib import Path

import numpy as np
import pytest

from scanny_boy import disk_check, raw_decode
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
    REAL_SAMPLE_FILES,
    requires_real_samples,
)

MANIFEST_SCHEMA = load_manifest_schema()
FILM_DATE = datetime.date(2026, 8, 2)


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
    def _fake_decode(path: Path) -> raw_decode.DecodedFrame:
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
        FIXTURES_DIR, list(NEGATIVE_1), out_dir, FILM_DATE, 3, run_id="r1", emit=lambda e: None
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
        FIXTURES_DIR, list(NEGATIVE_1), out_dir, FILM_DATE, 3, run_id="r1", emit=lambda e: None
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

    outcome = run_convert(
        FIXTURES_DIR,
        list(REAL_SAMPLE_FILES),
        out_dir,
        FILM_DATE,
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

    run_convert(
        FIXTURES_DIR,
        list(REAL_SAMPLE_FILES),
        out_dir,
        FILM_DATE,
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
            FIXTURES_DIR, list(NEGATIVE_1), out_dir, FILM_DATE, 3, run_id="r1", emit=lambda e: None
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
        FIXTURES_DIR, list(NEGATIVE_1), out_dir, FILM_DATE, 3, run_id="r1", emit=lambda e: None
    )
    assert first.status == "complete"

    with pytest.raises(ConvertFailure) as excinfo:
        run_convert(
            FIXTURES_DIR, list(NEGATIVE_1), out_dir, FILM_DATE, 3, run_id="r2", emit=lambda e: None
        )
    assert excinfo.value.code.value == "OUTPUT_CONFLICT"
    for name in NEGATIVE_1:
        assert _stem(name) in excinfo.value.message

    third = run_convert(
        FIXTURES_DIR,
        list(NEGATIVE_1),
        out_dir,
        FILM_DATE,
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


@requires_real_samples
def test_rerun_with_a_different_film_date_is_manifest_mismatch(monkeypatch, tmp_path):
    _install_fast_decode(monkeypatch)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    run_convert(FIXTURES_DIR, list(NEGATIVE_1), out_dir, FILM_DATE, 3, run_id="r1", emit=lambda e: None)

    with pytest.raises(ConvertFailure) as excinfo:
        run_convert(
            FIXTURES_DIR,
            list(NEGATIVE_1),
            out_dir,
            datetime.date(2026, 9, 1),
            3,
            run_id="r2",
            emit=lambda e: None,
        )
    assert excinfo.value.code.value == "MANIFEST_MISMATCH"


@requires_real_samples
def test_unreadable_manifest_is_bad_manifest(monkeypatch, tmp_path):
    _install_fast_decode(monkeypatch)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    run_convert(FIXTURES_DIR, list(NEGATIVE_1), out_dir, FILM_DATE, 3, run_id="r1", emit=lambda e: None)

    (out_dir / MANIFEST_FILENAME).write_text("not valid json")

    with pytest.raises(ConvertFailure) as excinfo:
        run_convert(
            FIXTURES_DIR, list(NEGATIVE_1), out_dir, FILM_DATE, 3, run_id="r2", emit=lambda e: None
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
            FIXTURES_DIR, list(NEGATIVE_1), out_dir, FILM_DATE, 3, run_id="r1", emit=lambda e: None
        )
    assert excinfo.value.code.value == "OUTPUT_NOT_EMPTY"


@requires_real_samples
def test_output_folder_equal_to_input_folder_is_rejected(monkeypatch, tmp_path):
    _install_fast_decode(monkeypatch)
    with pytest.raises(ConvertFailure) as excinfo:
        run_convert(
            FIXTURES_DIR, list(NEGATIVE_1), FIXTURES_DIR, FILM_DATE, 3, run_id="r1", emit=lambda e: None
        )
    assert excinfo.value.code.value == "OUTPUT_SAME_AS_INPUT"


# --- source changed after hashing ------------------------------------------


@requires_real_samples
def test_source_changed_after_hashing_stops_its_group(monkeypatch, tmp_path):
    input_dir = _copy_samples(tmp_path, list(NEGATIVE_1))
    mutated = input_dir / NEGATIVE_1[1]

    def _decode_then_mutate(path: Path) -> raw_decode.DecodedFrame:
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
        input_dir, list(NEGATIVE_1), out_dir, FILM_DATE, 3, run_id="r1", emit=lambda e: None
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
            FIXTURES_DIR, list(NEGATIVE_1), out_dir, FILM_DATE, 3, run_id="run-a", emit=lambda e: None
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
        FIXTURES_DIR, list(NEGATIVE_1), out_dir, FILM_DATE, 3, run_id="run-b", emit=lambda e: None
    )

    assert outcome.status == "complete"
    recovered_manifest = load_manifest(out_dir)
    assert recovered_manifest.run_id == "run-b"
    assert recovered_manifest.groups[0].status == "completed"
    for name in NEGATIVE_1:
        published_path = out_dir / f"{_stem(name)}.tif"
        assert published_path.exists()
    assert [p for p in out_dir.iterdir() if p.name.endswith(STAGING_SUFFIX)] == []
