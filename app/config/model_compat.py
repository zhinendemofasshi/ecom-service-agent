"""OpenAI model-specific Chat Completions compatibility options."""

from __future__ import annotations


def chat_completion_options(model: str) -> dict[str, str]:
    """Return the smallest compatibility override required by ``model``.

    GPT-5.6 Luna supports Chat Completions function tools only with reasoning
    disabled. The Responses API supports reasoning + tools, but this project
    intentionally keeps its existing Chat Completions execution path for a
    controlled model-only evaluation.
    """
    if model == "gpt-5.6-luna":
        return {"reasoning_effort": "none"}
    return {}
