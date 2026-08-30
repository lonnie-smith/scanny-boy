import json

import pytest

from scanny_boy.manifest import BadManifestError, ManifestMismatchError, SourceRecord
from scanny_boy.roll_manifest import (
    ROLL_MANIFEST_FILENAME,
    FrameRecord,
    NegativeRecord,
    PairRecord,
    RollManifest,
    check_roll_rerun_matches,
    current_roll_manifest_path,
    estimate_roll_manifest_size,
    load_roll_manifest,
    validate_roll_manifest_dict,
    write_roll_manifest,
)
from scanny_boy.roll_manifest_schema_test_support import (
    assert_matches_roll_manifest_schema,
    load_roll_manifest_schema,
)

_SHA = "a" * 64
_OTHER_SHA = "b" * 64


def _source(filename: str = "_DSC4638.NEF", sha256: str = _SHA) -> SourceRecord:
    return SourceRecord(
        filename=filename,
        absolute_path=f"/tmp/{filename}",
        size=123,
        mtime=1.0,
        sha256=sha256,
    )


def _negative(**overrides) -> NegativeRecord:
    defaults = {
        "negative_id": "g1",
        "members": ["_DSC4638.NEF", "_DSC4639.NEF"],
        "expected_output": "_DSC4638.tif",
        "fill_color": (0, 0, 0),
    }
    defaults.update(overrides)
    return NegativeRecord(**defaults)


def _manifest(**overrides) -> RollManifest:
    defaults = {
        "scanny_boy_version": "0.1.0",
        "run_id": "run-1",
        "status": "running",
        "input_folder": "/tmp/in",
        "film_date": "2026-08-02",
        "shots_per_negative": 2,
        "convert_run_id": "convert-1",
        "processing_params": {"gamma": [1.8, 16]},
        "icc_profile": {"name": "ProPhoto-v4.icc", "sha256": _SHA},
        "stitch_params": {"detector": "AKAZE"},
        "source_order": ["_DSC4638.NEF", "_DSC4639.NEF"],
        "sources": [_source("_DSC4638.NEF"), _source("_DSC4639.NEF")],
        "negatives": [_negative()],
        "started_at": "2026-08-02T00:00:00Z",
    }
    defaults.update(overrides)
    return RollManifest(**defaults)


def _completed_negative() -> NegativeRecord:
    return _negative(
        status="completed",
        output={
            "name": "_DSC4638.tif",
            "size": 4096,
            "sha256": _SHA,
            "width": 2080,
            "height": 730,
        },
        frames=[
            FrameRecord(name="_DSC4638.tif", rotation_deg=0.0, translation=(0.0, 0.0)),
            FrameRecord(name="_DSC4639.tif", rotation_deg=1.5, translation=(900.0, 3.0)),
        ],
        pairs=[
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
        global_rms_px=1.2,
        canvas=(2080, 730),
        valid_rect=(10, 10, 2000, 700),
    )


def test_round_trips_through_disk(tmp_path):
    manifest = _manifest(negatives=[_completed_negative()], status="complete")
    write_roll_manifest(tmp_path, manifest)

    assert current_roll_manifest_path(tmp_path).name == ROLL_MANIFEST_FILENAME
    loaded = load_roll_manifest(tmp_path)

    assert loaded.to_dict() == manifest.to_dict()
    assert loaded.negative("g1").canvas == (2080, 730)
    assert loaded.negative("g1").valid_rect == (10, 10, 2000, 700)
    assert loaded.all_expected_outputs() == ["_DSC4638.tif"]


def test_written_manifest_matches_the_published_schema(tmp_path):
    manifest = _manifest(negatives=[_completed_negative()], status="complete")
    write_roll_manifest(tmp_path, manifest)

    data = json.loads(current_roll_manifest_path(tmp_path).read_text())
    assert_matches_roll_manifest_schema(data, load_roll_manifest_schema())


def test_rebate_deviation_is_always_null(tmp_path):
    # Section 3.12.2: the rebate check cannot be calibrated, so Phase 2
    # records `null` in every negative and never emits
    # STITCH_REBATE_CHECK_FAILED.
    manifest = _manifest(negatives=[_completed_negative()])
    write_roll_manifest(tmp_path, manifest)

    data = json.loads(current_roll_manifest_path(tmp_path).read_text())
    for negative in data["negatives"]:
        assert negative["rebate_deviation_px"] is None


def test_write_is_atomic_and_leaves_no_temp_file(tmp_path):
    write_roll_manifest(tmp_path, _manifest())
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == [ROLL_MANIFEST_FILENAME]


def test_validate_rejects_a_missing_top_level_field():
    data = _manifest().to_dict()
    del data["convert_run_id"]
    with pytest.raises(BadManifestError):
        validate_roll_manifest_dict(data)


def test_validate_rejects_a_wrong_manifest_kind():
    data = _manifest().to_dict()
    data["manifest_kind"] = "convert"
    with pytest.raises(BadManifestError):
        validate_roll_manifest_dict(data)


def test_validate_rejects_a_bad_status():
    data = _manifest().to_dict()
    data["status"] = "halfway"
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


def test_rerun_check_accepts_an_identical_run():
    check_roll_rerun_matches(_manifest(), _manifest(run_id="run-2", status="complete"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_order": ["_DSC4639.NEF", "_DSC4638.NEF"]},
        {"sources": [_source("_DSC4638.NEF", _OTHER_SHA), _source("_DSC4639.NEF")]},
        {"shots_per_negative": 3},
        {"negatives": [_negative(members=["_DSC4638.NEF"])]},
        {"icc_profile": {"name": "ProPhoto-v4.icc", "sha256": _OTHER_SHA}},
        {"film_date": "2026-08-03"},
        {"processing_params": {"gamma": [1, 1]}},
    ],
)
def test_rerun_check_rejects_a_different_run(overrides):
    with pytest.raises(ManifestMismatchError):
        check_roll_rerun_matches(_manifest(), _manifest(**overrides))
