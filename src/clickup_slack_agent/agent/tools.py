"""The tools the agent can call, and how they execute.

Reads and writes are separated deliberately: writes are marked, and the loop
refuses to run them without a confirmation from Slack. The model can *ask*
to change the board; only you can approve it.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..clickup.client import ClickUpClient
from ..clickup.models import Task
from .llm import ToolSpec

log = logging.getLogger(__name__)

MAX_TASKS_RETURNED = 40


@dataclass(frozen=True)
class Tool:
    spec: ToolSpec
    handler: Callable[..., Any]
    is_write: bool = False


class ToolRegistry:
    """Binds tool schemas to a ClickUp client and this user's identity."""

    def __init__(self, clickup: ClickUpClient, user_id: int, tz: ZoneInfo) -> None:
        self.clickup = clickup
        self.user_id = user_id
        self.tz = tz
        self._tools: dict[str, Tool] = {t.spec.name: t for t in self._build()}

    # -- exposure ---------------------------------------------------------

    @property
    def specs(self) -> list[ToolSpec]:
        return [t.spec for t in self._tools.values()]

    def is_write(self, name: str) -> bool:
        tool = self._tools.get(name)
        return bool(tool and tool.is_write)

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        """Run a tool and return a JSON string for the model to read.

        Errors come back as data rather than exceptions so the model can
        recover — a wrong task id should produce a correction, not a crash.
        """
        tool = self._tools.get(name)
        if tool is None:
            return json.dumps({"error": f"unknown tool: {name}"})
        try:
            return json.dumps(tool.handler(**arguments), default=str)
        except TypeError as exc:
            return json.dumps({"error": f"bad arguments for {name}: {exc}"})
        except Exception as exc:  # noqa: BLE001 - surfaced to the model, not raised
            log.warning("tool %s failed: %s", name, exc)
            return json.dumps({"error": str(exc)})

    # -- serialisation ----------------------------------------------------

    def _summarise(self, task: Task) -> dict[str, Any]:
        return {
            "id": task.id,
            "name": task.name,
            "status": task.status.name,
            "is_finished": task.status.is_terminal,
            "priority": task.priority.label,
            "due_date": task.due_date.astimezone(self.tz).date().isoformat()
            if task.due_date
            else None,
            "list": task.list_name,
            "url": task.url,
        }

    def _my_tasks(self, *, include_closed: bool = False) -> list[Task]:
        return self.clickup.get_tasks(assignee_ids=[self.user_id], include_closed=include_closed)

    def _in_window(self, task: Task, start: date | None, end: date | None) -> bool:
        if start is None and end is None:
            return True
        if task.due_date is None:
            return False
        due = task.due_date.astimezone(self.tz).date()
        if start and due < start:
            return False
        return not (end and due > end)

    # -- handlers ---------------------------------------------------------

    def _get_my_tasks(
        self,
        due_from: str | None = None,
        due_to: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        include_finished: bool = False,
    ) -> dict[str, Any]:
        start = date.fromisoformat(due_from) if due_from else None
        end = date.fromisoformat(due_to) if due_to else None

        tasks = [
            t
            for t in self._my_tasks(include_closed=include_finished)
            if self._in_window(t, start, end)
            and (include_finished or not t.status.is_terminal)
            and (status is None or status.lower() in t.status.name.lower())
            and (priority is None or priority.lower() == t.priority.label.lower())
        ]
        tasks.sort(key=lambda t: (t.priority, t.due_date or datetime.max.replace(tzinfo=self.tz)))
        return {
            "count": len(tasks),
            "tasks": [self._summarise(t) for t in tasks[:MAX_TASKS_RETURNED]],
        }

    def _search_tasks(self, query: str) -> dict[str, Any]:
        needle = query.lower()
        hits = [t for t in self._my_tasks(include_closed=True) if needle in t.name.lower()]
        return {"count": len(hits), "tasks": [self._summarise(t) for t in hits[:20]]}

    def _get_task_details(self, task_id: str) -> dict[str, Any]:
        task = self.clickup.get_task(task_id)
        detail = self._summarise(task)
        detail["last_updated"] = (
            task.date_updated.astimezone(self.tz).isoformat() if task.date_updated else None
        )
        return detail

    def _get_task_comments(self, task_id: str) -> dict[str, Any]:
        comments = self.clickup.get_comments(task_id)
        return {
            "count": len(comments),
            "comments": [
                {
                    "author": c.author,
                    "text": c.text,
                    "at": c.created.astimezone(self.tz).isoformat() if c.created else None,
                }
                for c in comments
            ],
        }

    def _list_statuses(self) -> dict[str, Any]:
        lists = self.clickup.get_lists()
        if not lists:
            return {"statuses": []}
        statuses = self.clickup.get_statuses(lists[0].id)
        return {
            "list": lists[0].name,
            "statuses": [
                {"name": s.name, "is_finished": s.is_terminal, "group": s.type} for s in statuses
            ],
        }

    def _update_task_status(self, task_id: str, status: str) -> dict[str, Any]:
        task = self.clickup.update_status(task_id, status)
        return {"updated": True, **self._summarise(task)}

    def _add_comment(self, task_id: str, text: str) -> dict[str, Any]:
        comment_id = self.clickup.add_comment(task_id, text)
        return {"added": True, "comment_id": comment_id, "task_id": task_id}

    # -- schemas ----------------------------------------------------------

    def _build(self) -> list[Tool]:
        today = datetime.now(self.tz).date()
        tomorrow = today + timedelta(days=1)

        return [
            Tool(
                ToolSpec(
                    name="get_my_tasks",
                    description=(
                        "Tasks assigned to the user, optionally narrowed by due-date range, "
                        "status or priority. Use this for questions like 'what's due today', "
                        "'what was assigned for August 29th', or 'what's in progress'. "
                        "Omit the date arguments to get everything currently open."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "due_from": {
                                "type": "string",
                                "description": f"Earliest due date, YYYY-MM-DD (e.g. {today}).",
                            },
                            "due_to": {
                                "type": "string",
                                "description": (
                                    "Latest due date, YYYY-MM-DD. For a single day, set this "
                                    f"equal to due_from (e.g. both {today})."
                                ),
                            },
                            "status": {
                                "type": "string",
                                "description": (
                                    "Match a status by name, e.g. 'in progress', 'blocked'. "
                                    "Call list_statuses first if unsure what exists."
                                ),
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["Urgent", "High", "Normal", "Low"],
                            },
                            "include_finished": {
                                "type": "boolean",
                                "description": "Include completed tasks. Defaults to false.",
                            },
                        },
                        "required": [],
                    },
                ),
                self._get_my_tasks,
            ),
            Tool(
                ToolSpec(
                    name="search_tasks",
                    description=(
                        "Find tasks whose title contains the given text. Use this to resolve "
                        "a task the user referred to by name, e.g. 'the payment ticket', "
                        "before calling a tool that needs a task_id."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Words from the title."}
                        },
                        "required": ["query"],
                    },
                ),
                self._search_tasks,
            ),
            Tool(
                ToolSpec(
                    name="get_task_details",
                    description="Full detail for one task, including when it was last updated.",
                    parameters={
                        "type": "object",
                        "properties": {"task_id": {"type": "string"}},
                        "required": ["task_id"],
                    },
                ),
                self._get_task_details,
            ),
            Tool(
                ToolSpec(
                    name="get_task_comments",
                    description=(
                        "Read the comment thread on a task. Use this when the user asks why "
                        "something is blocked or what the latest is on a task — the answer is "
                        "usually in the comments, not the status."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {"task_id": {"type": "string"}},
                        "required": ["task_id"],
                    },
                ),
                self._get_task_comments,
            ),
            Tool(
                ToolSpec(
                    name="list_statuses",
                    description=(
                        "The statuses this board actually has, and which count as finished. "
                        "Call this before changing a status so you use a real one."
                    ),
                    parameters={"type": "object", "properties": {}, "required": []},
                ),
                self._list_statuses,
            ),
            Tool(
                ToolSpec(
                    name="update_task_status",
                    description=(
                        "Move a task to a different status. This changes the real board, so it "
                        f"is confirmed by the user before it runs. Today is {today}, "
                        f"tomorrow is {tomorrow}."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "string"},
                            "status": {
                                "type": "string",
                                "description": "An existing status name from list_statuses.",
                            },
                        },
                        "required": ["task_id", "status"],
                    },
                ),
                self._update_task_status,
                is_write=True,
            ),
            Tool(
                ToolSpec(
                    name="add_comment",
                    description=(
                        "Post a comment on a task. This is visible to everyone on the board, "
                        "so it is confirmed by the user before it runs."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "string"},
                            "text": {"type": "string"},
                        },
                        "required": ["task_id", "text"],
                    },
                ),
                self._add_comment,
                is_write=True,
            ),
        ]
