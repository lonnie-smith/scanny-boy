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
    check_rerun_compatible,
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
from scanny_boy.selection import GridSpec

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


# --- check_rerun_compatible (probe --out's preview, before a film date is
# known) ---------------------------------------------------------------


def _known_fields(manifest: Manifest) -> dict:
    return {
        "source_order": manifest.source_order,
        "source_hashes": {s.filename: s.sha256 for s in manifest.sources},
        "shots_per_negative": manifest.shots_per_negative,
        "groups": [(g.group_id, g.members) for g in manifest.groups],
        "icc_sha256": manifest.icc_profile.get("sha256"),
    }


def test_check_rerun_compatible_accepts_an_identical_candidate():
    existing = _manifest(run_id="old-run")
    check_rerun_compatible(existing, **_known_fields(_manifest(run_id="new-run")))  # no raise


@pytest.mark.parametrize(
    ("field", "override"),
    [
        ("source_order", {"source_order": ["_DSC4639.NEF"], "sources": [_source("_DSC4639.NEF")]}),
        ("sources", {"sources": [SourceRecord("_DSC4638.NEF", "/input/x", 1, 1.0, "b" * 64)]}),
        ("shots_per_negative", {"shots_per_negative": 4}),
        ("icc_profile", {"icc_profile": {"name": "ProPhoto-v4.icc", "sha256": "b" * 64}}),
    ],
)
def test_check_rerun_compatible_rejects_each_known_field(field, override):
    existing = _manifest()
    candidate = _manifest(**override)

    with pytest.raises(ManifestMismatchError):
        check_rerun_compatible(existing, **_known_fields(candidate))


def test_check_rerun_compatible_rejects_different_grouping():
    existing = _manifest(groups=[_group(group_id="negative-01", members=["_DSC4638.NEF"])])
    candidate = _manifest(
        groups=[_group(group_id="negative-01", members=["_DSC4639.NEF"], expected_outputs=["x.tif"])]
    )

    with pytest.raises(ManifestMismatchError):
        check_rerun_compatible(existing, **_known_fields(candidate))


def test_check_rerun_compatible_ignores_a_different_film_date_or_processing_params():
    """The whole reason this check exists separately from
    `check_rerun_matches`: at probe time neither field is known yet
    (section 4.1), so a preview must not treat a difference in either as a
    mismatch — even though `check_rerun_matches` (used by `convert`, once
    the film date is known) still does."""
    existing = _manifest(film_date="2026-08-02", processing_params={"output_bps": 16})
    candidate = _manifest(film_date="2026-08-09", processing_params={"output_bps": 8})

    check_rerun_compatible(existing, **_known_fields(candidate))  # must not raise
    with pytest.raises(ManifestMismatchError):
        check_rerun_matches(existing, candidate)


# --- manifest size estimate -------------------------------------------------


def test_estimate_manifest_size_is_positive_and_reasonable():
    size = estimate_manifest_size(_manifest())
    assert 0 < size < 10_000


# --- grid (docs/GRID_STITCH_PLAN.md section 2.3) ---------------------------


def test_manifest_grid_defaults_to_none_and_grid_spec_falls_back_to_strip():
    manifest = _manifest()
    assert manifest.grid is None
    assert manifest.grid_spec == GridSpec(across=3, down=1)


def test_manifest_grid_round_trips(tmp_path):
    manifest = _manifest(
        shots_per_negative=6, grid={"across": 3, "down": 2}
    )
    write_manifest(tmp_path, manifest)
    loaded = load_manifest(tmp_path)
    assert loaded.grid == {"across": 3, "down": 2}
    assert loaded.grid_spec == GridSpec(across=3, down=2)
    assert loaded.shots_per_negative == 6


def test_validate_manifest_dict_rejects_grid_product_mismatch():
    data = _manifest(grid={"across": 3, "down": 2}).to_dict()
    with pytest.raises(BadManifestError, match="across \\* down"):
        validate_manifest_dict(data)


def test_validate_manifest_dict_rejects_grid_without_both_dimensions():
    data = _manifest().to_dict()
    data["grid"] = {"across": 3}
    with pytest.raises(BadManifestError):
        validate_manifest_dict(data)


def test_check_rerun_matches_rejects_a_changed_grid():
    """A 3x2 and a 6x1 batch are not the same batch even though both are
    six scans: the resume comparison compares the grid, not just the
    count."""
    existing = _manifest(grid={"across": 3, "down": 2}, shots_per_negative=6)
    candidate = _manifest(grid={"across": 6, "down": 1}, shots_per_negative=6)
    with pytest.raises(ManifestMismatchError, match="grid"):
        check_rerun_matches(existing, candidate)


def test_check_rerun_matches_accepts_the_same_grid():
    existing = _manifest(grid={"across": 3, "down": 2}, shots_per_negative=6)
    candidate = _manifest(
        grid={"across": 3, "down": 2},
        shots_per_negative=6,
        run_id="new",
        started_at="t",
    )
    check_rerun_matches(existing, candidate)  # must not raise


def test_check_rerun_compatible_rejects_a_changed_grid():
    existing = _manifest(grid={"across": 3, "down": 2}, shots_per_negative=6)
    candidate = _manifest(grid={"across": 6, "down": 1}, shots_per_negative=6)
    with pytest.raises(ManifestMismatchError, match="grid"):
        check_rerun_compatible(existing, **_known_fields(candidate))
# --- MONOCHROME_PLAN section 5.1: the forward shim at the rerun compare ----


def test_rerun_matches_a_v1_normalize_block_against_a_v2_build():
    """§5.1: a work manifest whose stored `normalize` block predates the
    mono feature (format_version 1) must not read as "processing settings
    differ" against a v2 build's candidate."""
    from scanny_boy.normalization import build_params

    existing = _manifest(
        processing_params={
            "output_bps": 16,
            "normalize": {**build_params(), "format_version": 1},
        }
    )
    candidate = _manifest(
        processing_params={"output_bps": 16, "normalize": build_params()},
        status="complete",
    )

    check_rerun_matches(existing, candidate)


def test_rerun_still_rejects_a_genuinely_different_normalize_block():
    from scanny_boy.normalization import build_params

    existing = _manifest(
        processing_params={
            "output_bps": 16,
            "normalize": {**build_params(), "format_version": 1, "analysis_grid": 512},
        }
    )
    candidate = _manifest(
        processing_params={"output_bps": 16, "normalize": build_params()},
        status="complete",
    )

    with pytest.raises(ManifestMismatchError):
        check_rerun_matches(existing, candidate)
