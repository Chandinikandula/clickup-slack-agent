"""Provider selection.

Imports are lazy so the app only loads the SDK for the provider actually in
use. Only Gemini ships today; the other names are accepted so that adding
one is a new module plus a branch, and nothing else.
"""

from ..llm import LLMProvider


def build_provider(name: str, api_key: str, model: str) -> LLMProvider:
    if not api_key:
        raise ValueError(f"no API key configured for provider {name!r}")

    if name == "gemini":
        from .gemini import GeminiProvider

        return GeminiProvider(api_key, model)

    if name in {"anthropic", "groq"}:
        # Accepted by config but not written yet — say so plainly rather
        # than surfacing a ModuleNotFoundError from an import three frames up.
        raise NotImplementedError(
            f"the {name} provider is not implemented yet. Write "
            f"agent/providers/{name}.py with a `complete` method matching "
            "LLMProvider, then add a branch here. See CODE_GUIDE.md."
        )

    raise ValueError(f"unknown provider: {name}")
