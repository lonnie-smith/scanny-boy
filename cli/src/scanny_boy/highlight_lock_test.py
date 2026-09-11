"""Tests for docs/ROLL_HIGHLIGHT_LOCK.md: the roll-level highlight-colour
estimate (`compute_roll_highlight_lock`) and the render-time correction
(`corrected_floors`).

Pure unit tests over the module's own data shapes — a minimal stand-in
`RollManifest`-like object for `compute_roll_highlight_lock`, plain tuples
for `corrected_floors` — no work_dir, no image I/O: this module never
touches pixels.

Every fixture here uses the real sign convention (module §-1): log10
density, base (clear film) is the LEAST negative value on the roll, a
scene highlight is DENSER — more negative — than base. `H - B < 0` in
every channel on real film; `floors` (the dense end) sits BELOW `ceils`
(the thin end) numerically. A roll base of `(-0.50, -0.20, -0.90)` — the
same numbers `work_dir_support.base_frame_block` uses for real stitch
tests — and a `K` of `(0.9, 1.0, 1.1)` (REBATE_ANCHORING's own base-density
asymmetry) recur throughout as realistic stand-ins."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from scanny_boy.highlight_lock import (
    HighlightLock,
    base_offset_for,
    compute_roll_highlight_lock,
    corrected_floors,
)

_BASE = (-0.50, -0.20, -0.90)
_K = (0.9, 1.0, 1.1)


@dataclasses.dataclass
class _FakeNegative:
    normalization: dict | None


@dataclasses.dataclass
class _FakeRoll:
    film_base: dict | None
    negatives: list


def _negative(
    highlight_refs, *, exposure_matched: bool = True, level_offset: float | None = None
) -> _FakeNegative:
    """`exposure_matched=True` (the default) is round 3's "EXIF matched the
    base frame" case — `base_offset_for` then resolves to `0.0`, exactly
    round 2's assumed-matched behaviour, so every pre-round-3 test fixture
    still exercises the same math by default. Pass `level_offset` to
    exercise the "measured drift" source instead (it takes priority)."""
    record: dict = {"highlight_refs": highlight_refs, "exposure_matched": exposure_matched}
    if level_offset is not None:
        record["base_check"] = {"level_offset": level_offset}
    return _FakeNegative(normalization=record)


def _highlight_refs(base=_BASE, k=_K, amplitude=-1.8) -> tuple[float, float, float]:
    """A negative's own `highlight_refs`, built from a green-channel
    `amplitude` (negative, log10 D — §-1) and a per-channel ratio `k`:
    `H = B + amplitude * k`, so `(H - B) / amplitude == k` exactly by
    construction. `amplitude=-1.8` is a plausible ~6-stop highlight (roughly
    what REBATE_ANCHORING's own worked examples use)."""
    return tuple(float(b + amplitude * kc) for b, kc in zip(base, k, strict=True))


def _floors_ceils_for(
    highlight_refs, base=_BASE, mean_lf: float = -1.5, mean_lc: float = 0.05
):
    """The `floors`/`ceils` `analyze_bounds` would record for a negative
    whose dense-end reference is `highlight_refs` and whose roll base is
    `base` — the exact recombination `analyze_bounds` performs (median
    recentring, then the luma level added back), so the green-relative
    subtraction `corrected_floors` relies on cancels exactly, matching real
    stitch output."""
    h = np.asarray(highlight_refs, dtype=np.float64)
    b = np.asarray(base, dtype=np.float64)
    floors = tuple(mean_lf + (h - np.median(h)))
    ceils = tuple(mean_lc + (b - np.median(b)))
    return floors, ceils


# --- compute_roll_highlight_lock ---------------------------------------------


def test_no_film_base_gives_no_lock():
    roll = _FakeRoll(film_base=None, negatives=[_negative(list(_highlight_refs()))])
    assert compute_roll_highlight_lock(roll) is None


def test_no_qualifying_negative_gives_no_lock():
    """A colour roll where every negative's own neutral gate fell back
    (`highlight_refs` null) has nothing to build a ratio from — the
    identity case for a roll that has never had a trustworthy highlight
    measurement."""
    roll = _FakeRoll(
        film_base={"density": list(_BASE)},
        negatives=[_FakeNegative(normalization={"highlight_refs": None})],
    )
    assert compute_roll_highlight_lock(roll) is None


def test_mono_negatives_never_qualify():
    """`measure_highlight_refs` never records `highlight_refs` for a
    single-channel grid, so a mono roll's negatives read back as
    non-qualifying by construction — this is the mono no-op path."""
    roll = _FakeRoll(
        film_base={"density": list(_BASE)},
        negatives=[_negative([-2.1])],  # a 1-length list can never qualify
    )
    assert compute_roll_highlight_lock(roll) is None


def test_single_qualifying_negative_reproduces_its_own_ratio():
    refs = _highlight_refs(amplitude=-1.8)
    roll = _FakeRoll(film_base={"density": list(_BASE)}, negatives=[_negative(list(refs))])
    lock = compute_roll_highlight_lock(roll)
    assert lock is not None
    assert lock.qualifying_count == 1
    assert lock.base == _BASE
    np.testing.assert_allclose(lock.k, _K, atol=1e-9)


def test_positive_or_near_zero_amplitude_negative_is_skipped():
    """A negative whose green channel shows no measurable density above
    base — amplitude at or above `-_MIN_AMPLITUDE` — contributes no ratio.
    A *positive* amplitude (green landing above/thinner than base) is
    physically impossible for a real highlight and must be rejected the
    same way."""
    roll = _FakeRoll(
        film_base={"density": list(_BASE)},
        negatives=[
            _negative(list(_BASE)),  # amplitude exactly 0.0
            _negative([-2.4, -0.1, -3.0]),  # green ABOVE base: amplitude +0.1
            _negative(list(_highlight_refs(amplitude=-1.8))),
        ],
    )
    lock = compute_roll_highlight_lock(roll)
    assert lock is not None
    assert lock.qualifying_count == 1


def test_roll_estimate_updates_when_a_negative_is_added():
    """The deliverable's explicit case: the roll estimate is a pure
    function of the roll's current negative set, so appending a qualifying
    negative changes it (docs/ROLL_HIGHLIGHT_LOCK.md §4 — every run that
    publishes negatives, and every removal, must recompute)."""
    roll = _FakeRoll(
        film_base={"density": list(_BASE)},
        negatives=[_negative(list(_highlight_refs(amplitude=-1.8)))],
    )
    lock_before = compute_roll_highlight_lock(roll)
    assert lock_before is not None and lock_before.qualifying_count == 1

    # A second negative with a different ratio shifts the median.
    other_k = (1.3, 1.0, 0.95)
    roll.negatives.append(
        _negative(list(_highlight_refs(k=other_k, amplitude=-1.2)))
    )
    lock_after = compute_roll_highlight_lock(roll)
    assert lock_after is not None
    assert lock_after.qualifying_count == 2
    assert lock_after.k != lock_before.k


def test_median_recombination_is_robust_to_one_outlier():
    """Two negatives agree at `_K`; a third with a wild but
    amplitude-qualifying ratio must not move the roll median."""
    outlier_refs = _highlight_refs(k=(5.0, 1.0, -3.0), amplitude=-2.0)
    roll = _FakeRoll(
        film_base={"density": list(_BASE)},
        negatives=[
            _negative(list(_highlight_refs(amplitude=-1.8))),
            _negative(list(_highlight_refs(amplitude=-1.8))),
            _negative(list(outlier_refs)),
        ],
    )
    lock = compute_roll_highlight_lock(roll)
    assert lock is not None
    assert lock.qualifying_count == 3
    np.testing.assert_allclose(lock.k, _K, atol=1e-9)


# --- corrected_floors: identity and colour-replacement -----------------------


def test_identity_when_no_lock():
    floors, ceils = _floors_ceils_for(_highlight_refs())
    assert corrected_floors(floors, ceils, None) == floors


def test_identity_when_mono():
    lock = HighlightLock(k=_K, base=_BASE, qualifying_count=3)
    floors = (-1.5,)
    ceils = (-0.35,)
    assert corrected_floors(floors, ceils, lock) == floors


def test_identity_when_negative_matches_roll():
    """The load-bearing identity property: a qualifying negative whose own
    dense-end colour ratio already equals the roll's `K` is corrected to
    exactly itself — algebraically exact (module docstring §2), not merely
    a close numerical match."""
    lock = HighlightLock(k=_K, base=_BASE, qualifying_count=5)
    refs = _highlight_refs(amplitude=-1.8)
    floors, ceils = _floors_ceils_for(refs)

    result = corrected_floors(floors, ceils, lock, refs, base_offset=0.0)
    np.testing.assert_allclose(result, floors, atol=1e-9)


def test_green_floor_is_never_moved():
    """Whatever the correction does to R/B, green's own recorded floor is
    always left exactly as it was — the direct consequence of measuring
    every deviation relative to green (§0)."""
    lock = HighlightLock(k=_K, base=_BASE, qualifying_count=5)
    # A negative whose own ratio does NOT match K, so R/B genuinely move.
    refs = _highlight_refs(k=(1.4, 1.0, 0.6), amplitude=-1.5)
    floors, ceils = _floors_ceils_for(refs)
    result = corrected_floors(floors, ceils, lock, refs, base_offset=0.0)
    assert result[1] == floors[1]
    assert result[0] != floors[0] or result[2] != floors[2]


def test_cast_along_the_roll_colour_axis_is_fully_corrected():
    """The bug this fix closes: a scene cast that happens to sit along
    `K - 1` (the roll's own red-blue colour axis — the common case for a
    warm/cool cast on real colour film) must be corrected exactly as much
    as any other cast, because the amplitude comes from the negative's own
    *measured* green channel, never from fitting the negative's own R/B
    deviations against `K - 1`."""
    lock = HighlightLock(k=_K, base=_BASE, qualifying_count=5)
    base_refs = _highlight_refs(amplitude=-1.8)
    k_dev = np.asarray(_K) - 1.0  # the roll's own colour axis
    cast = 0.08 * np.sign(k_dev)  # a cast built to sit exactly along it (green term 0)
    refs = tuple(float(v) for v in np.asarray(base_refs) + cast)
    floors, ceils = _floors_ceils_for(refs)

    result = corrected_floors(floors, ceils, lock, refs, base_offset=0.0)
    expected = _expected_corrected_floors(floors, ceils, lock, refs)
    np.testing.assert_allclose(result, expected, atol=1e-9)
    # And the cast is actually gone: retargeted to the roll's own colour,
    # not left as recorded.
    assert not np.allclose(result, floors, atol=1e-4)


def test_cast_perpendicular_to_the_roll_colour_axis_is_also_fully_corrected():
    """The same check with a cast perpendicular to `K - 1` — this direction
    already worked even under the retired (buggy) projection formula, so
    this test alone would not have caught the bug; it exists so the two
    directions are demonstrably treated alike."""
    lock = HighlightLock(k=_K, base=_BASE, qualifying_count=5)
    base_refs = _highlight_refs(amplitude=-1.8)
    k_dev = np.asarray(_K) - 1.0
    perpendicular = np.array([k_dev[2], 0.0, -k_dev[0]])  # rotate 90 degrees in R,B
    cast = 0.08 * perpendicular / max(np.linalg.norm(perpendicular), 1e-9)
    refs = tuple(float(v) for v in np.asarray(base_refs) + cast)
    floors, ceils = _floors_ceils_for(refs)

    result = corrected_floors(floors, ceils, lock, refs, base_offset=0.0)
    expected = _expected_corrected_floors(floors, ceils, lock, refs)
    np.testing.assert_allclose(result, expected, atol=1e-9)
    assert not np.allclose(result, floors, atol=1e-4)


def _expected_corrected_floors(floors, ceils, lock, highlight_refs):
    """The module docstring's §2 formula, computed independently of
    `corrected_floors` for the tests above to check against."""
    floors_arr = np.asarray(floors)
    ceils_arr = np.asarray(ceils)
    base_dev = ceils_arr - ceils_arr[1]
    a = highlight_refs[1] - lock.base[1]
    k_dev = np.asarray(lock.k) - 1.0
    dev_new = base_dev + a * k_dev
    return floors_arr[1] + dev_new


def test_non_qualifying_negative_uses_the_green_only_approximation():
    """A negative with no recorded `highlight_refs` (its own gate fell
    back) still gets a correction, from the green-only approximation
    (`floors[green] - lock.base[green]`) — no R/B is read to build it."""
    lock = HighlightLock(k=_K, base=_BASE, qualifying_count=5)
    refs = _highlight_refs(k=(1.4, 1.0, 0.6), amplitude=-1.5)
    floors, ceils = _floors_ceils_for(refs)

    result = corrected_floors(floors, ceils, lock, highlight_refs=None, base_offset=0.0)
    approx_amplitude = floors[1] - lock.base[1]
    base_dev = np.asarray(ceils) - ceils[1]
    k_dev = np.asarray(lock.k) - 1.0
    expected = floors[1] + base_dev + approx_amplitude * k_dev
    np.testing.assert_allclose(result, expected, atol=1e-9)
    assert result[1] == floors[1]


def test_non_qualifying_amplitude_gate_falls_back_to_identity():
    """When even the green-only approximate amplitude is not sufficiently
    negative, the negative is left uncorrected rather than dividing by
    ~zero or applying a sign-flipped correction."""
    lock = HighlightLock(k=_K, base=_BASE, qualifying_count=5)
    # floors[green] essentially equal to the base green density.
    floors = (-1.4, _BASE[1] + 1e-6, -1.6)
    ceils = (-0.4, -0.2, -0.9)
    result = corrected_floors(floors, ceils, lock, highlight_refs=None, base_offset=0.0)
    assert result == floors


def test_degenerate_roll_k_falls_back_to_base_colour():
    """When the roll's `K` carries no colour variation at all (every
    channel's ratio equal to green's), the corrected deviation collapses
    to the film base's own colour, with no cast — no fitted amplitude to
    blow up, because the amplitude is read (or approximated), never
    fitted."""
    lock = HighlightLock(k=(1.0, 1.0, 1.0), base=_BASE, qualifying_count=5)
    refs = _highlight_refs(k=(1.4, 1.0, 0.6), amplitude=-1.5)
    floors, ceils = _floors_ceils_for(refs)
    result = corrected_floors(floors, ceils, lock, refs, base_offset=0.0)
    ceils_arr = np.asarray(ceils)
    base_dev = ceils_arr - ceils_arr[1]
    expected = floors[1] + base_dev
    np.testing.assert_allclose(result, expected, atol=1e-9)


def test_never_crosses_ceils():
    """The safety net: a channel whose corrected floor would land at or
    past its own ceil falls back to the original floor for that channel
    only — never degenerates the stretch, and never discards the other
    channels' correction."""
    lock = HighlightLock(k=(50.0, 1.0, -40.0), base=_BASE, qualifying_count=5)
    refs = _highlight_refs(k=(1.4, 1.0, 0.6), amplitude=-1.5)
    floors, ceils = _floors_ceils_for(refs)
    result = corrected_floors(floors, ceils, lock, refs, base_offset=0.0)
    assert all(f < c for f, c in zip(result, ceils, strict=True))
    # At least one channel's wild-K correction was rejected outright.
    assert any(r == f for r, f in zip(result, floors, strict=True))


# --- round 3: base_offset_for / same-exposure base frame --------------------


def test_dark_base_frame_with_level_offset_matches_zero_offset_roll():
    """A base frame recorded 0.6 log10 D darker than the roll's actual
    exposure, but with a measured `level_offset` of +0.6 (this negative's
    own rebate reads 0.6 above the recorded base), must produce exactly the
    same correction as a base already at the right level with no offset —
    `level_offset` is what makes the level source robust to a bad base
    frame."""
    dark_base = tuple(b - 0.6 for b in _BASE)  # what got recorded
    lock_dark = HighlightLock(k=_K, base=dark_base, qualifying_count=5)
    lock_zero = HighlightLock(k=_K, base=_BASE, qualifying_count=5)

    refs = _highlight_refs(k=(1.4, 1.0, 0.6), amplitude=-1.5)
    floors, ceils = _floors_ceils_for(refs)

    with_offset = corrected_floors(floors, ceils, lock_dark, refs, base_offset=0.6)
    zero_offset = corrected_floors(floors, ceils, lock_zero, refs, base_offset=0.0)
    np.testing.assert_allclose(with_offset, zero_offset, atol=1e-9)


def test_exif_mismatch_with_no_level_offset_gives_no_correction():
    record = {"highlight_refs": None, "exposure_matched": False}
    assert base_offset_for(record) is None

    lock = HighlightLock(k=_K, base=_BASE, qualifying_count=5)
    refs = _highlight_refs(k=(1.4, 1.0, 0.6), amplitude=-1.5)
    floors, ceils = _floors_ceils_for(refs)
    result = corrected_floors(floors, ceils, lock, refs, base_offset=base_offset_for(record))
    assert result == floors


def test_exif_mismatch_negative_does_not_contribute_to_k():
    roll = _FakeRoll(
        film_base={"density": list(_BASE)},
        negatives=[
            _negative(list(_highlight_refs(amplitude=-1.8)), exposure_matched=False),
        ],
    )
    assert compute_roll_highlight_lock(roll) is None  # the only negative doesn't qualify

    roll.negatives.append(
        _negative(list(_highlight_refs(amplitude=-1.8)), exposure_matched=True)
    )
    lock = compute_roll_highlight_lock(roll)
    assert lock is not None
    assert lock.qualifying_count == 1  # only the matched one


def test_exif_match_gives_a_correction_using_the_absolute_base():
    """The plain matched case: `base_offset_for` resolves to `0.0`, and the
    correction uses `lock.base` directly."""
    record = {"highlight_refs": None, "exposure_matched": True}
    assert base_offset_for(record) == 0.0

    lock = HighlightLock(k=_K, base=_BASE, qualifying_count=5)
    refs = _highlight_refs(k=(1.4, 1.0, 0.6), amplitude=-1.5)
    floors, ceils = _floors_ceils_for(refs)
    result = corrected_floors(floors, ceils, lock, None, base_offset=base_offset_for(record))
    assert result != floors
    assert result[1] == floors[1]


def test_level_offset_takes_priority_over_exposure_match():
    record = {"base_check": {"level_offset": 0.6}, "exposure_matched": False}
    assert base_offset_for(record) == 0.6


def test_never_raises_on_malformed_input():
    lock = HighlightLock(k=_K, base=_BASE, qualifying_count=1)
    assert corrected_floors((-1.4, -0.3), (-0.4, -0.2), lock) == (-1.4, -0.3)
    # Non-finite input: must not raise, and must return a 3-tuple.
    result = corrected_floors(
        (float("nan"), -0.3, -0.4), (-0.4, -0.2, -0.35), lock
    )
    assert len(result) == 3


# --- HighlightLock (de)serialization -----------------------------------------


def test_to_dict_from_dict_round_trip():
    lock = HighlightLock(k=_K, base=_BASE, qualifying_count=7)
    restored = HighlightLock.from_dict(lock.to_dict())
    assert restored == lock


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"k": [1.0, 2.0], "base": list(_BASE)},  # wrong length
        {"k": "not a list", "base": list(_BASE), "qualifying_count": 1},
        {"k": [1.0, 2.0, float("nan")], "base": list(_BASE), "qualifying_count": 1},
        {"k": list(_K), "qualifying_count": 1},  # missing base
        {"k": list(_K), "base": list(_BASE), "qualifying_count": "not an int"},
        "not a dict",
    ],
)
def test_from_dict_never_raises_on_malformed_input(data):
    assert HighlightLock.from_dict(data) is None
