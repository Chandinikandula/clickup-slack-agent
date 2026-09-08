"""Tests for the ClickUp client. All HTTP is mocked — CI never calls ClickUp."""

import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from clickup_slack_agent.clickup.client import BASE_URL, ClickUpClient, ClickUpError
from clickup_slack_agent.clickup.models import Priority, Status, Task

TEAM = "9001"


def make_client(**kwargs) -> ClickUpClient:
    return ClickUpClient("pk_test", TEAM, **kwargs)


def raw_task(
    task_id: str = "abc",
    *,
    list_id: str = "L1",
    status: str = "in progress",
    status_type: str = "custom",
    due: int | None = 1_757_260_800_000,  # 2025-09-07T16:00:00Z
    priority: str | None = "2",
) -> dict:
    return {
        "id": task_id,
        "name": f"Task {task_id}",
        "status": {"status": status, "type": status_type},
        "url": f"https://app.clickup.com/t/{task_id}",
        "due_date": str(due) if due else None,
        "date_updated": "1757260800000",
        "priority": {"orderindex": priority, "priority": "high"} if priority else None,
        "list": {"id": list_id, "name": "Sprint Board"},
        "assignees": [{"id": 42}],
    }


# -- models ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("status_type", "terminal"),
    [("open", False), ("custom", False), ("done", True), ("closed", True)],
)
def test_terminal_is_derived_from_type_not_name(status_type: str, terminal: bool) -> None:
    # A status called "completed" sitting in the Active group is NOT terminal —
    # this is the whole reason we read type instead of matching names.
    status = Status(status="completed", type=status_type)
    assert status.is_terminal is terminal


def test_epoch_millis_become_utc_datetimes() -> None:
    task = Task.from_api(raw_task(due=1_757_260_800_000))
    assert task.due_date == datetime(2025, 9, 7, 16, 0, tzinfo=UTC)


def test_missing_due_date_is_none_not_epoch_zero() -> None:
    assert Task.from_api(raw_task(due=None)).due_date is None


def test_absent_priority_sorts_last() -> None:
    assert Task.from_api(raw_task(priority=None)).priority is Priority.NONE
    assert Priority.NONE > Priority.LOW


# -- list filtering -------------------------------------------------------


@respx.mock
def test_excluded_lists_are_dropped() -> None:
    respx.get(f"{BASE_URL}/team/{TEAM}/task").mock(
        return_value=httpx.Response(
            200, json={"tasks": [raw_task("keep", list_id="L1"), raw_task("drop", list_id="L2")]}
        )
    )
    tasks = make_client(exclude_list_ids=frozenset({"L2"})).get_tasks()
    assert [t.id for t in tasks] == ["keep"]


@respx.mock
def test_include_list_wins_over_exclude() -> None:
    respx.get(f"{BASE_URL}/team/{TEAM}/task").mock(
        return_value=httpx.Response(
            200, json={"tasks": [raw_task("a", list_id="L1"), raw_task("b", list_id="L2")]}
        )
    )
    client = make_client(include_list_ids=frozenset({"L2"}), exclude_list_ids=frozenset({"L2"}))
    assert [t.id for t in client.get_tasks()] == ["b"]


@respx.mock
def test_new_list_is_included_by_default() -> None:
    # The point of excluding rather than allowlisting: a list we have never
    # heard of still shows up, so tasks are never silently missing.
    respx.get(f"{BASE_URL}/team/{TEAM}/task").mock(
        return_value=httpx.Response(200, json={"tasks": [raw_task("new", list_id="L_BRAND_NEW")]})
    )
    assert len(make_client(exclude_list_ids=frozenset({"L2"})).get_tasks()) == 1


# -- paging and failures --------------------------------------------------


@respx.mock
def test_pages_until_a_short_page() -> None:
    full = {"tasks": [raw_task(str(i)) for i in range(100)]}
    route = respx.get(f"{BASE_URL}/team/{TEAM}/task").mock(
        side_effect=[
            httpx.Response(200, json=full),
            httpx.Response(200, json={"tasks": [raw_task("last")]}),
        ]
    )
    assert len(make_client().get_tasks()) == 101
    assert route.call_count == 2


@respx.mock
def test_rate_limit_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("clickup_slack_agent.clickup.client.time.sleep", lambda _: None)
    route = respx.get(f"{BASE_URL}/team/{TEAM}/task").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"tasks": [raw_task("ok")]}),
        ]
    )
    assert len(make_client().get_tasks()) == 1
    assert route.call_count == 2


@respx.mock
def test_bad_token_raises_a_readable_error() -> None:
    respx.get(f"{BASE_URL}/user").mock(return_value=httpx.Response(401, json={"err": "nope"}))
    with pytest.raises(ClickUpError, match="401"):
        make_client().get_current_user()


# -- writes ---------------------------------------------------------------


@respx.mock
def test_update_status_sends_only_the_status_field() -> None:
    route = respx.put(f"{BASE_URL}/task/abc").mock(
        return_value=httpx.Response(
            200, json=raw_task("abc", status="completed", status_type="done")
        )
    )
    task = make_client().update_status("abc", "completed")
    # Only the status — never echo back fields we did not intend to change.
    assert json.loads(route.calls.last.request.read()) == {"status": "completed"}
    assert task.status.is_terminal
