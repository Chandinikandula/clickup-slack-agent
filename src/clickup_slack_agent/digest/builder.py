"""Decide what belongs in the morning digest.

Pure functions over a list of tasks — no HTTP, no clock, no Slack. That makes
the date-boundary rules (the part most likely to be subtly wrong) directly
testable, and keeps "what to say" separate from "how to say it".
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from ..clickup.models import Priority, Task


@dataclass(frozen=True)
class Digest:
    """What the morning message will contain, already bucketed and ordered."""

    on_date: date
    overdue: list[Task] = field(default_factory=list)
    due_today: list[Task] = field(default_factory=list)
    in_progress: list[Task] = field(default_factory=list)
    upcoming: list[Task] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.overdue or self.due_today or self.in_progress or self.upcoming)

    @property
    def total(self) -> int:
        return len(self.overdue) + len(self.due_today) + len(self.in_progress) + len(self.upcoming)


def _local_date(when: datetime, tz: ZoneInfo) -> date:
    """A task due 23:30 UTC is due *tomorrow* in Kolkata.

    ClickUp stores due dates as UTC epoch millis, so every comparison has to
    happen in the user's timezone or the digest is a day out for late-evening
    due dates — the single most likely bug in this whole file.
    """
    return when.astimezone(tz).date()


def _sort_key(task: Task) -> tuple[Priority, str]:
    return (task.priority, task.name.lower())


def build_digest(
    tasks: list[Task],
    *,
    tz: ZoneInfo,
    now: datetime,
    upcoming_days: int = 3,
) -> Digest:
    """Bucket a day's work into what needs attention, most urgent first.

    Terminal tasks are dropped: a task finished yesterday is not carried over,
    which is the difference between a digest you read and one you ignore.
    """
    today = _local_date(now, tz)

    overdue: list[Task] = []
    due_today: list[Task] = []
    in_progress: list[Task] = []
    upcoming: list[Task] = []

    for task in tasks:
        if task.status.is_terminal:
            continue

        if task.due_date is None:
            # No due date, but someone has started it — exactly the work that
            # goes stale unnoticed, so it earns a place in the digest.
            if task.status.type == "custom":
                in_progress.append(task)
            continue

        due = _local_date(task.due_date, tz)
        if due < today:
            overdue.append(task)
        elif due == today:
            due_today.append(task)
        elif (due - today).days <= upcoming_days:
            upcoming.append(task)

    return Digest(
        on_date=today,
        overdue=sorted(overdue, key=_sort_key),
        due_today=sorted(due_today, key=_sort_key),
        in_progress=sorted(in_progress, key=_sort_key),
        upcoming=sorted(upcoming, key=lambda t: (t.due_date or now, t.priority)),
    )
