import datetime

from scanny_boy.roll_manifest import CaptureTime, RollMetadata
from scanny_boy.roll_manifest_test import _completed_negative, _manifest, _run
from scanny_boy.roll_sequence import intended_times, sequence_negatives


def test_sequence_orders_by_capture_time_across_runs():
    """Capture time, not append order, decides the sequence: `run-2` was
    appended second but its negative was shot earlier."""
    manifest = _manifest(
        runs=[_run(run_id="run-1"), _run(run_id="run-2")],
        negatives=[
            _completed_negative(
                negative_id="run-1-negative-01",
                run_id="run-1",
                capture_time=CaptureTime(source_datetime_original="2026-08-02T12:00:10"),
            ),
            _completed_negative(
                negative_id="run-2-negative-01",
                run_id="run-2",
                capture_time=CaptureTime(source_datetime_original="2026-08-02T09:00:00"),
            ),
        ],
    )

    assert sequence_negatives(manifest) == ["run-2-negative-01", "run-1-negative-01"]


def test_sequence_ties_break_by_run_then_filename():
    same_time = CaptureTime(source_datetime_original="2026-08-02T12:00:00")
    manifest = _manifest(
        runs=[_run(run_id="run-1"), _run(run_id="run-2")],
        negatives=[
            _completed_negative(
                negative_id="a", run_id="run-1", members=["b.NEF"], capture_time=same_time
            ),
            _completed_negative(
                negative_id="b", run_id="run-1", members=["a.NEF"], capture_time=same_time
            ),
            _completed_negative(
                negative_id="c", run_id="run-2", members=["a.NEF"], capture_time=same_time
            ),
        ],
    )

    # Same capture time for all three: run-1's two negatives sort before
    # run-2's (run index), and within run-1 the earlier filename wins.
    assert sequence_negatives(manifest) == ["b", "a", "c"]


def test_intended_times_are_one_second_apart():
    manifest = _manifest(
        runs=[_run(run_id="run-1")],
        negatives=[
            _completed_negative(
                negative_id="a",
                members=["a.NEF"],
                capture_time=CaptureTime(source_datetime_original="2026-08-02T09:00:00"),
            ),
            _completed_negative(
                negative_id="b",
                members=["b.NEF"],
                capture_time=CaptureTime(source_datetime_original="2026-08-02T09:00:05"),
            ),
            _completed_negative(
                negative_id="c",
                members=["c.NEF"],
                capture_time=CaptureTime(source_datetime_original="2026-08-02T09:00:10"),
            ),
        ],
    )
    manifest.metadata = RollMetadata(roll_capture_date="2026-08-02")

    times = intended_times(manifest)
    ordered = [times[negative_id] for negative_id in sequence_negatives(manifest)]

    assert ordered[0] == datetime.datetime(2026, 8, 2, 12, 0, 0)  # noqa: DTZ001
    assert ordered[1] == ordered[0] + datetime.timedelta(seconds=1)
    assert ordered[2] == ordered[0] + datetime.timedelta(seconds=2)


def test_date_override_reranks_within_its_own_date():
    manifest = _manifest(
        runs=[_run(run_id="run-1")],
        negatives=[
            _completed_negative(
                negative_id="a",
                members=["a.NEF"],
                capture_time=CaptureTime(source_datetime_original="2026-08-02T09:00:00"),
            ),
            _completed_negative(
                negative_id="b",
                members=["b.NEF"],
                capture_time=CaptureTime(
                    source_datetime_original="2026-08-02T09:00:05",
                    date_override="2026-08-10",
                ),
            ),
            _completed_negative(
                negative_id="c",
                members=["c.NEF"],
                capture_time=CaptureTime(
                    source_datetime_original="2026-08-02T09:00:10",
                    date_override="2026-08-10",
                ),
            ),
        ],
    )
    manifest.metadata = RollMetadata(roll_capture_date="2026-08-02")

    times = intended_times(manifest)

    # "a" stays on the roll's own capture date, rank 1 there.
    assert times["a"] == datetime.datetime(2026, 8, 2, 12, 0, 0)  # noqa: DTZ001
    # "b" and "c" both override to 2026-08-10 and rank among themselves,
    # independent of "a"'s date or the overall sequence position.
    assert times["b"] == datetime.datetime(2026, 8, 10, 12, 0, 0)  # noqa: DTZ001
    assert times["c"] == datetime.datetime(2026, 8, 10, 12, 0, 1)  # noqa: DTZ001


def test_sequence_is_stable_when_nothing_changed():
    manifest = _manifest(
        negatives=[
            _completed_negative(negative_id="a"),
            _completed_negative(
                negative_id="b",
                members=["x.NEF"],
                capture_time=CaptureTime(source_datetime_original="2026-08-02T09:00:05"),
            ),
        ],
    )

    assert sequence_negatives(manifest) == sequence_negatives(manifest)
