"""Tests for the agent loop, driven by a scripted provider — no LLM calls.

The point of these is the control flow the loop owns: chaining, the write
gate, and the step ceiling. What a real model would *choose* is an eval
question, not a unit-test question.
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from clickup_slack_agent.agent.llm import LLMResponse, Message, ToolCall, ToolSpec
from clickup_slack_agent.agent.loop import MAX_STEPS, Agent
from clickup_slack_agent.agent.tools import ToolRegistry
from clickup_slack_agent.clickup.models import Comment, Status, Task, TaskList

IST = ZoneInfo("Asia/Kolkata")
USER_ID = 42


class ScriptedProvider:
    """Replays a fixed list of responses, recording what it was asked."""

    name = "scripted"
    model = "test"

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[list[Message]] = []

    def complete(
        self, *, system: str, messages: Sequence[Message], tools: Sequence[ToolSpec]
    ) -> LLMResponse:
        self.calls.append(list(messages))
        if not self._responses:
            return LLMResponse(text="(script exhausted)", tool_calls=[])
        return self._responses.pop(0)


class StubClickUp:
    """Just enough ClickUp to exercise the tools."""

    def __init__(self) -> None:
        self.status_updates: list[tuple[str, str]] = []
        self.comments_added: list[tuple[str, str]] = []

    def _task(self, task_id: str = "T1", status: str = "in progress") -> Task:
        return Task(
            id=task_id,
            name="Payment gateway integration",
            status=Status(status=status, type="custom"),
            url=f"https://app.clickup.com/t/{task_id}",
            due_date=datetime(2026, 9, 8, 6, 0, tzinfo=UTC),
            list_id="L1",
            list_name="Sprint Board",
        )

    def get_tasks(self, **_) -> list[Task]:
        return [self._task()]

    def get_task(self, task_id: str) -> Task:
        return self._task(task_id)

    def get_comments(self, task_id: str, limit: int = 20) -> list[Comment]:
        return [
            Comment(
                id="c1",
                text="Waiting on QA signoff since Tuesday",
                author="priya",
                created=datetime(2026, 9, 7, 9, 0, tzinfo=UTC),
            )
        ]

    def get_lists(self) -> list[TaskList]:
        return [TaskList(id="L1", name="Sprint Board")]

    def get_statuses(self, list_id: str) -> list[Status]:
        return [
            Status(status="in progress", type="custom"),
            Status(status="in review", type="custom"),
            Status(status="completed", type="done"),
        ]

    def update_status(self, task_id: str, status: str) -> Task:
        self.status_updates.append((task_id, status))
        return self._task(task_id, status=status)

    def add_comment(self, task_id: str, text: str, notify_all: bool = False) -> str:
        self.comments_added.append((task_id, text))
        return "c99"


@pytest.fixture
def clickup() -> StubClickUp:
    return StubClickUp()


@pytest.fixture
def registry(clickup: StubClickUp) -> ToolRegistry:
    return ToolRegistry(clickup, USER_ID, IST)


def agent_with(
    registry: ToolRegistry, responses: list[LLMResponse]
) -> tuple[Agent, ScriptedProvider]:
    provider = ScriptedProvider(responses)
    return Agent(provider, registry, timezone="Asia/Kolkata"), provider


def call(name: str, **args) -> ToolCall:
    return ToolCall(id=f"c_{name}", name=name, arguments=args)


# -- the loop -------------------------------------------------------------


def test_answers_directly_when_no_tool_is_needed(registry: ToolRegistry) -> None:
    agent, _ = agent_with(registry, [LLMResponse(text="Nothing due.", tool_calls=[])])
    result = agent.run("anything due?")
    assert result.text == "Nothing due."
    assert result.steps == []


def test_runs_a_tool_then_answers(registry: ToolRegistry) -> None:
    agent, _ = agent_with(
        registry,
        [
            LLMResponse(text="", tool_calls=[call("get_my_tasks")]),
            LLMResponse(text="One task.", tool_calls=[]),
        ],
    )
    result = agent.run("what's on?")
    assert result.tools_used == ["get_my_tasks"]
    assert result.text == "One task."


def test_chains_a_second_call_on_the_first_result(registry: ToolRegistry) -> None:
    # The behaviour that makes this an agent rather than a lookup: the second
    # tool is chosen after seeing what the first returned.
    agent, provider = agent_with(
        registry,
        [
            LLMResponse(text="", tool_calls=[call("search_tasks", query="payment")]),
            LLMResponse(text="", tool_calls=[call("get_task_comments", task_id="T1")]),
            LLMResponse(text="Waiting on QA.", tool_calls=[]),
        ],
    )
    result = agent.run("what's blocking the payment ticket?")

    assert result.tools_used == ["search_tasks", "get_task_comments"]
    # The comment text reached the model, which is what let it answer.
    assert "QA signoff" in result.steps[1].result
    assert len(provider.calls) == 3


def test_tool_results_are_fed_back_to_the_model(registry: ToolRegistry) -> None:
    agent, provider = agent_with(
        registry,
        [
            LLMResponse(text="", tool_calls=[call("get_my_tasks")]),
            LLMResponse(text="done", tool_calls=[]),
        ],
    )
    agent.run("what's on?")
    second_prompt = provider.calls[1]
    assert any(m.tool_results for m in second_prompt)


def test_stops_at_the_step_ceiling_and_says_so(registry: ToolRegistry) -> None:
    # A model that keeps calling tools must not be able to loop forever on
    # your quota — and the user must be told the answer is incomplete.
    looping = [
        LLMResponse(text="", tool_calls=[call("get_my_tasks")]) for _ in range(MAX_STEPS * 2)
    ]
    agent, _ = agent_with(registry, looping)
    result = agent.run("go round in circles")
    assert len(result.steps) == MAX_STEPS
    assert "could not settle" in result.text


# -- the write gate -------------------------------------------------------


def test_a_write_is_proposed_never_executed(registry: ToolRegistry, clickup: StubClickUp) -> None:
    agent, _ = agent_with(
        registry,
        [
            LLMResponse(
                text="", tool_calls=[call("update_task_status", task_id="T1", status="in review")]
            )
        ],
    )
    result = agent.run("move T1 to in review")

    assert result.pending_write is not None
    assert result.pending_write.name == "update_task_status"
    # The board was not touched.
    assert clickup.status_updates == []


def test_a_confirmed_write_does_execute(registry: ToolRegistry, clickup: StubClickUp) -> None:
    agent, _ = agent_with(registry, [])
    output = agent.execute_confirmed_write(
        call("update_task_status", task_id="T1", status="in review")
    )
    assert clickup.status_updates == [("T1", "in review")]
    assert json.loads(output)["updated"] is True


def test_the_confirm_path_refuses_a_read_tool(registry: ToolRegistry) -> None:
    # Only writes may travel this path — it must not become a second, ungated
    # way to run arbitrary tools.
    agent, _ = agent_with(registry, [])
    assert "not a write tool" in agent.execute_confirmed_write(call("get_my_tasks"))


def test_a_read_heavy_answer_never_reaches_the_gate(
    registry: ToolRegistry, clickup: StubClickUp
) -> None:
    agent, _ = agent_with(
        registry,
        [
            LLMResponse(text="", tool_calls=[call("get_my_tasks")]),
            LLMResponse(text="all read", tool_calls=[]),
        ],
    )
    assert agent.run("what's on?").pending_write is None
    assert clickup.comments_added == []


# -- tool execution -------------------------------------------------------


def test_an_unknown_tool_becomes_data_not_a_crash(registry: ToolRegistry) -> None:
    assert "unknown tool" in registry.execute("no_such_tool", {})


def test_bad_arguments_come_back_as_an_error_the_model_can_read(registry: ToolRegistry) -> None:
    assert "error" in json.loads(registry.execute("get_task_details", {"wrong": "arg"}))


def test_write_tools_are_the_only_ones_flagged(registry: ToolRegistry) -> None:
    assert {n for n in ("update_task_status", "add_comment") if registry.is_write(n)} == {
        "update_task_status",
        "add_comment",
    }
    assert not any(
        registry.is_write(n)
        for n in ("get_my_tasks", "search_tasks", "get_task_details", "get_task_comments")
    )
