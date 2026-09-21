"""Tests for ``roll refresh``'s highlight-lock recompute, auto-neutral
catch-up, and preview sync.

See docs/TETHER_PLAN.md §4.4 and auto_neutral.py.
"""

from __future__ import annotations

from scanny_boy import auto_neutral, roll_refresh
from scanny_boy.events import Code, WarningEvent
from scanny_boy.roll_manifest import load_roll_manifest
from scanny_boy.roll_refresh import run_roll_refresh
from scanny_boy.work_dir_support import (
    make_roll_dir,
    make_work_dir,
    run_stitch_with_defaults,
)


def test_deferred_stitch_then_refresh_matches_the_non_deferred_path(tmp_path):
    """The regression this bug is about: a `stitch --defer-roll-refresh`
    followed by `roll refresh` must leave a completed colour negative with
    the same `auto_neutral` block a non-deferred stitch produces — not
    `None`, and not a silently-never-measured block that leaves
    `color.auto_neutral_active()` false forever.

    `make_work_dir`'s synthetic content is seeded only by negative index,
    so two independent work dirs of the same shape stitch to the same
    pixels; the deferred roll (refreshed) and the non-deferred roll are
    therefore expected to land on the exact same measured block, not just
    "both non-null"."""
    (tmp_path / "deferred").mkdir()
    (tmp_path / "direct").mkdir()
    deferred_work = make_work_dir(tmp_path / "deferred")
    direct_work = make_work_dir(tmp_path / "direct")

    deferred_out = make_roll_dir(tmp_path, "deferred-out")
    result = run_stitch_with_defaults(
        deferred_work, deferred_out, defer_roll_refresh=True
    )
    assert result.status == "complete"

    # Before refresh: exactly the bug report's symptom — the deferred stitch
    # itself never measures auto-neutral, so the block is absent.
    roll = load_roll_manifest(deferred_out)
    assert roll.refresh_pending is True
    assert roll.negatives[0].normalization.get("auto_neutral") is None

    events: list = []
    outcome = run_roll_refresh(deferred_out, emit=events.append)
    assert not any(isinstance(e, WarningEvent) for e in events)

    refreshed = load_roll_manifest(deferred_out)
    assert refreshed.refresh_pending is False
    assert refreshed.negatives[0].normalization.get("auto_neutral") is not None

    direct_out = make_roll_dir(tmp_path, "direct-out")
    direct_result = run_stitch_with_defaults(
        direct_work, direct_out, defer_roll_refresh=False
    )
    assert direct_result.status == "complete"
    direct_roll = load_roll_manifest(direct_out)

    assert (
        refreshed.negatives[0].normalization["auto_neutral"]
        == direct_roll.negatives[0].normalization["auto_neutral"]
    )
    # The lock the refresh computed must also agree with the non-deferred
    # run's — refresh is not supposed to produce a colour-correction state
    # a stitch run couldn't have produced itself.
    assert refreshed.highlight_lock == direct_roll.highlight_lock
    assert outcome.lock_changed == (refreshed.highlight_lock is not None)


def test_refresh_forces_previews_when_only_auto_neutral_changed(tmp_path, monkeypatch):
    """`sync_previews(force=...)` must be requested whenever auto-neutral
    moved, even if the highlight lock happens not to have — mirrors
    `stitch_pipeline.py`'s `force=lock_changed or auto_neutral_changed`, so
    a deferred roll's cached preview PNGs don't go stale relative to what
    `roll info` now reports."""
    (tmp_path / "work").mkdir()
    work = make_work_dir(tmp_path / "work")
    out_dir = make_roll_dir(tmp_path, "out")
    assert run_stitch_with_defaults(work, out_dir, defer_roll_refresh=True).status == (
        "complete"
    )

    roll_before = load_roll_manifest(out_dir)
    # Pin the lock to whatever it already is, so this refresh's own
    # recompute cannot change it — isolating auto-neutral as the only thing
    # that can force previews.
    monkeypatch.setattr(
        roll_refresh.highlight_lock,
        "compute_roll_highlight_lock",
        lambda roll: (
            None
            if roll_before.highlight_lock is None
            else roll_refresh.highlight_lock.HighlightLock.from_dict(
                roll_before.highlight_lock
            )
        ),
    )

    calls: list[dict] = []
    real_sync = roll_refresh.previews.sync_previews

    def _spy_sync(roll_dir, roll, published, *, force):
        calls.append({"force": force})
        return real_sync(roll_dir, roll, published, force=force)

    monkeypatch.setattr(roll_refresh.previews, "sync_previews", _spy_sync)

    outcome = run_roll_refresh(out_dir, emit=lambda e: None)

    assert outcome.lock_changed is False
    assert len(calls) == 1
    assert calls[0]["force"] is True
    assert outcome.previews_regenerated is True


def test_one_negatives_measurement_failure_does_not_abort_the_others(
    tmp_path, monkeypatch
):
    """A corrupt or unreadable published TIFF for one negative must warn and
    move on, exactly like a preview failure does — not abort every other
    negative's catch-up measurement."""
    (tmp_path / "work").mkdir()
    work = make_work_dir(tmp_path / "work", negatives=2)
    out_dir = make_roll_dir(tmp_path, "out")
    assert run_stitch_with_defaults(work, out_dir, defer_roll_refresh=True).status == (
        "complete"
    )

    roll = load_roll_manifest(out_dir)
    assert len(roll.negatives) == 2
    failing_id = roll.negatives[0].negative_id

    real_recompute = auto_neutral.recompute_negative_auto_neutral

    def _flaky_recompute(negative, roll_dir, *, highlight_lock):
        if negative.negative_id == failing_id:
            raise RuntimeError("simulated corrupt TIFF")
        return real_recompute(negative, roll_dir, highlight_lock=highlight_lock)

    monkeypatch.setattr(
        roll_refresh.auto_neutral, "recompute_negative_auto_neutral", _flaky_recompute
    )

    events: list = []
    run_roll_refresh(out_dir, emit=events.append)

    warnings = [e for e in events if isinstance(e, WarningEvent)]
    assert len(warnings) == 1
    assert warnings[0].code == Code.INTERNAL_ERROR
    assert failing_id in warnings[0].message

    refreshed = load_roll_manifest(out_dir)
    by_id = {n.negative_id: n for n in refreshed.negatives}
    # The negative whose measurement raised is left untouched (still no
    # block) rather than the refresh aborting before the other negative
    # gets measured.
    assert by_id[failing_id].normalization.get("auto_neutral") is None
    other_id = next(n for n in refreshed.negatives if n.negative_id != failing_id)
    assert by_id[other_id.negative_id].normalization.get("auto_neutral") is not None
