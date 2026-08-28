import os

import pytest

from scanny_boy.catalogue import (
    CatalogueError,
    compute_canonical_order,
    discover_catalogue,
    natural_sort_key,
)
from scanny_boy.fake_nef_support import write_fake_nef


def test_natural_sort_key_orders_dsc9_before_dsc10():
    names = ["DSC_10.NEF", "DSC_9.NEF", "DSC_2.NEF"]
    assert sorted(names, key=natural_sort_key) == ["DSC_2.NEF", "DSC_9.NEF", "DSC_10.NEF"]


def test_discover_catalogue_is_case_insensitive_and_non_recursive(tmp_path):
    write_fake_nef(tmp_path / "a.NEF")
    write_fake_nef(tmp_path / "b.nef")
    write_fake_nef(tmp_path / "c.Nef")
    (tmp_path / "not-a-raw.txt").write_text("ignore me")
    sub = tmp_path / "subdir"
    sub.mkdir()
    write_fake_nef(sub / "d.NEF")

    names = discover_catalogue(tmp_path)

    assert names == ["a.NEF", "b.nef", "c.Nef"]


def test_discover_catalogue_rejects_duplicate_real_file_via_symlink(tmp_path):
    write_fake_nef(tmp_path / "a.NEF")
    os.symlink(tmp_path / "a.NEF", tmp_path / "a-link.NEF")

    with pytest.raises(CatalogueError):
        discover_catalogue(tmp_path)


def test_discover_catalogue_rejects_entry_outside_input_folder(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    write_fake_nef(outside / "escaped.NEF")
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    os.symlink(outside / "escaped.NEF", input_dir / "escaped.NEF")

    with pytest.raises(CatalogueError):
        discover_catalogue(input_dir)


def test_compute_canonical_order_sorts_by_timestamp(tmp_path):
    write_fake_nef(tmp_path / "b.NEF", date_time_original="2026:08:02 12:00:10")
    write_fake_nef(tmp_path / "a.NEF", date_time_original="2026:08:02 12:00:05")

    order = compute_canonical_order(tmp_path, ["a.NEF", "b.NEF"])

    assert order.order == ["a.NEF", "b.NEF"]
    assert not order.used_filename_fallback


def test_compute_canonical_order_breaks_ties_with_natural_filename_order(tmp_path):
    same_time = "2026:08:02 12:00:00"
    write_fake_nef(tmp_path / "DSC_10.NEF", date_time_original=same_time, subsec_time_original="00")
    write_fake_nef(tmp_path / "DSC_9.NEF", date_time_original=same_time, subsec_time_original="00")

    order = compute_canonical_order(tmp_path, ["DSC_10.NEF", "DSC_9.NEF"])

    assert order.order == ["DSC_9.NEF", "DSC_10.NEF"]


def test_compute_canonical_order_handles_year_rollover(tmp_path):
    write_fake_nef(tmp_path / "late.NEF", date_time_original="9999:12:31 23:59:59")
    write_fake_nef(tmp_path / "early.NEF", date_time_original="0001:01:01 00:00:00")

    order = compute_canonical_order(tmp_path, ["late.NEF", "early.NEF"])

    assert order.order == ["early.NEF", "late.NEF"]
    assert not order.used_filename_fallback


def test_missing_timestamp_anywhere_falls_back_to_whole_catalogue_filename_order(tmp_path):
    write_fake_nef(tmp_path / "DSC_1.NEF", date_time_original="2026:08:02 12:00:00")
    write_fake_nef(tmp_path / "DSC_2.NEF", date_time_original="2026:08:02 12:00:05")
    # The file with no usable timestamp is outside any particular selection;
    # compute_canonical_order always considers the whole catalogue.
    write_fake_nef(tmp_path / "DSC_10.NEF", date_time_original=None)

    order = compute_canonical_order(tmp_path, ["DSC_1.NEF", "DSC_2.NEF", "DSC_10.NEF"])

    assert order.used_filename_fallback
    assert order.order == ["DSC_1.NEF", "DSC_2.NEF", "DSC_10.NEF"]  # natural filename order
