"""The agent loop.

Ask the model, run whatever tools it asks for, feed the results back, repeat
until it answers. Writes are the exception: the loop stops and hands the
proposed change back for confirmation rather than executing it.

Written out rather than delegated to a framework, because the control flow
here — the write gate, the step ceiling, the trace — is the product.
"""

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from .llm import LLMProvider, Message, ToolCall, ToolResult, Usage
from .prompts import build_system_prompt
from .tools import ToolRegistry

log = logging.getLogger(__name__)

# Enough for search -> details -> comments with room to recover from a
# mistake; low enough that a confused model cannot spend your quota.
MAX_STEPS = 6


@dataclass
class Step:
    """One tool call, kept so evals can assert on what the agent chose."""

    tool: str
    arguments: dict
    result: str
    duration_ms: int


@dataclass
class AgentResult:
    text: str
    steps: list[Step] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    pending_write: ToolCall | None = None
    usage: Usage = Usage()

    @property
    def tools_used(self) -> list[str]:
        return [s.tool for s in self.steps]


class Agent:
    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        *,
        timezone: str,
        max_steps: int = MAX_STEPS,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.timezone = timezone
        self.max_steps = max_steps

    def run(self, question: str, history: Sequence[Message] = ()) -> AgentResult:
        tz = ZoneInfo(self.timezone)
        system = build_system_prompt(datetime.now(tz).date(), self.timezone)

        messages: list[Message] = [*history, Message(role="user", text=question)]
        steps: list[Step] = []
        total_in = total_out = 0

        for step_no in range(self.max_steps):
            response = self.provider.complete(
                system=system, messages=messages, tools=self.registry.specs
            )
            total_in += response.usage.input_tokens
            total_out += response.usage.output_tokens

            if not response.wants_tools:
                messages.append(Message(role="assistant", text=response.text))
                return AgentResult(
                    text=response.text,
                    steps=steps,
                    messages=messages,
                    usage=Usage(total_in, total_out),
                )

            # A write is a proposal, not an action. Stop here and let Slack ask.
            for call in response.tool_calls:
                if self.registry.is_write(call.name):
                    log.info("write proposed: %s %s", call.name, call.arguments)
                    return AgentResult(
                        text=response.text,
                        steps=steps,
                        messages=messages,
                        pending_write=call,
                        usage=Usage(total_in, total_out),
                    )

            messages.append(
                Message(role="assistant", text=response.text, tool_calls=response.tool_calls)
            )

            results: list[ToolResult] = []
            for call in response.tool_calls:
                started = time.monotonic()
                output = self.registry.execute(call.name, call.arguments)
                elapsed = int((time.monotonic() - started) * 1000)
                log.info("step %d: %s(%s) -> %dms", step_no, call.name, call.arguments, elapsed)
                steps.append(Step(call.name, call.arguments, output, elapsed))
                results.append(ToolResult(id=call.id, name=call.name, content=output))

            messages.append(Message(role="user", tool_results=results))

        # Out of steps. Say so rather than presenting a half-answer as complete.
        log.warning("hit step ceiling of %d", self.max_steps)
        return AgentResult(
            text=(
                "I looked at several things but could not settle on an answer. "
                "Try narrowing the question."
            ),
            steps=steps,
            messages=messages,
            usage=Usage(total_in, total_out),
        )

    def execute_confirmed_write(self, call: ToolCall) -> str:
        """Run a write the user approved in Slack.

        Kept separate from run() so a write can only ever happen on this path.
        """
        if not self.registry.is_write(call.name):
            return '{"error": "not a write tool"}'
        return self.registry.execute(call.name, call.arguments)
