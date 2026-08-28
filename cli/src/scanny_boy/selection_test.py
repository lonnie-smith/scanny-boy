import pytest

from scanny_boy.selection import (
    SelectionUsageError,
    group,
    is_contiguous,
    nearest_valid_counts,
    order_selection,
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
