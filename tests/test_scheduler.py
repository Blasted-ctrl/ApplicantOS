"""Everything on the beat schedule actually runs on a desktop install.

``PeriodicScheduler`` exists because a desktop app has no ``celery beat`` process, so the API
process ticks the schedule itself. It used to skip every crontab entry, on the reasoning that
a wall-clock time is meaningless on a machine that is asleep at 03:00 — which is true, and
which left two tasks running **nowhere**:

* ``sync.detect_ghosted`` is what turns a silent application into a ``ghosted`` one. Without
  it, an application the employer never answered stays ``submitted`` forever and the four-way
  summary counts it as still in play.
* ``cleanup.expire_postings`` is what stops the feed accumulating advertisements that closed
  months ago.

The translation is the fix and these tests are its contract: every entry Celery knows about
gets an in-process schedule, and no entry is silently dropped.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.workers.celery_app import BEAT_SCHEDULE
from app.workers.scheduler import CRONTAB_FIRST_DELAY, CRONTAB_INTERVAL, _entries

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def test_every_beat_entry_gets_an_in_process_schedule() -> None:
    """The regression: a task in ``BEAT_SCHEDULE`` that this scheduler drops runs nowhere.

    Asserted against the whole schedule rather than against the two known crontabs, so adding
    a third crontab entry cannot reintroduce the gap silently.
    """
    scheduled = {entry.task for entry in _entries(NOW)}

    assert scheduled == set(BEAT_SCHEDULE)


def test_the_two_daily_sweeps_are_among_them() -> None:
    """Named explicitly, because these are the two that were missing."""
    scheduled = {entry.task for entry in _entries(NOW)}

    assert "sync.detect_ghosted" in scheduled
    assert "cleanup.expire_postings" in scheduled


def test_a_translated_crontab_fires_within_a_short_session() -> None:
    """A daily interval measured from launch never elapses on a machine used for an evening.

    The first firing is minutes after start, not a day after it, which is the whole point:
    ghost detection that only runs on a machine left on for 24 hours is ghost detection that
    does not run.
    """
    translated = [
        entry
        for entry in _entries(NOW)
        if not isinstance(BEAT_SCHEDULE[entry.task].get("schedule"), timedelta)
    ]

    assert translated, "expected at least one crontab entry to translate"
    for entry in translated:
        assert entry.due_at - NOW == CRONTAB_FIRST_DELAY
        assert entry.interval == CRONTAB_INTERVAL
        assert entry.interval < timedelta(days=1)


def test_fixed_interval_entries_are_untouched() -> None:
    """Translation applies to crontabs only; everything else keeps Celery's own interval.

    A first firing one full interval out is what stops the scheduler stampeding every task
    at launch.
    """
    for entry in _entries(NOW):
        declared = BEAT_SCHEDULE[entry.task].get("schedule")
        if not isinstance(declared, timedelta):
            continue
        assert entry.interval == declared
        assert entry.due_at - NOW == declared


def test_the_run_loop_is_the_most_frequent_entry() -> None:
    """A stop the user pressed is noticed on this tick, so nothing may out-rank it.

    Guards the pacing rather than the plumbing: if some future sweep were scheduled more
    often than ``session.advance``, the loop that responds to Stop would be the slowest thing
    on the schedule.
    """
    intervals = {entry.task: entry.interval for entry in _entries(NOW)}

    assert intervals["session.advance"] == min(intervals.values())
