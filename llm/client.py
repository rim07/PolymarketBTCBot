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


def parse_structured(
    model: str,
    system: str,
    user_content: str,
    output_format: Type[T],
    effort: str = "low",
    max_tokens: int = 2048,
    timeout_seconds: float = config.LLM_CALL_TIMEOUT_SECONDS,
) -> T:
    client = get_client().with_options(timeout=timeout_seconds)
    response = client.messages.parse(
        model=model,
        max_tokens=max_tokens,
        system=system,
        output_config={"effort": effort},
        messages=[{"role": "user", "content": user_content}],
        output_format=output_format,
    )
    return response.parsed_output
