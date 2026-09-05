import pytest

from scanny_boy.selection import (
    GridSpec,
    InvalidGridError,
    SelectionUsageError,
    group,
    is_contiguous,
    nearest_valid_counts,
    order_selection,
    validate_grid,
)

CATALOGUE = [f"DSC_{i:04d}.NEF" for i in range(1, 13)]  # DSC_0001..DSC_0012


def test_order_selection_reorders_into_canonical_order():
    selection = order_selection(CATALOGUE, ["DSC_0003.NEF", "DSC_0001.NEF", "DSC_0002.NEF"])
    assert selection.names == ["DSC_0001.NEF", "DSC_0002.NEF", "DSC_0003.NEF"]
    assert selection.start_index == 0
    assert selection.end_index == 2


def test_order_selection_rejects_duplicate_filename():
    with pytest.raises(SelectionUsageError):
        order_selection(CATALOGUE, ["DSC_0001.NEF", "DSC_0001.NEF"])


def test_order_selection_rejects_unknown_filename():
    with pytest.raises(SelectionUsageError):
        order_selection(CATALOGUE, ["DSC_9999.NEF"])


def test_contiguous_selection_is_accepted():
    selection = order_selection(CATALOGUE, ["DSC_0004.NEF", "DSC_0005.NEF", "DSC_0006.NEF"])
    assert is_contiguous(selection)


def test_separated_selection_is_rejected():
    selection = order_selection(CATALOGUE, ["DSC_0001.NEF", "DSC_0002.NEF", "DSC_0004.NEF"])
    assert not is_contiguous(selection)


@pytest.mark.parametrize("per_negative", range(1, 13))
def test_group_splits_selection_into_chunks_of_per_negative(per_negative):
    names = [f"DSC_{i:04d}.NEF" for i in range(per_negative * 3)]
    groups = group(names, per_negative)
    assert len(groups) == 3
    assert all(len(g) == per_negative for g in groups)
    assert [name for g in groups for name in g] == names


def test_group_default_three():
    groups = group(CATALOGUE[:6], 3)
    assert groups == [CATALOGUE[0:3], CATALOGUE[3:6]]


@pytest.mark.parametrize(
    ("count", "per_negative", "expected"),
    [
        (7, 3, (6, 9)),
        (1, 3, (0, 3)),
        (10, 4, (8, 12)),
    ],
)
def test_nearest_valid_counts(count, per_negative, expected):
    assert nearest_valid_counts(count, per_negative) == expected


# --- GridSpec (docs/GRID_STITCH_PLAN.md section 2.1) -----------------------


def test_gridspec_count_and_is_strip():
    assert GridSpec(across=5, down=2).count == 10
    assert GridSpec(across=6, down=1).count == 6
    assert GridSpec(across=1, down=6).count == 6
    assert GridSpec(across=3, down=2).is_strip is False
    assert GridSpec(across=6, down=1).is_strip is True
    assert GridSpec(across=1, down=6).is_strip is True


def test_gridspec_round_trips_through_a_dict():
    spec = GridSpec(across=3, down=2)
    assert GridSpec.from_dict(spec.to_dict()) == spec


@pytest.mark.parametrize(
    ("across", "down"),
    [(1, 1), (12, 1), (1, 12), (5, 2), (2, 5), (2, 2), (3, 1), (2, 6)],
)
def test_validate_grid_accepts_legal_shapes(across, down):
    validate_grid(GridSpec(across=across, down=down))  # must not raise


@pytest.mark.parametrize(
    ("across", "down"), [(3, 3), (4, 3), (3, 5), (0, 1), (1, 0), (-1, 2), (13, 1)]
)
def test_validate_grid_rejects_illegal_shapes(across, down):
    with pytest.raises(InvalidGridError):
        validate_grid(GridSpec(across=across, down=down))


def test_validate_grid_names_the_rebate_rule_for_3x3():
    with pytest.raises(InvalidGridError, match="rebate"):
        validate_grid(GridSpec(across=3, down=3))


def test_validate_grid_names_the_count_cap():
    with pytest.raises(InvalidGridError, match="maximum of 12"):
        validate_grid(GridSpec(across=13, down=1))


def test_validate_grid_error_is_not_a_selection_usage_error():
    """`InvalidGridError` is a distinct type: the handlers that catch
    `SelectionUsageError` map it to `NO_FILES`, which would be actively
    misleading for a bad grid shape (docs/GRID_STITCH_PLAN.md section 2.1)."""
    assert not issubclass(InvalidGridError, SelectionUsageError)
