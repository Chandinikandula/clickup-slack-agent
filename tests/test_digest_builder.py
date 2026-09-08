"""Tests for digest bucketing — mostly about date boundaries in the user's tz."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from clickup_slack_agent.clickup.models import Priority, Status, Task
from clickup_slack_agent.digest.builder import build_digest

IST = ZoneInfo("Asia/Kolkata")
# 08:00 IST on Tue 8 Sep 2026 — a normal moment for the 09:30 job to look back from.
NOW = datetime(2026, 9, 8, 2, 30, tzinfo=UTC)


def task(
    name: str = "t",
    *,
    due: datetime | None = None,
    status: str = "in progress",
    status_type: str = "custom",
    priority: Priority = Priority.NORMAL,
) -> Task:
    return Task(
        id=name,
        name=name,
        status=Status(status=status, type=status_type),
        url=f"https://app.clickup.com/t/{name}",
        due_date=due,
        priority=priority,
    )


def test_late_evening_due_date_counts_as_the_local_next_day() -> None:
    # 22:00 UTC on the 8th is 03:30 IST on the 9th. Comparing in UTC would
    # call this "due today"; in Kolkata it is tomorrow.
    late = task("late", due=datetime(2026, 9, 8, 22, 0, tzinfo=UTC))
    digest = build_digest([late], tz=IST, now=NOW)

    assert digest.due_today == []
    assert digest.upcoming == [late]


def test_early_morning_utc_is_still_today_locally() -> None:
    # 00:30 UTC on the 8th is 06:00 IST on the 8th — genuinely today.
    early = task("early", due=datetime(2026, 9, 8, 0, 30, tzinfo=UTC))
    assert build_digest([early], tz=IST, now=NOW).due_today == [early]


def test_yesterdays_unfinished_work_is_carried_over() -> None:
    stale = task("stale", due=datetime(2026, 9, 7, 6, 0, tzinfo=UTC))
    assert build_digest([stale], tz=IST, now=NOW).overdue == [stale]


def test_finished_tasks_are_dropped_however_late() -> None:
    # The digest is about what still needs you, not what happened.
    done = task("done", due=datetime(2026, 9, 1, 6, 0, tzinfo=UTC), status_type="done")
    closed = task("closed", due=datetime(2026, 9, 1, 6, 0, tzinfo=UTC), status_type="closed")
    assert build_digest([done, closed], tz=IST, now=NOW).is_empty


def test_started_work_without_a_due_date_still_surfaces() -> None:
    # This is the task that quietly rots for three weeks.
    wip = task("wip", due=None, status_type="custom")
    assert build_digest([wip], tz=IST, now=NOW).in_progress == [wip]


def test_untouched_backlog_without_a_due_date_stays_out() -> None:
    # Not started and not due — including it would make the digest a backlog dump.
    backlog = task("backlog", due=None, status="open", status_type="open")
    assert build_digest([backlog], tz=IST, now=NOW).is_empty


def test_far_future_work_is_not_upcoming() -> None:
    soon = task("soon", due=datetime(2026, 9, 10, 6, 0, tzinfo=UTC))
    later = task("later", due=datetime(2026, 10, 1, 6, 0, tzinfo=UTC))
    digest = build_digest([soon, later], tz=IST, now=NOW, upcoming_days=3)
    assert digest.upcoming == [soon]


def test_each_bucket_leads_with_the_most_urgent() -> None:
    low = task("low", due=datetime(2026, 9, 8, 6, 0, tzinfo=UTC), priority=Priority.LOW)
    urgent = task("urgent", due=datetime(2026, 9, 8, 6, 0, tzinfo=UTC), priority=Priority.URGENT)
    digest = build_digest([low, urgent], tz=IST, now=NOW)
    assert [t.name for t in digest.due_today] == ["urgent", "low"]


def test_timezone_choice_changes_the_answer() -> None:
    # Same instant, two users: proof the tz is genuinely applied rather than
    # the machine's local time leaking in.
    late = task("late", due=datetime(2026, 9, 8, 22, 0, tzinfo=UTC))
    assert build_digest([late], tz=ZoneInfo("UTC"), now=NOW).due_today == [late]
    assert build_digest([late], tz=IST, now=NOW).due_today == []
