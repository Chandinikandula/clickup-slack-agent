"""Provider selection.

Imports are lazy so the app runs with only the SDK for the provider actually
in use — and so a missing optional dependency fails with a clear message
rather than at import time.
"""

from ..llm import LLMProvider


def build_provider(name: str, api_key: str, model: str) -> LLMProvider:
    if not api_key:
        raise ValueError(f"no API key configured for provider {name!r}")

    if name == "gemini":
        from .gemini import GeminiProvider

        return GeminiProvider(api_key, model)

    if name == "anthropic":
        from .anthropic import AnthropicProvider

        return AnthropicProvider(api_key, model)

    if name == "groq":
        from .groq import GroqProvider

        return GroqProvider(api_key, model)

    raise ValueError(f"unknown provider: {name}")
