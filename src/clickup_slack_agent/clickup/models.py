"""Typed views over the slice of ClickUp's API we actually use.

ClickUp returns timestamps as epoch-milliseconds *strings* and omits fields
rather than nulling them, so most of the work here is normalising that into
something the rest of the app can rely on.
"""

from datetime import UTC, datetime
from enum import IntEnum

from pydantic import BaseModel, Field, field_validator

# Status types ClickUp reports. "done" and "closed" both mean the work is
# finished; the split only matters inside ClickUp's own UI.
TERMINAL_TYPES = frozenset({"done", "closed"})


def _epoch_ms(value: str | int | None) -> datetime | None:
    if value in (None, "", "null"):
        return None
    return datetime.fromtimestamp(int(value) / 1000, UTC)


class Priority(IntEnum):
    """Sortable priority. ClickUp sends orderindex 1-4, or omits it entirely."""

    URGENT = 1
    HIGH = 2
    NORMAL = 3
    LOW = 4
    NONE = 5

    @property
    def label(self) -> str:
        return self.name.title() if self is not Priority.NONE else "—"

    @classmethod
    def parse(cls, raw: dict | None) -> "Priority":
        if not raw:
            return cls.NONE
        try:
            return cls(int(raw["orderindex"]))
        except (KeyError, ValueError, TypeError):
            return cls.NONE


class Status(BaseModel):
    name: str = Field(alias="status")
    type: str

    @property
    def is_terminal(self) -> bool:
        """True once the task is done — derived from ClickUp's own grouping,
        never from the status name, so new statuses work without a code change.
        """
        return self.type in TERMINAL_TYPES

    @property
    def display(self) -> str:
        return self.name.title()


class Task(BaseModel):
    id: str
    name: str
    status: Status
    url: str
    due_date: datetime | None = None
    date_updated: datetime | None = None
    priority: Priority = Priority.NONE
    list_id: str = ""
    list_name: str = ""
    assignee_ids: list[int] = Field(default_factory=list)

    @field_validator("due_date", "date_updated", mode="before")
    @classmethod
    def _parse_epoch(cls, v: object) -> datetime | None:
        return _epoch_ms(v) if not isinstance(v, datetime) else v

    @classmethod
    def from_api(cls, raw: dict) -> "Task":
        lst = raw.get("list") or {}
        return cls(
            id=raw["id"],
            name=raw["name"],
            status=Status.model_validate(raw["status"]),
            url=raw.get("url", ""),
            due_date=raw.get("due_date"),
            date_updated=raw.get("date_updated"),
            priority=Priority.parse(raw.get("priority")),
            list_id=str(lst.get("id", "")),
            list_name=lst.get("name", ""),
            assignee_ids=[int(a["id"]) for a in raw.get("assignees", [])],
        )


class Comment(BaseModel):
    id: str
    text: str
    author: str
    created: datetime | None = None

    @classmethod
    def from_api(cls, raw: dict) -> "Comment":
        return cls(
            id=raw["id"],
            text=raw.get("comment_text", "").strip(),
            author=(raw.get("user") or {}).get("username", "unknown"),
            created=_epoch_ms(raw.get("date")),
        )


class TaskList(BaseModel):
    id: str
    name: str
    space_name: str = ""
