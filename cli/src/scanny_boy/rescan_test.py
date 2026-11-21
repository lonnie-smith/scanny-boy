"""`stitch --replaces` / `--sequence-as`: a tethered re-scan takes over an
earlier negative's slot in the roll (docs/TETHER_PLAN.md §4.5).

The `work_dir` fixture's frames are named `IMG_0N.NEF`; a real re-scan has
new frames, so each test first renames the target's members in the roll
(`OLD_0N.NEF`) so the re-scan's group no longer covers it under the
replacement rule's subset test.
"""

from __future__ import annotations

import pytest

from scanny_boy import hashing, roll_sequence, stitch_pipeline
from scanny_boy.events import Code, NegativeFailed
from scanny_boy.library import repo
from scanny_boy.registration import StitchError
from scanny_boy.roll_manifest import (
    CaptureTime,
    NegativeRecord,
    RollSourceRecord,
    load_roll_manifest,
    mutate_roll_manifest,
)
from scanny_boy.work_dir_support import (
    make_roll_dir,
    make_work_dir,
    run_stitch_with_defaults,
)

TARGET_TIME = "2026-08-02T10:00:00"


def _neighbour(negative_id: str, run_id: str, when: str) -> NegativeRecord:
    return NegativeRecord(
        negative_id=negative_id,
        run_id=run_id,
        members=[f"{negative_id}.NEF"],
        expected_output=f"{negative_id}.tif",
        fill_color=stitch_pipeline.FILL_COLOR,
        status="completed",
        capture_time=CaptureTime(source_datetime_original=when),
        output={
            "name": f"{negative_id}.tif",
            "size": 1,
            "sha256": "0" * 64,
            "width": 1,
            "height": 1,
        },
    )


def _roll_with_target(work_dir, tmp_path, *, failed: bool = False):
    """A roll whose first stitch published `stitch-negative-01` as
    `IMG_00.tif`, then reshaped into a re-scan target: renamed members with
    their own source records, an earlier capture time between two
    neighbours, metadata to carry over, and one edit to drop. With `failed`,
    the target is instead a failed record that never published."""
    out_dir = make_roll_dir(tmp_path)
    run_stitch_with_defaults(work_dir, out_dir, defer_roll_refresh=True)

    def reshape(fresh):
        target = fresh.negative("stitch-negative-01")
        target.members = [f"OLD_0{i}.NEF" for i in range(len(target.members))]
        target.capture_time = CaptureTime(
            source_datetime_original=None if failed else TARGET_TIME,
            date_override="2026-07-30",
            applied_datetime_original=None,
        )
        target.metadata.caption = "the harbour"
        target.framelog_frame_uid = "frame-uid-7"
        if failed:
            target.status = "failed"
            target.output = None
            target.error_code = Code.STITCH_RESIDUAL_TOO_HIGH.value
        fresh.sources.extend(
            RollSourceRecord(
                filename=name,
                absolute_path=f"/tmp/old/{name}",
                size=1,
                mtime=1.0,
                sha256=f"old{name}".ljust(64, "d"),
                run_id=target.run_id,
            )
            for name in target.members
        )
        run_id = target.run_id
        fresh.negatives.insert(0, _neighbour("before", run_id, "2026-08-02T09:00:00"))
        fresh.negatives.append(_neighbour("after", run_id, "2026-08-02T11:00:00"))

    mutate_roll_manifest(out_dir, reshape)
    if failed:
        (out_dir / "IMG_00.tif").unlink()
    else:
        repo.append_edit(out_dir, "stitch-negative-01", "rotate", {"quarter_turns": 1})
    return out_dir


def _rescan(work_dir, out_dir, **kwargs):
    return run_stitch_with_defaults(
        work_dir,
        out_dir,
        run_id="rescan-run",
        defer_roll_refresh=True,
        **kwargs,
    )


def test_a_rescan_takes_over_the_targets_slot_name_and_metadata(work_dir, tmp_path):
    out_dir = _roll_with_target(work_dir, tmp_path)
    old_bytes = (out_dir / "IMG_00.tif").read_bytes()

    outcome = _rescan(work_dir, out_dir, replaces="stitch-negative-01")

    assert outcome.status == "complete"
    assert outcome.published == ["IMG_00.tif"]
    roll = load_roll_manifest(out_dir)
    ids = [n.negative_id for n in roll.negatives]
    assert "stitch-negative-01" not in ids
    (new,) = [n for n in roll.negatives if n.run_id == "rescan-run"]
    # Same slot: between the neighbours, ranked at the target's time.
    assert roll_sequence.sequence_negatives(roll) == [
        "before",
        new.negative_id,
        "after",
    ]
    assert new.capture_time.sequence_datetime_original == TARGET_TIME
    # Same file name, new pixels, and the record owns them.
    assert new.expected_output == new.output["name"] == "IMG_00.tif"
    published = out_dir / "IMG_00.tif"
    assert hashing.sha256_file(published) == new.output["sha256"]
    assert published.read_bytes() != old_bytes
    assert not (out_dir / "IMG_00-2.tif").exists()
    # Metadata carried over; the edits were the old pixels' and are gone.
    assert new.metadata.caption == "the harbour"
    assert new.framelog_frame_uid == "frame-uid-7"
    assert new.capture_time.date_override == "2026-07-30"
    assert not [
        e for e in repo.edits_for(out_dir, new.negative_id) if e["op"] == "rotate"
    ]
    # The old frames' source records are dropped; nothing else is.
    filenames = {s.filename for s in roll.sources}
    assert not {f for f in filenames if f.startswith("OLD_")}
    assert {"IMG_00.NEF", "IMG_01.NEF", "IMG_02.NEF"} <= filenames


def test_a_rescan_of_a_failed_negative_takes_its_expected_name(work_dir, tmp_path):
    out_dir = _roll_with_target(work_dir, tmp_path, failed=True)

    outcome = _rescan(work_dir, out_dir, replaces="stitch-negative-01")

    assert outcome.published == ["IMG_00.tif"]
    roll = load_roll_manifest(out_dir)
    assert "stitch-negative-01" not in [n.negative_id for n in roll.negatives]
    (new,) = [n for n in roll.negatives if n.run_id == "rescan-run"]
    assert new.status == "completed"
    # The failed target recorded no capture time, so its slot is read off its
    # first frame's file -- unreadable here -- and the re-scan ranks at its own.
    assert new.capture_time.sequence_datetime_original is None
    assert roll_sequence.sequence_negatives(roll)[-1] == new.negative_id


def test_an_unknown_target_is_refused_and_writes_nothing(work_dir, tmp_path):
    out_dir = _roll_with_target(work_dir, tmp_path)
    before = load_roll_manifest(out_dir).to_dict()

    with pytest.raises(StitchError) as raised:
        _rescan(work_dir, out_dir, replaces="no-such-negative")

    assert raised.value.code is Code.REPLACE_TARGET_NOT_FOUND
    after = load_roll_manifest(out_dir).to_dict()
    after.pop("updated_at")
    before.pop("updated_at")
    assert after == before


def test_a_rescan_must_be_a_single_negative(tmp_path):
    work_dir = make_work_dir(tmp_path, negatives=2)
    out_dir = make_roll_dir(tmp_path)

    with pytest.raises(StitchError) as raised:
        _rescan(work_dir, out_dir, replaces="anything")

    assert raised.value.code is Code.REPLACE_REQUIRES_SINGLE_NEGATIVE


def test_sequence_as_ranks_at_the_files_capture_time(work_dir, tmp_path):
    out_dir = make_roll_dir(tmp_path)
    anchor_file = work_dir / "IMG_00.tif"  # an intermediate carries the EXIF

    outcome = _rescan(work_dir, out_dir, sequence_as=anchor_file)

    assert outcome.status == "complete"
    (new,) = load_roll_manifest(out_dir).negatives
    assert new.capture_time.sequence_datetime_original == "2026-08-02T12:33:41.450000"


def test_an_unreadable_sequence_as_file_is_refused(work_dir, tmp_path):
    out_dir = make_roll_dir(tmp_path)
    junk = tmp_path / "junk.NEF"
    junk.write_bytes(b"not a raw file")

    with pytest.raises(StitchError) as raised:
        _rescan(work_dir, out_dir, sequence_as=junk)

    assert raised.value.code is Code.WORK_MANIFEST_UNUSABLE


def test_a_rolled_back_publish_leaves_the_target_untouched(
    work_dir, tmp_path, monkeypatch
):
    out_dir = _roll_with_target(work_dir, tmp_path)
    old_bytes = (out_dir / "IMG_00.tif").read_bytes()

    def refuse(*args, **kwargs):
        raise StitchError(Code.FILM_BASE_CHANGED, "the references moved")

    monkeypatch.setattr(stitch_pipeline, "_lock_references", refuse)
    events: list = []
    outcome = _rescan(work_dir, out_dir, replaces="stitch-negative-01", events=events)

    assert outcome.published == []
    assert [e for e in events if isinstance(e, NegativeFailed)]
    assert (out_dir / "IMG_00.tif").read_bytes() == old_bytes
    roll = load_roll_manifest(out_dir)
    target = roll.negative("stitch-negative-01")
    assert target.status == "completed"
    assert target.output["name"] == "IMG_00.tif"
    assert target.metadata.caption == "the harbour"
    assert "rotate" in [e["op"] for e in repo.edits_for(out_dir, "stitch-negative-01")]
    # The failed re-scan never claims the old negative's file name.
    (failed,) = [n for n in roll.negatives if n.run_id == "rescan-run"]
    assert failed.status == "failed"
    assert failed.expected_output != "IMG_00.tif"
