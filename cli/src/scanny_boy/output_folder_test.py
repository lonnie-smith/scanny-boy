import pytest

from scanny_boy.manifest import (
    BadManifestError,
    CuratedMetadata,
    GroupRecord,
    Manifest,
    ManifestMismatchError,
    OutputRecord,
    SourceRecord,
    write_manifest,
)
from scanny_boy.output_folder import (
    ROLL_RULES,
    OutputFolderError,
    apply_recovery_cleanup,
    list_non_dot_entries,
    plan_rerun,
    plan_rerun_preview,
    staging_dir_path,
    validate_not_same_as_input,
    validate_writable,
)
from scanny_boy.roll_manifest import RollInvariants, load_roll_manifest

# The roll tests below need a folder a genuine `stitch` produced (section 4);
# the stitch fixtures are the one place that machinery lives.
from scanny_boy.stitch_pipeline_test import _make_work_dir, _roll_dir, _stitch

GOOD_SHA = "a" * 64


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


def _manifest(run_id: str = "run-1", **overrides) -> Manifest:
    defaults = {
        "scanny_boy_version": "0.1.0",
        "run_id": run_id,
        "status": "complete",
        "input_folder": "/input",
        "film_date": "2026-08-02",
        "shots_per_negative": 3,
        "processing_params": {"output_bps": 16},
        "icc_profile": {"name": "ProPhoto-v4.icc", "sha256": GOOD_SHA},
        "source_order": ["a.NEF", "b.NEF", "c.NEF"],
        "sources": [
            SourceRecord(n, f"/input/{n}", 100, 1.0, GOOD_SHA) for n in ["a.NEF", "b.NEF", "c.NEF"]
        ],
        "curated_metadata": _curated(),
        "groups": [
            GroupRecord(
                group_id="negative-01",
                members=["a.NEF", "b.NEF", "c.NEF"],
                expected_outputs=["a.tif", "b.tif", "c.tif"],
                status="completed",
                outputs=[
                    OutputRecord(name=f"{n}.tif", size=1, sha256=GOOD_SHA) for n in ["a", "b", "c"]
                ],
            )
        ],
        "started_at": "2026-08-28T12:00:00+00:00",
        "finished_at": "2026-08-28T12:05:00+00:00",
    }
    defaults.update(overrides)
    return Manifest(**defaults)


# --- basic folder checks ----------------------------------------------------


def test_list_non_dot_entries_ignores_dot_files(tmp_path):
    (tmp_path / "a.tif").touch()
    (tmp_path / ".DS_Store").touch()
    (tmp_path / "._a.tif").touch()

    entries = {p.name for p in list_non_dot_entries(tmp_path)}
    assert entries == {"a.tif"}


def test_validate_not_same_as_input_rejects_identical_resolved_paths(tmp_path):
    with pytest.raises(OutputFolderError) as excinfo:
        validate_not_same_as_input(tmp_path, tmp_path)
    assert excinfo.value.code.value == "OUTPUT_SAME_AS_INPUT"


def test_validate_not_same_as_input_accepts_different_folders(tmp_path):
    other = tmp_path / "out"
    other.mkdir()
    validate_not_same_as_input(tmp_path, other)  # must not raise


def test_validate_writable_rejects_a_missing_directory(tmp_path):
    with pytest.raises(OutputFolderError) as excinfo:
        validate_writable(tmp_path / "does-not-exist")
    assert excinfo.value.code.value == "OUTPUT_NOT_WRITABLE"


# --- plan_rerun --------------------------------------------------------------


def test_plan_rerun_empty_folder_has_no_prior_manifest(tmp_path):
    plan = plan_rerun(tmp_path, _manifest())
    assert plan.existing_manifest is None
    assert plan.conflicting_outputs == []
    assert plan.stale_outputs == []
    assert plan.stale_staging_dirs == []


def test_plan_rerun_nonempty_folder_without_manifest_is_output_not_empty(tmp_path):
    (tmp_path / "some-other-file.tif").touch()

    with pytest.raises(OutputFolderError) as excinfo:
        plan_rerun(tmp_path, _manifest())
    assert excinfo.value.code.value == "OUTPUT_NOT_EMPTY"


def test_plan_rerun_accepts_a_folder_with_only_dot_files_alongside_outputs(tmp_path):
    existing = _manifest()
    write_manifest(tmp_path, existing)
    (tmp_path / "a.tif").touch()
    (tmp_path / "b.tif").touch()
    (tmp_path / "c.tif").touch()
    (tmp_path / ".DS_Store").touch()
    (tmp_path / "._a.tif").touch()
    (tmp_path / ".Spotlight-V100").mkdir()

    plan = plan_rerun(tmp_path, _manifest())  # candidate matches existing

    assert plan.existing_manifest is not None
    assert plan.conflicting_outputs == ["a.tif", "b.tif", "c.tif"]


def test_plan_rerun_rejects_unrelated_content_alongside_a_valid_manifest(tmp_path):
    write_manifest(tmp_path, _manifest())
    (tmp_path / "unrelated.jpg").touch()

    with pytest.raises(OutputFolderError) as excinfo:
        plan_rerun(tmp_path, _manifest())
    assert excinfo.value.code.value == "OUTPUT_NOT_EMPTY"


def test_plan_rerun_bad_manifest_propagates(tmp_path):
    (tmp_path / "scanny-boy-manifest.json").write_text("not json")
    (tmp_path / "a.tif").touch()

    with pytest.raises(BadManifestError):
        plan_rerun(tmp_path, _manifest())


def test_plan_rerun_mismatch_propagates(tmp_path):
    write_manifest(tmp_path, _manifest())

    different = _manifest(film_date="2026-09-01")
    with pytest.raises(ManifestMismatchError):
        plan_rerun(tmp_path, different)


def test_plan_rerun_reports_conflicts_for_completed_groups_whose_outputs_exist(tmp_path):
    existing = _manifest()
    write_manifest(tmp_path, existing)
    (tmp_path / "a.tif").touch()
    (tmp_path / "b.tif").touch()
    (tmp_path / "c.tif").touch()

    plan = plan_rerun(tmp_path, _manifest())

    assert sorted(plan.conflicting_outputs) == ["a.tif", "b.tif", "c.tif"]
    assert plan.stale_outputs == []


def test_plan_rerun_treats_non_completed_groups_outputs_as_stale_not_conflicting(tmp_path):
    existing = _manifest(
        groups=[
            GroupRecord(
                group_id="negative-01",
                members=["a.NEF", "b.NEF", "c.NEF"],
                expected_outputs=["a.tif", "b.tif", "c.tif"],
                status="pending",
            )
        ]
    )
    write_manifest(tmp_path, existing)
    (tmp_path / "a.tif").touch()  # a stray leftover from an interrupted run
    staging = staging_dir_path(tmp_path, existing.run_id, "negative-01")
    staging.mkdir()

    plan = plan_rerun(tmp_path, _manifest())

    assert plan.conflicting_outputs == []
    assert plan.stale_outputs == ["a.tif"]
    assert plan.stale_staging_dirs == [staging]


# --- rolls (Phase 3 section 3.4: additive semantics) ----------------------


def _candidate_from(manifest) -> RollInvariants:
    """The invariants of a rerun with the same parameters the roll already
    established, read back from the roll itself rather than restated."""
    return RollInvariants(
        shots_per_negative=manifest.shots_per_negative,
        processing_params=manifest.processing_params,
        icc_profile_sha256=manifest.icc_profile.get("sha256", ""),
        stitch_params=manifest.stitch_params,
    )


def test_roll_folder_with_prior_outputs_is_valid(tmp_path):
    """Section 3.4: a nonempty roll folder holding published outputs from
    earlier runs is normal, not `OUTPUT_NOT_EMPTY`, and under `ROLL_RULES`
    those outputs are neither conflicts nor stale. The roll is built by a
    genuine `stitch` through P3-2's writer (section 4) — a hand-authored
    manifest proves nothing about what the folder really holds. The dot-dir
    skip covers `.work`, which `run --roll` will use for scratch."""
    work_dir = _make_work_dir(tmp_path, negatives=1)
    out_dir = _roll_dir(tmp_path)
    _stitch(work_dir, out_dir)

    manifest = load_roll_manifest(out_dir)
    [negative] = manifest.negatives
    assert negative.status == "completed"
    published = out_dir / negative.expected_output
    assert published.exists()

    # The scratch and metadata dot-directories a run may leave behind.
    (out_dir / ".work").mkdir()
    (out_dir / ".work" / "scratch").write_text("scratch")
    (out_dir / ".DS_Store").write_bytes(b"")

    plan = plan_rerun(out_dir, _candidate_from(manifest), rules=ROLL_RULES)

    assert plan.conflicting_outputs == []
    assert plan.stale_outputs == []
    assert plan.stale_staging_dirs == []
    assert plan.existing_manifest is not None
    assert [n.negative_id for n in plan.existing_manifest.negatives] == [
        negative.negative_id
    ]


# --- plan_rerun_preview (probe --out, before a film date is known) --------


def _known_fields(manifest: Manifest) -> dict:
    return {
        "source_order": manifest.source_order,
        "source_hashes": {s.filename: s.sha256 for s in manifest.sources},
        "shots_per_negative": manifest.shots_per_negative,
        "groups": [(g.group_id, g.members) for g in manifest.groups],
        "icc_sha256": manifest.icc_profile.get("sha256"),
    }


def test_plan_rerun_preview_empty_folder_has_no_prior_manifest(tmp_path):
    plan = plan_rerun_preview(tmp_path, **_known_fields(_manifest()))
    assert plan.existing_manifest is None
    assert plan.conflicting_outputs == []


def test_plan_rerun_preview_nonempty_folder_without_manifest_is_output_not_empty(tmp_path):
    (tmp_path / "some-other-file.tif").touch()

    with pytest.raises(OutputFolderError) as excinfo:
        plan_rerun_preview(tmp_path, **_known_fields(_manifest()))
    assert excinfo.value.code.value == "OUTPUT_NOT_EMPTY"


def test_plan_rerun_preview_reports_conflicts_for_completed_groups_whose_outputs_exist(tmp_path):
    existing = _manifest()
    write_manifest(tmp_path, existing)
    (tmp_path / "a.tif").touch()
    (tmp_path / "b.tif").touch()
    (tmp_path / "c.tif").touch()

    plan = plan_rerun_preview(tmp_path, **_known_fields(_manifest()))

    assert sorted(plan.conflicting_outputs) == ["a.tif", "b.tif", "c.tif"]


def test_plan_rerun_preview_mismatch_on_source_order_propagates(tmp_path):
    write_manifest(tmp_path, _manifest())

    different = _manifest(source_order=["a.NEF", "b.NEF"])
    with pytest.raises(ManifestMismatchError):
        plan_rerun_preview(tmp_path, **_known_fields(different))


def test_plan_rerun_preview_accepts_a_different_film_date(tmp_path):
    """The reason this function exists separately from `plan_rerun`: at
    probe time the film date is not known yet (section 4.1's preview runs
    before `convert`), so a difference there must not be treated as a
    mismatch — only `convert`'s full `plan_rerun` call does that, once the
    film date is entered."""
    write_manifest(tmp_path, _manifest(film_date="2026-08-02"))
    (tmp_path / "a.tif").touch()

    different_date = _manifest(film_date="2026-09-01")
    plan = plan_rerun_preview(tmp_path, **_known_fields(different_date))  # must not raise

    assert plan.conflicting_outputs == ["a.tif"]
    with pytest.raises(ManifestMismatchError):
        plan_rerun(tmp_path, different_date)


def test_apply_recovery_cleanup_deletes_stale_outputs_and_staging_dirs(tmp_path):
    existing = _manifest(
        groups=[
            GroupRecord(
                group_id="negative-01",
                members=["a.NEF", "b.NEF", "c.NEF"],
                expected_outputs=["a.tif", "b.tif", "c.tif"],
                status="pending",
            )
        ]
    )
    write_manifest(tmp_path, existing)
    (tmp_path / "a.tif").touch()
    staging = staging_dir_path(tmp_path, existing.run_id, "negative-01")
    staging.mkdir()
    (staging / "leftover.tif").touch()

    plan = plan_rerun(tmp_path, _manifest())
    apply_recovery_cleanup(tmp_path, plan)

    assert not (tmp_path / "a.tif").exists()
    assert not staging.exists()
