import json

import pytest

from scanny_boy.manifest import (
    MANIFEST_FILENAME,
    BadManifestError,
    CuratedMetadata,
    GroupRecord,
    Manifest,
    ManifestMismatchError,
    OutputRecord,
    SourceRecord,
    check_rerun_matches,
    estimate_manifest_size,
    load_manifest,
    resolve_within,
    validate_manifest_dict,
    write_manifest,
)
from scanny_boy.manifest_schema_test_support import (
    assert_matches_manifest_schema,
    load_manifest_schema,
)

SCHEMA = load_manifest_schema()

GOOD_SHA = "a" * 64


def _source(filename: str = "_DSC4638.NEF") -> SourceRecord:
    return SourceRecord(
        filename=filename,
        absolute_path=f"/input/{filename}",
        size=32001076,
        mtime=1754145207.0,
        sha256=GOOD_SHA,
    )


def _curated() -> CuratedMetadata:
    return CuratedMetadata(
        exposure_time="1/30",
        f_number="8",
        iso=100,
        focal_length="55",
        lens_model="55mm f/2.8",
        orientation=1,
        camera_whitebalance=(1.691406, 1.0, 1.378906, 1.0),
    )


def _group(**overrides) -> GroupRecord:
    defaults = {
        "group_id": "negative-01",
        "members": ["_DSC4638.NEF"],
        "expected_outputs": ["_DSC4638.tif"],
    }
    defaults.update(overrides)
    return GroupRecord(**defaults)


def _manifest(**overrides) -> Manifest:
    defaults = {
        "scanny_boy_version": "0.1.0",
        "run_id": "run-1",
        "status": "running",
        "input_folder": "/input",
        "film_date": "2026-08-02",
        "shots_per_negative": 3,
        "processing_params": {"output_bps": 16},
        "icc_profile": {"name": "ProPhoto-v4.icc", "sha256": GOOD_SHA},
        "source_order": ["_DSC4638.NEF"],
        "sources": [_source()],
        "curated_metadata": _curated(),
        "groups": [_group()],
        "started_at": "2026-08-28T12:00:00+00:00",
        "finished_at": None,
    }
    defaults.update(overrides)
    return Manifest(**defaults)


# --- round trip / atomic write --------------------------------------------


def test_write_then_load_round_trips(tmp_path):
    manifest = _manifest()
    write_manifest(tmp_path, manifest)

    loaded = load_manifest(tmp_path)

    assert loaded.run_id == manifest.run_id
    assert loaded.source_order == manifest.source_order
    assert loaded.sources[0].sha256 == GOOD_SHA
    assert loaded.curated_metadata.camera_whitebalance == (1.691406, 1.0, 1.378906, 1.0)
    assert loaded.groups[0].group_id == "negative-01"
    assert loaded.groups[0].members == ["_DSC4638.NEF"]


def test_write_manifest_leaves_no_temp_file(tmp_path):
    write_manifest(tmp_path, _manifest())

    entries = {p.name for p in tmp_path.iterdir()}
    assert entries == {MANIFEST_FILENAME}


def test_every_written_manifest_validates_against_schema(tmp_path):
    manifest = _manifest(
        groups=[
            _group(
                group_id="negative-01",
                members=["_DSC4638.NEF"],
                expected_outputs=["_DSC4638.tif"],
                status="completed",
                outputs=[OutputRecord(name="_DSC4638.tif", size=123, sha256=GOOD_SHA)],
            )
        ]
    )
    write_manifest(tmp_path, manifest)

    data = json.loads((tmp_path / MANIFEST_FILENAME).read_text())
    assert_matches_manifest_schema(data, SCHEMA)


# --- structural (BAD_MANIFEST) validation ---------------------------------


def test_load_manifest_missing_file_is_bad_manifest(tmp_path):
    with pytest.raises(BadManifestError):
        load_manifest(tmp_path)


def test_load_manifest_invalid_json_is_bad_manifest(tmp_path):
    (tmp_path / MANIFEST_FILENAME).write_text("{not json")
    with pytest.raises(BadManifestError):
        load_manifest(tmp_path)


def test_validate_manifest_dict_rejects_missing_required_field():
    data = json.loads(json.dumps(_manifest().to_dict()))
    del data["run_id"]
    with pytest.raises(BadManifestError):
        validate_manifest_dict(data)


def test_validate_manifest_dict_rejects_invalid_status():
    data = _manifest().to_dict()
    data["status"] = "not-a-status"
    with pytest.raises(BadManifestError):
        validate_manifest_dict(data)


def test_validate_manifest_dict_rejects_invalid_group_status():
    data = _manifest().to_dict()
    data["groups"][0]["status"] = "not-a-status"
    with pytest.raises(BadManifestError):
        validate_manifest_dict(data)


def test_validate_manifest_dict_rejects_bad_sha256():
    data = _manifest().to_dict()
    data["sources"][0]["sha256"] = "not-hex"
    with pytest.raises(BadManifestError):
        validate_manifest_dict(data)


def test_validate_manifest_dict_accepts_a_well_formed_manifest():
    validate_manifest_dict(_manifest().to_dict())  # must not raise


# --- output-path escape (resolve_within) ----------------------------------


def test_resolve_within_rejects_absolute_name(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        resolve_within(tmp_path, "/etc/passwd")


def test_resolve_within_rejects_dot_dot(tmp_path):
    with pytest.raises(ValueError, match=r"\.\."):
        resolve_within(tmp_path, "../escape.tif")


def test_resolve_within_rejects_symlink_escape(tmp_path):
    outside = tmp_path.parent / "outside-target.tif"
    outside.write_bytes(b"x")
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    (output_dir / "escape.tif").symlink_to(outside)

    with pytest.raises(ValueError, match="escapes"):
        resolve_within(output_dir, "escape.tif")


def test_resolve_within_accepts_a_plain_relative_name(tmp_path):
    resolved = resolve_within(tmp_path, "_DSC4638.tif")
    assert resolved == (tmp_path / "_DSC4638.tif").resolve()


def test_load_manifest_rejects_manifest_naming_an_escaping_output(tmp_path):
    manifest = _manifest(groups=[_group(expected_outputs=["../escape.tif"])])
    write_manifest(tmp_path, manifest)

    with pytest.raises(BadManifestError):
        load_manifest(tmp_path)


# --- rerun-mismatch comparison ---------------------------------------------


def test_check_rerun_matches_accepts_an_identical_candidate():
    existing = _manifest(run_id="old-run")
    candidate = _manifest(run_id="new-run", started_at="2026-08-29T12:00:00+00:00")
    check_rerun_matches(existing, candidate)  # must not raise


@pytest.mark.parametrize(
    ("field", "override"),
    [
        ("source_order", {"source_order": ["_DSC4639.NEF"], "sources": [_source("_DSC4639.NEF")]}),
        ("sources", {"sources": [SourceRecord("_DSC4638.NEF", "/input/x", 1, 1.0, "b" * 64)]}),
        ("shots_per_negative", {"shots_per_negative": 4}),
        ("film_date", {"film_date": "2026-08-03"}),
        ("processing_params", {"processing_params": {"output_bps": 8}}),
        ("icc_profile", {"icc_profile": {"name": "ProPhoto-v4.icc", "sha256": "b" * 64}}),
    ],
)
def test_check_rerun_matches_rejects_each_compared_field(field, override):
    existing = _manifest()
    candidate = _manifest(**override)

    with pytest.raises(ManifestMismatchError):
        check_rerun_matches(existing, candidate)


def test_check_rerun_matches_rejects_different_grouping():
    existing = _manifest(groups=[_group(group_id="negative-01", members=["_DSC4638.NEF"])])
    candidate = _manifest(
        groups=[_group(group_id="negative-01", members=["_DSC4638.NEF"], expected_outputs=["x.tif"])]
    )
    candidate.groups[0].members = ["_DSC4639.NEF"]

    with pytest.raises(ManifestMismatchError):
        check_rerun_matches(existing, candidate)


def test_check_rerun_matches_ignores_run_id_status_and_timing():
    existing = _manifest(run_id="old", status="complete", started_at="t1", finished_at="t2")
    candidate = _manifest(run_id="new", status="running", started_at="t3", finished_at=None)
    check_rerun_matches(existing, candidate)  # must not raise


# --- manifest size estimate -------------------------------------------------


def test_estimate_manifest_size_is_positive_and_reasonable():
    size = estimate_manifest_size(_manifest())
    assert 0 < size < 10_000
