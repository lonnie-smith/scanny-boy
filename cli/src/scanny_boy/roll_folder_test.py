import os

import pytest

from scanny_boy.roll_folder import (
    FALLBACK_SLUG,
    MAX_SUFFIX_ATTEMPTS,
    SLUG_MAX_LENGTH,
    RollFolderError,
    create_roll,
    delete_roll,
    rename_roll,
    scan_library,
    slugify,
    unique_folder_name,
)
from scanny_boy.roll_manifest import (
    load_roll_manifest,
    write_roll_manifest,
)
from scanny_boy.roll_manifest_test import _negative

# --- slugify --------------------------------------------------------------


def test_slugify_normalizes_unicode():
    decomposed = "Café"  # "Café" as e + combining acute accent
    assert slugify(decomposed) == slugify("Café")


def test_slugify_replaces_punctuation_and_whitespace_runs():
    assert slugify("Tri-X, Portland 1998") == "Tri-X-Portland-1998"


def test_slugify_collapses_whitespace_runs():
    assert slugify("a   b") == "a-b"


def test_slugify_strips_leading_and_trailing_dashes_and_dots():
    assert slugify("...--Roll Name--...") == "Roll-Name"


def test_slugify_empty_becomes_fallback():
    assert slugify("") == FALLBACK_SLUG
    assert slugify("!!!") == FALLBACK_SLUG
    assert slugify("   ") == FALLBACK_SLUG


def test_slugify_truncates_to_max_length():
    slug = slugify("a" * 100)
    assert slug == "a" * SLUG_MAX_LENGTH
    assert len(slug) == SLUG_MAX_LENGTH


# --- unique_folder_name -----------------------------------------------------


def test_unique_folder_name_returns_slug_when_free(tmp_path):
    assert unique_folder_name(tmp_path, "roll-a") == "roll-a"


def test_unique_folder_name_appends_suffix_on_single_collision(tmp_path):
    (tmp_path / "roll-a").mkdir()

    assert unique_folder_name(tmp_path, "roll-a") == "roll-a-2"


def test_unique_folder_name_appends_suffix_through_many_collisions(tmp_path):
    (tmp_path / "roll-a").mkdir()
    for suffix in range(2, 6):
        (tmp_path / f"roll-a-{suffix}").mkdir()

    assert unique_folder_name(tmp_path, "roll-a") == "roll-a-6"


def test_unique_folder_name_comparison_is_case_insensitive(tmp_path):
    (tmp_path / "Roll-A").mkdir()

    assert unique_folder_name(tmp_path, "roll-a") == "roll-a-2"


def test_unique_folder_name_raises_roll_exists_after_exhaustion(tmp_path):
    (tmp_path / "roll-a").mkdir()
    for suffix in range(2, MAX_SUFFIX_ATTEMPTS + 2):
        (tmp_path / f"roll-a-{suffix}").mkdir()

    with pytest.raises(RollFolderError) as exc_info:
        unique_folder_name(tmp_path, "roll-a")
    assert exc_info.value.code == "ROLL_EXISTS"


# --- create_roll -------------------------------------------------------------


def test_create_roll_registers_an_empty_roll(tmp_path):
    roll_dir = create_roll(tmp_path, "Tri-X, Portland 1998", film_kind="colour")

    assert roll_dir == tmp_path / "Tri-X-Portland-1998"
    # The record lives in the library database; the folder holds only
    # stitched TIFFs and staging directories.
    assert sorted(p.name for p in roll_dir.iterdir()) == []

    manifest = load_roll_manifest(roll_dir)
    assert manifest.roll_name == "Tri-X, Portland 1998"
    assert manifest.film == {"kind": "colour"}
    assert manifest.runs == []
    assert manifest.sources == []
    assert manifest.negatives == []


def test_create_roll_without_film_kind(tmp_path):
    roll_dir = create_roll(tmp_path, "Fresh")
    manifest = load_roll_manifest(roll_dir)
    assert manifest.film is None


def test_create_roll_monochrome_seeds_grey_density_profile(tmp_path):
    from scanny_boy.icc_profile import ProfileKind, profile_record

    roll_dir = create_roll(tmp_path, "Tri-X", film_kind="monochrome")
    manifest = load_roll_manifest(roll_dir)
    assert manifest.film == {"kind": "monochrome"}
    assert manifest.published_icc_profile == profile_record(ProfileKind.DENSITY_GREY)


def test_set_film_kind_updates_manifest(tmp_path):
    from scanny_boy.icc_profile import ProfileKind, profile_record
    from scanny_boy.roll_folder import set_film_kind

    roll_dir = create_roll(tmp_path, "Fresh")
    set_film_kind(roll_dir, "monochrome")
    manifest = load_roll_manifest(roll_dir)
    assert manifest.film == {"kind": "monochrome"}
    assert manifest.published_icc_profile == profile_record(ProfileKind.DENSITY_GREY)


# --- rename_roll ---------------------------------------------------------


def test_rename_roll_moves_folder_and_updates_name(tmp_path):
    roll_dir = create_roll(tmp_path, "Old Name", film_kind="colour")

    new_dir = rename_roll(roll_dir, "New Name")

    assert new_dir == tmp_path / "New-Name"
    assert new_dir.exists()
    assert not roll_dir.exists()
    manifest = load_roll_manifest(new_dir)
    assert manifest.roll_name == "New Name"


def test_rename_roll_leaves_everything_alone_on_move_failure(tmp_path, monkeypatch):
    roll_dir = create_roll(tmp_path, "Old Name", film_kind="colour")
    original_manifest = load_roll_manifest(roll_dir)

    def _failing_rename(*_args, **_kwargs):
        raise OSError("simulated move failure")

    monkeypatch.setattr(os, "rename", _failing_rename)

    # Section 5.5: wrapped in RollFolderError(ROLL_RENAME_FAILED) rather
    # than left as a raw OSError, so `roll rename` has one exception type
    # to catch — the move-failure guarantee itself is unchanged.
    with pytest.raises(RollFolderError) as exc_info:
        rename_roll(roll_dir, "New Name")
    assert exc_info.value.code == "ROLL_RENAME_FAILED"

    assert roll_dir.exists()
    assert not (tmp_path / "New-Name").exists()
    manifest = load_roll_manifest(roll_dir)
    assert manifest.roll_name == original_manifest.roll_name


# --- scan_library ----------------------------------------------------------


def test_scan_library_ignores_directories_without_a_registered_roll(tmp_path):
    create_roll(tmp_path, "Roll A", film_kind="colour")
    (tmp_path / "not-a-roll").mkdir()
    (tmp_path / "some-file.txt").write_text("hello")

    listings = scan_library(tmp_path)

    assert [listing.path for listing in listings] == [tmp_path / "Roll-A"]


def test_scan_library_reports_ok_and_vanished_side_by_side(tmp_path):
    ok_dir = create_roll(tmp_path, "Roll A", film_kind="colour")
    vanished_dir = create_roll(tmp_path, "Vanished Roll", film_kind="colour")
    import shutil

    shutil.rmtree(vanished_dir)

    listings = scan_library(tmp_path)

    by_path = {listing.path: listing for listing in listings}
    assert by_path[ok_dir].status == "ok"
    assert by_path[ok_dir].roll_id is not None
    assert by_path[ok_dir].negative_count == 0
    assert by_path[vanished_dir].status == "unreadable"
    assert by_path[vanished_dir].reason is not None
    assert by_path[vanished_dir].reason[0] == "ROLL_NOT_FOUND"
    assert by_path[vanished_dir].roll_id is None
    assert by_path[vanished_dir].negative_count is None


def test_scan_library_negative_count_is_every_negative(tmp_path):
    roll_dir = create_roll(tmp_path, "Roll A", film_kind="colour")
    manifest = load_roll_manifest(roll_dir)
    manifest.negatives.append(_negative(negative_id="aaaaaa-negative-01"))
    manifest.negatives.append(
        _negative(
            negative_id="aaaaaa-negative-02",
            members=manifest.negatives[0].members,
            expected_output="_DSC4638-2.tif",
        )
    )
    write_roll_manifest(roll_dir, manifest)

    listings = scan_library(tmp_path)

    assert len(listings) == 1
    assert listings[0].negative_count == 2


def test_roll_list_emits_one_event_for_the_whole_library(tmp_path):
    create_roll(tmp_path, "Roll A", film_kind="colour")
    create_roll(tmp_path, "Roll B", film_kind="colour")

    listings = scan_library(tmp_path)

    assert {listing.roll_name for listing in listings} == {"Roll A", "Roll B"}
    assert all(listing.status == "ok" for listing in listings)


# --- delete_roll ----------------------------------------------------------


def _preview_file(roll_id: str, name: str):
    from scanny_boy.previews import previews_root

    path = previews_root() / roll_id / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"png")
    return path


def test_delete_roll_removes_the_rolls_whole_preview_directory(tmp_path):
    """The recorded previews go, and so does anything else in the roll's
    preview directory: a preview whose `preview_path` was lost has nothing
    left that could ever name it, so the directory itself is the unit."""
    roll_dir = create_roll(tmp_path, "Roll A", film_kind="colour")
    manifest = load_roll_manifest(roll_dir)
    recorded = _preview_file(manifest.roll_id, "aaaaaa-negative-01.png")
    manifest.negatives.append(
        _negative(negative_id="aaaaaa-negative-01", preview_path=str(recorded))
    )
    write_roll_manifest(roll_dir, manifest)
    # An orphan from some earlier failed unlink: on disk, named by nothing.
    unrecorded = _preview_file(manifest.roll_id, "aaaaaa-negative-99.png")

    warnings = []
    fields = delete_roll(roll_dir, emit=warnings.append)

    assert fields == {"roll_id": manifest.roll_id, "path": str(roll_dir)}
    assert warnings == []
    assert not recorded.exists()
    assert not unrecorded.exists()
    assert not recorded.parent.exists()


def test_delete_roll_leaves_other_rolls_previews_alone(tmp_path):
    keeper = create_roll(tmp_path, "Keeper", film_kind="colour")
    doomed = create_roll(tmp_path, "Doomed", film_kind="colour")
    keeper_preview = _preview_file(
        load_roll_manifest(keeper).roll_id, "bbbbbb-negative-01.png"
    )

    delete_roll(doomed, emit=lambda event: None)

    assert keeper_preview.exists()


def test_delete_roll_without_previews_reports_no_warning(tmp_path):
    """A roll that was never opened in the Edit tab has no preview
    directory at all, which is the ordinary case, not a failure."""
    roll_dir = create_roll(tmp_path, "Roll A", film_kind="colour")

    warnings = []
    delete_roll(roll_dir, emit=warnings.append)

    assert warnings == []
