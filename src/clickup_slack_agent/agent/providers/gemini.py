"""Gemini implementation of LLMProvider.

Translation notes: Gemini calls the assistant role "model", carries tool
results in a *user* turn, and identifies calls by function name rather than
by id — so ids are synthesised here and never leave this module.
"""

import logging
import random
import time
from collections.abc import Sequence

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from ..llm import LLMResponse, Message, ToolCall, ToolSpec, Usage

log = logging.getLogger(__name__)

# The free tier answers 503 under load and 429 over quota. Both are worth
# waiting out — the alternative is the agent dying mid-question.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 4


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, model: str = "gemini-3.5-flash") -> None:
        self.model = model
        self._client = genai.Client(api_key=api_key)

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
    ) -> LLMResponse:
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=t.name,
                            description=t.description,
                            # Raw JSON Schema, so tool specs stay provider-neutral.
                            parameters_json_schema=t.parameters,
                        )
                        for t in tools
                    ]
                )
            ]
            if tools
            else None,
        )

        contents = [_to_content(m) for m in messages]
        response = self._generate_with_retry(contents, config)
        return _from_response(response)

    def _generate_with_retry(
        self, contents: list[types.Content], config: types.GenerateContentConfig
    ) -> types.GenerateContentResponse:
        for attempt in range(MAX_ATTEMPTS):
            try:
                return self._client.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
            except genai_errors.APIError as exc:
                last = attempt == MAX_ATTEMPTS - 1
                if exc.code not in RETRY_STATUSES or last:
                    raise
                # Jitter, so a retry storm does not sync up across requests.
                delay = 2**attempt + random.uniform(0, 1)
                log.warning("gemini %s, retrying in %.1fs", exc.code, delay)
                time.sleep(delay)

        raise RuntimeError("unreachable")  # pragma: no cover


def _to_content(message: Message) -> types.Content:
    parts: list[types.Part] = []

    if message.text:
        parts.append(types.Part.from_text(text=message.text))

    for call in message.tool_calls:
        part = types.Part.from_function_call(name=call.name, args=call.arguments)
        # Gemini 3 rejects a replayed function call whose thought signature is
        # missing, so the opaque blob has to go back exactly as it came.
        part.thought_signature = call.meta.get("thought_signature")
        parts.append(part)

    for result in message.tool_results:
        parts.append(
            types.Part.from_function_response(
                name=result.name,
                # Gemini requires a dict here, not a bare string.
                response={"result": result.content},
            )
        )

    # Tool results are sent back as a user turn — Gemini has no "tool" role.
    role = "model" if message.role == "assistant" and not message.tool_results else "user"
    return types.Content(role=role, parts=parts)


def _from_response(response: types.GenerateContentResponse) -> LLMResponse:
    text_parts: list[str] = []
    calls: list[ToolCall] = []

    candidates = response.candidates or []
    if candidates and candidates[0].content and candidates[0].content.parts:
        for index, part in enumerate(candidates[0].content.parts):
            if part.function_call:
                calls.append(
                    ToolCall(
                        # Gemini gives no call id, so make a stable local one.
                        id=part.function_call.id or f"call_{index}",
                        name=part.function_call.name or "",
                        arguments=dict(part.function_call.args or {}),
                        meta={"thought_signature": part.thought_signature},
                    )
                )
            elif part.text:
                text_parts.append(part.text)

    meta = response.usage_metadata
    usage = Usage(
        input_tokens=getattr(meta, "prompt_token_count", 0) or 0,
        output_tokens=getattr(meta, "candidates_token_count", 0) or 0,
    )
    return LLMResponse(text="".join(text_parts).strip(), tool_calls=calls, usage=usage)
