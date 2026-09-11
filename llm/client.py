"""Thin Anthropic API wrapper. Structured output via client.messages.parse()
(Pydantic output_format) — no manual JSON parsing/repair needed."""
from typing import Type, TypeVar

import anthropic
from pydantic import BaseModel

import config

T = TypeVar("T", bound=BaseModel)

_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set in .env")
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


class PermanentLLMError(RuntimeError):
    """A failure that will recur identically on every subsequent call — a bad
    request, a rejected schema, a dead API key. Distinguished from transient
    failures (timeout, rate limit, 5xx) because the caller should shout about
    these: a permanent error silently logged as a warning means the desk stops
    trading indefinitely while looking healthy."""


def parse_structured(
    model: str,
    system: str,
    user_content: str,
    output_format: Type[T],
    effort: str = "low",
    max_tokens: int = 2048,
    timeout_seconds: float = config.LLM_CALL_TIMEOUT_SECONDS,
    max_retries: int = 0,
) -> T:
    # max_retries=0 by default: inside a 5-minute window the SDK's default of 2
    # retries turns one slow call into 3x timeout_seconds of dead time, during
    # which the price that produced the edge has moved and risk_manager will
    # reject the fill anyway. The tick loop is the retry.
    client = get_client().with_options(timeout=timeout_seconds, max_retries=max_retries)
    try:
        response = client.messages.parse(
            model=model,
            max_tokens=max_tokens,
            system=system,
            # Explicit rather than relying on the default. Thinking tokens are
            # drawn from max_tokens, so this is load-bearing for the budget
            # below — and "omit the param" means adaptive on some models and
            # no thinking at all on others (Opus 4.8/4.7), so spelling it out
            # keeps behaviour stable if HEAD_TRADER_MODEL is ever changed.
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            messages=[{"role": "user", "content": user_content}],
            output_format=output_format,
        )
    except (
        anthropic.BadRequestError,
        anthropic.AuthenticationError,
        anthropic.PermissionDeniedError,
        anthropic.NotFoundError,
    ) as e:
        raise PermanentLLMError(f"{type(e).__name__}: {e}") from e
    if response.parsed_output is None:
        # response.parsed_output scans response.content for a text block and
        # returns None if there isn't one — happens when max_tokens is
        # exhausted by (adaptive, on-by-default on Opus-tier models) thinking
        # before any text block is produced. Surface stop_reason/refusal
        # detail here rather than let this crash downstream as a confusing
        # AttributeError on whatever the caller does with the result.
        detail = f"stop_reason={response.stop_reason}"
        if response.stop_reason == "refusal" and response.stop_details:
            detail += f" category={response.stop_details.category} explanation={response.stop_details.explanation}"
        raise RuntimeError(f"No structured output returned ({detail}) — consider raising max_tokens")
    return response.parsed_output
