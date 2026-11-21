"""Default alignment: positional by rank, or the stored pairing for a
re-import of the linked roll."""

from __future__ import annotations

from scanny_boy.framelog_align import default_alignment
from scanny_boy.framelog_support import (
    load_fixture,
    make_sequenced_roll,
    negative_id,
)
from scanny_boy.roll_manifest import load_roll_manifest, write_roll_manifest


def _roll_42():
    data = load_fixture("roll_42")
    return data["roll"], data["frames"]


def test_positional_alignment_covers_missing_numbers(tmp_path):
    manifest = load_roll_manifest(make_sequenced_roll(tmp_path, 7))
    roll, frames = _roll_42()
    result = default_alignment(manifest, roll, frames)
    # Frame 4 is in missingFrameNumbers but is still assigned.
    assert result == [(negative_id(i), i) for i in range(1, 8)]


def test_positional_alignment_beyond_last_frame_is_none(tmp_path):
    manifest = load_roll_manifest(make_sequenced_roll(tmp_path, 9))
    roll, frames = _roll_42()
    result = default_alignment(manifest, roll, frames)
    assert [number for _, number in result] == [1, 2, 3, 4, 5, 6, 7, None, None]


def test_positional_alignment_with_empty_roll(tmp_path):
    manifest = load_roll_manifest(make_sequenced_roll(tmp_path, 2))
    data = load_fixture("roll_empty")
    result = default_alignment(manifest, data["roll"], data["frames"])
    assert result == [(negative_id(1), None), (negative_id(2), None)]


def test_alignment_follows_sequence_order_not_manifest_order(tmp_path):
    roll_dir = make_sequenced_roll(tmp_path, 3)
    manifest = load_roll_manifest(roll_dir)
    manifest.negatives.reverse()
    roll, frames = _roll_42()
    result = default_alignment(manifest, roll, frames)
    assert result == [(negative_id(i), i) for i in (1, 2, 3)]


def _link(tmp_path, count, pairs, uid="k3j9x0a1bc"):
    roll_dir = make_sequenced_roll(tmp_path, count)
    manifest = load_roll_manifest(roll_dir)
    manifest.metadata.framelog_roll_uid = uid
    by_id = {n.negative_id: n for n in manifest.negatives}
    for index, frame_uid in pairs.items():
        by_id[negative_id(index)].framelog_frame_uid = frame_uid
    write_roll_manifest(roll_dir, manifest)
    return load_roll_manifest(roll_dir)


def test_linked_roll_reuses_stored_pairs_and_leaves_the_rest_unmatched(tmp_path):
    # Negative 1 holds frame 5's uid, negative 2 frame 6's; others unlinked.
    manifest = _link(tmp_path, 4, {1: "d2m7p0s3vx", 2: "e9w5h1j4ka"})
    roll, frames = _roll_42()
    result = default_alignment(manifest, roll, frames)
    assert [number for _, number in result] == [5, 6, None, None]


def test_stored_pairs_that_break_monotonic_order_are_dropped(tmp_path):
    # Negative 3 would pair with frame 2 after negative 2 paired with 6.
    manifest = _link(tmp_path, 3, {1: "a8f2m4q9zz", 2: "e9w5h1j4ka", 3: "b7k2n5r8tw"})
    roll, frames = _roll_42()
    result = default_alignment(manifest, roll, frames)
    assert [number for _, number in result] == [1, 6, None]


def test_equal_stored_numbers_keep_the_earlier(tmp_path):
    manifest = _link(tmp_path, 2, {1: "a8f2m4q9zz", 2: "a8f2m4q9zz"})
    roll, frames = _roll_42()
    result = default_alignment(manifest, roll, frames)
    assert [number for _, number in result] == [1, None]


def test_linked_but_no_matching_frame_uid_falls_back_to_positional(tmp_path):
    manifest = _link(tmp_path, 3, {1: "0000000000"})
    roll, frames = _roll_42()
    assert [n for _, n in default_alignment(manifest, roll, frames)] == [1, 2, 3]


def test_other_linked_roll_ignores_stored_uids(tmp_path):
    manifest = _link(tmp_path, 3, {1: "e9w5h1j4ka"}, uid="zzzzzzzzzz")
    roll, frames = _roll_42()
    assert [n for _, n in default_alignment(manifest, roll, frames)] == [1, 2, 3]
