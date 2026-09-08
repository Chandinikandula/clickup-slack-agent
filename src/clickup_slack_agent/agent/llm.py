"""Provider-neutral types for the agent loop.

The loop is written against these, never against a vendor SDK, so switching
model provider is a config change rather than a rewrite — and so the same
eval suite can be run across providers to compare them.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class ToolCall:
    """A tool the model decided to call, with the arguments it chose."""

    id: str
    name: str
    arguments: dict[str, Any]
    # Opaque per-provider data that must survive the round trip — Gemini's
    # thought signatures, for instance. The loop never reads this; only the
    # provider that produced it does.
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    id: str
    name: str
    content: str


@dataclass
class Message:
    """One turn. Assistant turns may carry tool calls; user turns may carry
    the results of those calls, which is how every provider models this.
    """

    role: Literal["user", "assistant"]
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    usage: Usage = Usage()

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@dataclass(frozen=True)
class ToolSpec:
    """A tool as the model sees it — name, description, JSON Schema."""

    name: str
    description: str
    parameters: dict[str, Any]


class LLMProvider(Protocol):
    """What the agent loop needs from a model. Deliberately small."""

    name: str
    model: str

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
    ) -> LLMResponse: ...
