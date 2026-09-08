"""Render a Digest as Slack Block Kit.

Kept separate from the digest logic so the wording can change without
touching the rules about what counts as overdue.
"""

from datetime import date

from ..clickup.models import Priority, Task
from ..digest.builder import Digest

# Slack truncates hard rather than erroring, so stay well inside its limits.
MAX_SECTION_CHARS = 2900
MAX_TASKS_PER_SECTION = 12

PRIORITY_ICON = {
    Priority.URGENT: "🔴",
    Priority.HIGH: "🟠",
    Priority.NORMAL: "🔵",
    Priority.LOW: "⚪",
    Priority.NONE: "▫️",
}


def _days_late(task: Task, today: date) -> str:
    if task.due_date is None:
        return ""
    late = (today - task.due_date.date()).days
    if late == 1:
        return " · 1 day late"
    return f" · {late} days late" if late > 1 else ""


def _task_line(task: Task, today: date, *, show_late: bool = False) -> str:
    icon = PRIORITY_ICON[task.priority]
    name = task.name.replace("<", "&lt;").replace(">", "&gt;")
    link = f"<{task.url}|{name}>" if task.url else name
    late = _days_late(task, today) if show_late else ""
    return f"{icon} {link} · _{task.status.display}_{late}"


def _section(title: str, tasks: list[Task], today: date, *, show_late: bool = False) -> list[dict]:
    if not tasks:
        return []

    shown = tasks[:MAX_TASKS_PER_SECTION]
    lines = [_task_line(t, today, show_late=show_late) for t in shown]
    if len(tasks) > len(shown):
        lines.append(f"_…and {len(tasks) - len(shown)} more_")

    body = "\n".join(lines)
    if len(body) > MAX_SECTION_CHARS:
        body = body[:MAX_SECTION_CHARS].rsplit("\n", 1)[0] + "\n_…truncated_"

    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title} ({len(tasks)})*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
    ]


def digest_blocks(digest: Digest) -> list[dict]:
    """The morning message. Ordered by what you should look at first."""
    pretty_date = digest.on_date.strftime("%A, %-d %B")

    if digest.is_empty:
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{pretty_date}*\nNothing due and nothing in flight. Clear day. 🌤",
                },
            }
        ]

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{pretty_date} · {digest.total} tasks"},
        }
    ]

    blocks += _section("⚠️ Overdue", digest.overdue, digest.on_date, show_late=True)
    blocks += _section("📌 Due today", digest.due_today, digest.on_date)
    blocks += _section("🔨 In flight, no due date", digest.in_progress, digest.on_date)
    blocks += _section("📅 Coming up", digest.upcoming, digest.on_date)

    blocks.append(
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": "Ask me anything — _what's blocked?_, _priority today?_"}
            ],
        }
    )
    return blocks


def digest_text(digest: Digest) -> str:
    """Notification fallback — what shows on a phone lock screen."""
    if digest.is_empty:
        return "No tasks need you today."
    parts = []
    if digest.overdue:
        parts.append(f"{len(digest.overdue)} overdue")
    if digest.due_today:
        parts.append(f"{len(digest.due_today)} due today")
    if digest.in_progress:
        parts.append(f"{len(digest.in_progress)} in flight")
    return "Your day: " + ", ".join(parts)


def error_blocks(message: str) -> list[dict]:
    """Say so when the digest could not be built.

    Silence is the dangerous failure — you would assume a clear day.
    """
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":warning: *Could not build your digest*\n```{message[:500]}```",
            },
        }
    ]
