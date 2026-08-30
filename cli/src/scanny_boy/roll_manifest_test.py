import json

import pytest

from scanny_boy.icc_profile import PROFILE_FILENAME, PROFILE_SHA256
from scanny_boy.manifest import BadManifestError, SourceRecord
from scanny_boy.roll_manifest import (
    ROLL_MANIFEST_FILENAME,
    CaptureTime,
    FrameRecord,
    NegativeRecord,
    PairRecord,
    RollInvariantMismatchError,
    RollInvariants,
    RollManifest,
    RollManifestUnsupportedError,
    RunRecord,
    allocate_output_name,
    append_run,
    check_roll_invariants,
    current_roll_manifest_path,
    estimate_roll_manifest_size,
    format_negative_id,
    load_roll_manifest,
    mark_superseded,
    merge_sources,
    new_roll_manifest,
    validate_roll_manifest_dict,
    write_roll_manifest,
)
from scanny_boy.roll_manifest_schema_test_support import (
    assert_matches_roll_manifest_schema,
    load_roll_manifest_schema,
)

_SHA = "a" * 64
_OTHER_SHA = "b" * 64
_ROLL_ID = "8f14e45f-ea2e-4b3f-9c1d-2b7a6c5d4e3f"


def _source(filename: str = "_DSC4638.NEF", sha256: str = _SHA) -> SourceRecord:
    return SourceRecord(
        filename=filename,
        absolute_path=f"/tmp/{filename}",
        size=123,
        mtime=1.0,
        sha256=sha256,
    )


def _run(run_id: str = "run-1", **overrides) -> RunRecord:
    defaults = {
        "run_id": run_id,
        "short_id": "aaaaaa",
        "kind": "stitch",
        "status": "complete",
        "started_at": "2026-08-02T00:00:00Z",
        "convert_run_id": "convert-1",
        "input_folder": None,
        "source_order": ["_DSC4638.NEF", "_DSC4639.NEF"],
        "work_dir": "/tmp/work",
        "finished_at": "2026-08-02T00:10:00Z",
    }
    defaults.update(overrides)
    return RunRecord(**defaults)


def _negative(**overrides) -> NegativeRecord:
    defaults = {
        "negative_id": "aaaaaa-negative-01",
        "run_id": "run-1",
        "members": ["_DSC4638.NEF", "_DSC4639.NEF"],
        "expected_output": "_DSC4638.tif",
        "fill_color": (0, 0, 0),
    }
    defaults.update(overrides)
    return NegativeRecord(**defaults)


def _invariants(**overrides) -> RollInvariants:
    defaults = {
        "shots_per_negative": 2,
        "processing_params": {"gamma": [1.8, 16]},
        "icc_profile_sha256": _SHA,
        "stitch_params": {"detector": "AKAZE"},
    }
    defaults.update(overrides)
    return RollInvariants(**defaults)


def _manifest(**overrides) -> RollManifest:
    """A seeded roll: one run, its sources, and one negative."""
    defaults = {
        "scanny_boy_version": "0.3.0",
        "roll_id": _ROLL_ID,
        "roll_name": "Tri-X, Portland 1998",
        "shots_per_negative": 2,
        "created_at": "2026-08-02T00:00:00Z",
        "updated_at": "2026-08-02T00:00:00Z",
        "processing_params": {"gamma": [1.8, 16]},
        "icc_profile": {"name": PROFILE_FILENAME, "sha256": _SHA},
        "stitch_params": {"detector": "AKAZE"},
        "runs": [_run()],
        "negatives": [_negative()],
    }
    defaults.update(overrides)
    manifest = RollManifest(**defaults)
    if "sources" not in overrides:
        merge_sources(
            manifest, [_source("_DSC4638.NEF"), _source("_DSC4639.NEF", _OTHER_SHA)], "run-1"
        )
    return manifest


def _completed_negative(**overrides) -> NegativeRecord:
    defaults = {
        "status": "completed",
        "sequence": 1,
        "capture_time": CaptureTime(source_datetime_original="2026-08-02T12:33:41.450000"),
        "output": {
            "name": "_DSC4638.tif",
            "size": 4096,
            "sha256": _SHA,
            "width": 2080,
            "height": 730,
        },
        "frames": [
            FrameRecord(name="_DSC4638.tif", rotation_deg=0.0, translation=(0.0, 0.0)),
            FrameRecord(name="_DSC4639.tif", rotation_deg=1.5, translation=(900.0, 3.0)),
        ],
        "pairs": [
            PairRecord(
                a="_DSC4638.tif",
                b="_DSC4639.tif",
                inliers=120,
                good_matches=180,
                inlier_ratio=0.667,
                rms_residual_px=1.2,
                scale_drift=0.0004,
                overlap_fraction=0.35,
                overlap_mad=0.04,
                accepted=True,
            )
        ],
        "global_rms_px": 1.2,
        "canvas": (2080, 730),
        "valid_rect": (10, 10, 2000, 700),
    }
    defaults.update(overrides)
    return _negative(**defaults)


# --- shape, round trip, and the version break ---------------------------


def test_v2_round_trips(tmp_path):
    manifest = _manifest(negatives=[_completed_negative()])
    write_roll_manifest(tmp_path, manifest)

    assert current_roll_manifest_path(tmp_path).name == ROLL_MANIFEST_FILENAME
    loaded = load_roll_manifest(tmp_path)

    assert loaded.to_dict() == manifest.to_dict()
    assert loaded.manifest_format_version == 2
    assert loaded.manifest_kind == "roll"
    assert loaded.roll_id == _ROLL_ID
    assert loaded.run("run-1").short_id == manifest.run("run-1").short_id
    negative = loaded.negative("aaaaaa-negative-01")
    assert negative.canvas == (2080, 730)
    assert negative.valid_rect == (10, 10, 2000, 700)
    assert negative.sequence == 1
    assert negative.superseded_by is None
    assert negative.capture_time.source_datetime_original == "2026-08-02T12:33:41.450000"
    assert negative.capture_time.applied_datetime_original is None
    assert loaded.all_expected_outputs() == ["_DSC4638.tif"]
    assert loaded.metadata.roll_capture_date is None


def test_rejects_format_version_one():
    # Section 0: there is no migration. A Phase 2 manifest is refused with
    # its own code, not silently upgraded and not read as corrupt.
    data = _manifest().to_dict()
    data["manifest_format_version"] = 1
    with pytest.raises(RollManifestUnsupportedError):
        validate_roll_manifest_dict(data)


def test_new_roll_manifest_is_empty_and_schema_valid(tmp_path):
    manifest = new_roll_manifest(
        roll_id=_ROLL_ID, roll_name="Tri-X, Portland 1998", shots_per_negative=3
    )
    assert manifest.runs == []
    assert manifest.sources == []
    assert manifest.negatives == []
    assert manifest.processing_params == {}
    assert manifest.stitch_params == {}
    # Section 5.4: the one thing an empty roll can honestly know is which
    # profile it will embed, because there is exactly one.
    assert manifest.icc_profile == {"name": PROFILE_FILENAME, "sha256": PROFILE_SHA256}

    write_roll_manifest(tmp_path, manifest)
    data = json.loads(current_roll_manifest_path(tmp_path).read_text())
    assert_matches_roll_manifest_schema(data, load_roll_manifest_schema())
    assert load_roll_manifest(tmp_path).to_dict() == manifest.to_dict()


def test_written_manifest_matches_the_published_schema(tmp_path):
    manifest = _manifest(negatives=[_completed_negative()])
    write_roll_manifest(tmp_path, manifest)

    data = json.loads(current_roll_manifest_path(tmp_path).read_text())
    assert_matches_roll_manifest_schema(data, load_roll_manifest_schema())


def test_rebate_deviation_is_always_null(tmp_path):
    # Phase 2 section 3.12.2: the rebate check cannot be calibrated, so every
    # negative records `null` and STITCH_REBATE_CHECK_FAILED is never emitted.
    manifest = _manifest(negatives=[_completed_negative()])
    write_roll_manifest(tmp_path, manifest)

    data = json.loads(current_roll_manifest_path(tmp_path).read_text())
    for negative in data["negatives"]:
        assert negative["rebate_deviation_px"] is None


def test_write_is_atomic_and_leaves_no_temp_file(tmp_path):
    write_roll_manifest(tmp_path, _manifest())
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == [ROLL_MANIFEST_FILENAME]


def test_write_refreshes_updated_at(tmp_path):
    manifest = _manifest()
    before = manifest.updated_at
    write_roll_manifest(tmp_path, manifest)
    assert manifest.updated_at != before
    assert manifest.created_at == "2026-08-02T00:00:00Z"


def test_validate_rejects_a_missing_top_level_field():
    data = _manifest().to_dict()
    del data["metadata"]
    with pytest.raises(BadManifestError):
        validate_roll_manifest_dict(data)


def test_validate_rejects_a_wrong_manifest_kind():
    data = _manifest().to_dict()
    data["manifest_kind"] = "stitch"
    with pytest.raises(BadManifestError):
        validate_roll_manifest_dict(data)


def test_validate_rejects_a_bad_run_status():
    data = _manifest().to_dict()
    data["runs"][0]["status"] = "halfway"
    with pytest.raises(BadManifestError):
        validate_roll_manifest_dict(data)


def test_validate_rejects_a_bad_negative_status():
    data = _manifest().to_dict()
    data["negatives"][0]["status"] = "probably"
    with pytest.raises(BadManifestError):
        validate_roll_manifest_dict(data)


def test_validate_rejects_a_malformed_fill_color():
    data = _manifest().to_dict()
    data["negatives"][0]["fill_color"] = [0, 0]
    with pytest.raises(BadManifestError):
        validate_roll_manifest_dict(data)


def test_validate_rejects_a_missing_capture_time_field():
    data = _manifest().to_dict()
    del data["negatives"][0]["capture_time"]["date_override"]
    with pytest.raises(BadManifestError):
        validate_roll_manifest_dict(data)


def test_validate_rejects_a_source_without_a_run_id():
    data = _manifest().to_dict()
    del data["sources"][0]["run_id"]
    with pytest.raises(BadManifestError):
        validate_roll_manifest_dict(data)


def test_load_rejects_an_output_escaping_the_folder(tmp_path):
    manifest = _manifest(negatives=[_negative(expected_output="../escape.tif")])
    write_roll_manifest(tmp_path, manifest)
    with pytest.raises(BadManifestError):
        load_roll_manifest(tmp_path)


def test_load_rejects_invalid_json(tmp_path):
    current_roll_manifest_path(tmp_path).write_text("{not json")
    with pytest.raises(BadManifestError):
        load_roll_manifest(tmp_path)


def test_estimate_size_is_positive():
    assert estimate_roll_manifest_size(_manifest()) > 0


# --- section 3.4: invariants --------------------------------------------


def test_check_roll_invariants_rejects_changed_per_negative():
    with pytest.raises(RollInvariantMismatchError):
        check_roll_invariants(_manifest(), _invariants(shots_per_negative=3))


@pytest.mark.parametrize(
    "overrides",
    [
        {"processing_params": {"gamma": [1, 1]}},
        {"icc_profile_sha256": _OTHER_SHA},
        {"stitch_params": {"detector": "ORB"}},
    ],
)
def test_check_roll_invariants_rejects_a_changed_invariant(overrides):
    with pytest.raises(RollInvariantMismatchError):
        check_roll_invariants(_manifest(), _invariants(**overrides))


def test_check_roll_invariants_ignores_changed_input_folder():
    """Section 3.4: input folder, source list, order, and grouping are
    *expected* to differ between runs and are never compared. This is
    precisely what Phase 2's `check_roll_rerun_matches` refused."""
    manifest = _manifest()
    append_run(
        manifest,
        _run(
            run_id="run-2",
            input_folder="/somewhere/else/entirely",
            source_order=["_DSC9001.NEF"],
        ),
    )
    merge_sources(manifest, [_source("_DSC9001.NEF", "c" * 64)], "run-2")
    manifest.negatives.append(
        _negative(
            negative_id="run-2-negative-01",
            run_id="run-2",
            members=["_DSC9001.NEF"],
            expected_output="_DSC9001.tif",
        )
    )

    check_roll_invariants(manifest, _invariants())


def test_check_roll_invariants_seeds_on_an_unseeded_roll():
    """Section 5.4: an empty roll has no `processing_params` or
    `stitch_params` yet, so the first run establishes them rather than being
    compared against `{}`. `shots_per_negative` is set at creation and is
    still compared."""
    empty = new_roll_manifest(roll_id=_ROLL_ID, roll_name="Fresh", shots_per_negative=2)

    check_roll_invariants(empty, _invariants())
    assert empty.processing_params == {}, "check must never mutate"

    with pytest.raises(RollInvariantMismatchError):
        check_roll_invariants(empty, _invariants(shots_per_negative=4))


# --- section 3.4: runs and negative ids ---------------------------------


def test_append_run_preserves_earlier_negatives():
    manifest = _manifest()
    first = list(manifest.negatives)

    append_run(manifest, _run(run_id="run-2"))

    assert manifest.negatives == first
    assert [r.run_id for r in manifest.runs] == ["run-1", "run-2"]


def test_append_run_lengthens_a_colliding_short_id():
    """Section 3.4: `run_id` is a UUID, so six hex characters can collide.
    Uniqueness is enforced, not assumed."""
    manifest = _manifest(runs=[], negatives=[])
    a = _run(run_id="abcdef01-2345-4678-9abc-def012345678")
    b = _run(run_id="abcdef01-9999-4678-9abc-def012345678")
    c = _run(run_id="abcdef01-2345-4111-9abc-def012345678")

    append_run(manifest, a)
    append_run(manifest, b)
    append_run(manifest, c)

    assert a.short_id == "abcdef"
    assert b.short_id == "abcdef01"
    # `c` collides at six *and* eight characters, so it lengthens twice.
    assert c.short_id == "abcdef01-2"
    assert len({r.short_id for r in manifest.runs}) == 3


def test_append_run_falls_back_to_the_whole_run_id():
    """Six, eight, and ten characters can all be taken. The fourth run whose
    id agrees that far gets the whole `run_id`."""
    manifest = _manifest(runs=[], negatives=[])
    ids = [f"abcdef01-2345-4678-9abc-def01234567{n}" for n in range(4)]
    runs = [_run(run_id=run_id) for run_id in ids]
    for run in runs:
        append_run(manifest, run)

    assert [r.short_id for r in runs] == [
        "abcdef",
        "abcdef01",
        "abcdef01-2",
        ids[3],
    ]
    assert len({r.short_id for r in manifest.runs}) == 4


def test_negative_ids_unique_across_two_runs():
    manifest = _manifest(runs=[], negatives=[])
    a = _run(run_id="abcdef01-2345-4678-9abc-def012345678")
    b = _run(run_id="abcdef01-9999-4678-9abc-def012345678")
    append_run(manifest, a)
    append_run(manifest, b)

    ids = [format_negative_id(a.short_id, i) for i in (1, 2)]
    ids += [format_negative_id(b.short_id, i) for i in (1, 2)]

    assert ids == [
        "abcdef-negative-01",
        "abcdef-negative-02",
        "abcdef01-negative-01",
        "abcdef01-negative-02",
    ]
    assert len(set(ids)) == 4


# --- section 3.3: sources -----------------------------------------------


def test_merge_sources_deduplicates_by_hash():
    """Section 3.3: `sources` is keyed by `sha256`, so a file already present
    is never appended twice — not from a different folder, and not under a
    different name."""
    manifest = _manifest(sources=[], runs=[], negatives=[])
    merge_sources(manifest, [_source("a.NEF", _SHA)], "run-1")
    merge_sources(
        manifest,
        [
            SourceRecord(
                filename="renamed.NEF",
                absolute_path="/elsewhere/renamed.NEF",
                size=123,
                mtime=99.0,
                sha256=_SHA,
            ),
            _source("b.NEF", _OTHER_SHA),
        ],
        "run-2",
    )

    assert [s.sha256 for s in manifest.sources] == [_SHA, _OTHER_SHA]
    # The first contributor keeps the entry, name and run id included.
    assert manifest.sources[0].filename == "a.NEF"
    assert manifest.sources[0].run_id == "run-1"
    assert manifest.sources[1].run_id == "run-2"


# --- section 3.4: output naming -----------------------------------------


def test_allocate_output_name_suffixes_on_collision():
    manifest = _manifest(negatives=[_negative(expected_output="_DSC4638.tif")])

    name = allocate_output_name(manifest, "_DSC4638.NEF", "run-2-negative-01")

    assert name == "_DSC4638-2.tif"
    manifest.negatives.append(
        _negative(negative_id="run-2-negative-01", expected_output=name)
    )
    assert (
        allocate_output_name(manifest, "_DSC4638.NEF", "run-3-negative-01")
        == "_DSC4638-3.tif"
    )


def test_allocate_output_name_is_stable_across_reordering():
    """Section 3.4: names are assigned once, at publish, and are never
    changed afterwards by reordering, renaming, or re-stitching. Asking again
    for the *same* `negative_id` gets the same answer, whatever order the
    records are in."""
    mine = _negative(negative_id="run-1-negative-01", expected_output="_DSC4638.tif")
    other = _negative(negative_id="run-2-negative-01", expected_output="_DSC4638-2.tif")
    manifest = _manifest(negatives=[mine, other])

    assert allocate_output_name(manifest, "_DSC4638.NEF", "run-1-negative-01") == (
        "_DSC4638.tif"
    )

    manifest.negatives = [other, mine]
    assert allocate_output_name(manifest, "_DSC4638.NEF", "run-1-negative-01") == (
        "_DSC4638.tif"
    )
    assert allocate_output_name(manifest, "_DSC4638.NEF", "run-2-negative-01") == (
        "_DSC4638-2.tif"
    )


def test_allocate_output_name_never_reissues_a_superseded_name():
    """Section 3.4: a superseded negative's record is never removed and its
    output name stays claimed."""
    dead = _negative(
        negative_id="run-1-negative-01",
        expected_output="_DSC4638.tif",
        superseded_by="run-2-negative-01",
        sequence=None,
    )
    replacement = _negative(
        negative_id="run-2-negative-01", expected_output="_DSC4638-2.tif"
    )
    manifest = _manifest(negatives=[dead, replacement])

    assert allocate_output_name(manifest, "_DSC4638.NEF", "run-3-negative-01") == (
        "_DSC4638-3.tif"
    )


# --- section 3.4: supersession ------------------------------------------


def _roll_with(members_by_id: dict[str, list[str]]) -> RollManifest:
    return _manifest(
        negatives=[
            _negative(
                negative_id=nid,
                members=members,
                expected_output=f"{members[0].split('.')[0]}.tif",
                status="completed",
                sequence=i,
            )
            for i, (nid, members) in enumerate(members_by_id.items(), start=1)
        ]
    )


def test_mark_superseded_covers_an_exact_member_match():
    manifest = _roll_with({"old-negative-01": ["a.NEF", "b.NEF"]})
    replacement = _negative(
        negative_id="new-negative-01",
        members=["a.NEF", "b.NEF"],
        expected_output="a-2.tif",
        status="completed",
        sequence=1,
    )
    manifest.negatives.append(replacement)

    superseded = mark_superseded(manifest, replacement)

    assert [n.negative_id for n in superseded] == ["old-negative-01"]
    assert manifest.negative("old-negative-01").superseded_by == "new-negative-01"
    # The record is never removed, and the replacement is untouched.
    assert len(manifest.negatives) == 2
    assert replacement.superseded_by is None


def test_mark_superseded_covers_a_merging_regroup():
    """Two negatives merged into one: every member of each old negative is
    present in the new one, so both are covered."""
    manifest = _roll_with(
        {
            "old-negative-01": ["a.NEF", "b.NEF"],
            "old-negative-02": ["c.NEF"],
            "old-negative-03": ["z.NEF"],
        }
    )
    replacement = _negative(
        negative_id="new-negative-01",
        members=["a.NEF", "b.NEF", "c.NEF"],
        expected_output="a-2.tif",
        status="completed",
    )
    manifest.negatives.append(replacement)

    superseded = mark_superseded(manifest, replacement)

    assert [n.negative_id for n in superseded] == ["old-negative-01", "old-negative-02"]
    # The untouched neighbour keeps its sequence.
    assert manifest.negative("old-negative-03").superseded_by is None
    assert manifest.negative("old-negative-03").sequence == 3


def test_mark_superseded_ignores_a_splitting_regroup():
    """Section 3.4: a regrouping that *splits* one negative covers only part
    of it, so nothing is superseded and both remain. Phase 3 does not clean
    that up."""
    manifest = _roll_with({"old-negative-01": ["a.NEF", "b.NEF", "c.NEF"]})
    replacement = _negative(
        negative_id="new-negative-01",
        members=["a.NEF", "b.NEF"],
        expected_output="a-2.tif",
        status="completed",
    )
    manifest.negatives.append(replacement)

    assert mark_superseded(manifest, replacement) == []
    assert manifest.negative("old-negative-01").superseded_by is None
    assert manifest.negative("old-negative-01").sequence == 1


def test_mark_superseded_nulls_the_sequence():
    manifest = _roll_with(
        {
            "old-negative-01": ["a.NEF"],
            "old-negative-02": ["b.NEF"],
            "old-negative-03": ["c.NEF"],
        }
    )
    replacement = _negative(
        negative_id="new-negative-01",
        members=["b.NEF"],
        expected_output="b-2.tif",
        status="completed",
        sequence=2,
    )
    manifest.negatives.append(replacement)

    mark_superseded(manifest, replacement)

    assert manifest.negative("old-negative-02").sequence is None
    # Section 3.7: the replacement takes the position, so the neighbours do
    # not move.
    assert manifest.negative("old-negative-01").sequence == 1
    assert manifest.negative("old-negative-03").sequence == 3
    assert [n.negative_id for n in manifest.live_negatives()] == [
        "old-negative-01",
        "old-negative-03",
        "new-negative-01",
    ]


def test_mark_superseded_skips_an_already_superseded_negative():
    manifest = _roll_with({"old-negative-01": ["a.NEF"]})
    manifest.negative("old-negative-01").superseded_by = "mid-negative-01"
    manifest.negative("old-negative-01").sequence = None
    replacement = _negative(
        negative_id="new-negative-01", members=["a.NEF"], expected_output="a-3.tif"
    )
    manifest.negatives.append(replacement)

    assert mark_superseded(manifest, replacement) == []
    assert manifest.negative("old-negative-01").superseded_by == "mid-negative-01"
