"""Show a model its message history through a per-call projection.

Shared by the projections that keep history a model would reject away from it
(``text_only_input``, ``signed_reasoning_input``). The wrapper never touches the
message list it is handed: that list is aliased across every agent serving the
session (#741), so each projection returns a new list, or the same one when it
has nothing to change.
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional

Projection = Callable[[List[Any]], List[Any]]


def project_model_history(model: Any, project: Projection) -> Any:
    """Make ``model`` see its message history through ``project``.

    Wraps the instance's ``stream`` (the request) and ``count_tokens`` (Strands'
    pre-call estimate, which should count what is actually sent), plus
    ``native_count_tokens`` where the model has one (the context-attribution
    hook's background count). The wrappers are set on the instance rather than
    a subclass because the OpenAI-compatible models come from shared builders,
    and an instance attribute keeps every ``isinstance`` check on the model
    (``BedrockModel`` included) true. Projections compose: wrapping twice
    applies both.
    """
    stream = model.stream
    count_tokens = model.count_tokens
    native_count_tokens = getattr(model, "native_count_tokens", None)

    def projected_stream(messages: List[Any], *args: Any, **kwargs: Any) -> Any:
        return stream(project(messages), *args, **kwargs)

    async def projected_count_tokens(messages: List[Any], *args: Any, **kwargs: Any) -> int:
        return await count_tokens(project(messages), *args, **kwargs)

    model.stream = projected_stream
    model.count_tokens = projected_count_tokens
    if native_count_tokens is not None:

        def projected_native_count_tokens(messages: List[Any], *args: Any, **kwargs: Any) -> Optional[int]:
            return native_count_tokens(project(messages), *args, **kwargs)

        model.native_count_tokens = projected_native_count_tokens
    return model
