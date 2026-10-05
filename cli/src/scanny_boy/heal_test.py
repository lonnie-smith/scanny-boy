"""Tests for `HealParams` (`heal.py`): the bundled `scratches`/`spots`
params previews and the exporter thread as one keyword. Fast tier only —
no RAW, no TIFF, no stitched roll."""

from __future__ import annotations

import numpy as np

from scanny_boy import deband, heal, scratches, spots
from scanny_boy.library.repo import EditState


def _state(**overrides) -> EditState:
    fields = {
        "quarter_turns": 0,
        "flipped": False,
        "fine_angle_deg": 0.0,
        "tone": None,
        "color": None,
        "spots": None,
        "scratches": None,
        "deband": None,
        "crop": None,
    }
    fields.update(overrides)
    return EditState(**fields)


def test_default_heal_params_is_a_no_op(monkeypatch):
    """`heal.NONE` — the default every caller gets when it has no heal
    state to thread — must not touch a single pixel."""
    image = (np.arange(20 * 30 * 3, dtype=np.uint16).reshape(20, 30, 3) * 97) % 60000

    def _boom(*_args, **_kwargs):
        raise AssertionError("a no-op HealParams must never touch pixels")

    # Neither op's own guard (`is_live`/`is_repairing`) should even need to
    # run past the "params is None" check, but this pins the behaviour
    # regardless of how either module's internals evolve.
    monkeypatch.setattr(scratches, "_apply_one_scratch", _boom)

    result = heal.apply(image, heal.NONE)
    np.testing.assert_array_equal(result, image)


def test_from_state_picks_up_every_heal_op():
    """`HealParams.from_state` reads exactly `state.scratches`,
    `state.deband` and `state.spots` — the heal fields `EditState`
    carries — and nothing else."""
    scratches_state = {"enabled": True, "canvas": [10, 10], "scratches": []}
    deband_state = {"enabled": True, "canvas": [10, 10], "regions": []}
    spots_state = {"repair": True, "canvas": [10, 10], "spots": []}
    state = _state(scratches=scratches_state, deband=deband_state, spots=spots_state)

    params = heal.HealParams.from_state(state)

    assert params.scratches == scratches_state
    assert params.deband == deband_state
    assert params.spots == spots_state


def test_from_state_with_no_heal_ops_is_the_default():
    state = _state()
    assert heal.HealParams.from_state(state) == heal.HealParams()


def test_apply_replays_scratches_then_deband_then_spots(monkeypatch):
    """The canonical order — scratch correction, band removal, then spot repair — lives
    in exactly one place now: `heal.apply`. Pin it by recording each op's
    call order rather than asserting on pixels."""
    image = (np.arange(20 * 30 * 3, dtype=np.uint16).reshape(20, 30, 3) * 97) % 60000
    order: list[str] = []

    def fake_scratches_apply(image_codes, params, **kwargs):
        order.append("scratches")
        return image_codes

    def fake_deband_apply(image_codes, params, **kwargs):
        order.append("deband")
        return image_codes

    def fake_spots_apply_repair(image_codes, params):
        order.append("spots")
        return image_codes

    monkeypatch.setattr(scratches, "apply", fake_scratches_apply)
    monkeypatch.setattr(deband, "apply", fake_deband_apply)
    monkeypatch.setattr(spots, "apply_repair", fake_spots_apply_repair)

    params = heal.HealParams(
        scratches={"enabled": True},
        deband={"enabled": True},
        spots={"repair": True},
    )
    heal.apply(image, params)

    assert order == ["scratches", "deband", "spots"]


def test_cache_key_differs_between_default_and_populated_params():
    populated = heal.HealParams(
        scratches={"enabled": True, "canvas": [10, 10], "scratches": []},
        spots={"repair": True, "canvas": [10, 10], "spots": []},
    )
    assert heal.cache_key(heal.NONE) != heal.cache_key(populated)


def test_cache_key_is_stable_across_calls():
    """The same params must hash to the same key every time — an unstable
    key would defeat the pixel cache it feeds."""
    params = heal.HealParams(
        scratches={"enabled": True, "canvas": [10, 10], "scratches": []},
        spots={"repair": True, "canvas": [10, 10], "spots": []},
    )
    assert heal.cache_key(params) == heal.cache_key(params)
    # A fresh, equal-but-not-identical HealParams (and equal-but-not-
    # identical param dicts) must still land on the same key.
    same_again = heal.HealParams(
        scratches=dict(params.scratches),
        spots=dict(params.spots),
    )
    assert heal.cache_key(params) == heal.cache_key(same_again)


def test_cache_key_has_a_deband_element_that_tracks_the_op():
    """The key gained a third element for the deband op (a new key shape —
    the pixel cache is in-memory, so that is safe): None when absent, a
    hash of the whole op when present, so any region/strength/enabled change
    is a different key."""
    none_key = heal.cache_key(heal.NONE)
    assert len(none_key) == 3
    assert none_key[2] == (None,)

    op = {"enabled": True, "strength": 1.0, "regions": [{"id": 1, "corr": "AAAA"}]}
    base = heal.cache_key(heal.HealParams(deband=op))
    assert base[2][0] == "deband"
    assert base != none_key
    assert base == heal.cache_key(heal.HealParams(deband=dict(op)))
    for changed in (
        {**op, "strength": 0.9},
        {**op, "enabled": False},
        {**op, "regions": [{"id": 1, "corr": "AAAB"}]},
        {**op, "regions": []},
    ):
        assert heal.cache_key(heal.HealParams(deband=changed)) != base


def test_cache_key_changes_when_either_op_changes():
    base = heal.HealParams(
        scratches={"enabled": True, "canvas": [10, 10], "scratches": []},
        spots={"repair": True, "canvas": [10, 10], "spots": []},
    )
    different_spots = heal.HealParams(
        scratches=base.scratches,
        spots={"repair": True, "canvas": [10, 10], "spots": [{"id": 1}]},
    )
    different_scratches = heal.HealParams(
        scratches={"enabled": False, "canvas": [10, 10], "scratches": []},
        spots=base.spots,
    )
    assert heal.cache_key(base) != heal.cache_key(different_spots)
    assert heal.cache_key(base) != heal.cache_key(different_scratches)
