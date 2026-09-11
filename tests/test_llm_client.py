"""Confirms parse_structured separates permanent API failures from transient
ones. This matters operationally, not cosmetically: a permanent failure that
looks transient means the desk finds edges forever and never trades one, with
only a WARNING in the log to explain it.
Run with: python -m pytest tests/test_llm_client.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic
import httpx2
import pytest

import llm.client as llm_client
from llm.schemas import TradeDecision


def _fake_client_raising(exc: Exception):
    class _Messages:
        def parse(self, **_kwargs):
            raise exc

    class _Client:
        messages = _Messages()

        def with_options(self, **_kwargs):
            return self

    return _Client()


def _status_error(cls, status_code: int):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status_code, request=request)
    return cls("boom", response=response, body=None)


def _call() -> TradeDecision:
    return llm_client.parse_structured(
        model="claude-sonnet-5", system="s", user_content="u", output_format=TradeDecision,
    )


@pytest.mark.parametrize(
    "cls,status",
    [
        (anthropic.BadRequestError, 400),
        (anthropic.AuthenticationError, 401),
        (anthropic.PermissionDeniedError, 403),
        (anthropic.NotFoundError, 404),
    ],
)
def test_permanent_errors_are_flagged_permanent(monkeypatch, cls, status):
    monkeypatch.setattr(
        llm_client, "get_client", lambda: _fake_client_raising(_status_error(cls, status))
    )
    with pytest.raises(llm_client.PermanentLLMError):
        _call()


def test_transient_errors_are_not_flagged_permanent(monkeypatch):
    # A timeout is the common case and must stay transient — the next tick
    # (~18s later) retries it naturally.
    exc = anthropic.APITimeoutError(
        request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    monkeypatch.setattr(llm_client, "get_client", lambda: _fake_client_raising(exc))
    with pytest.raises(anthropic.APITimeoutError):
        _call()


def test_rate_limit_is_not_flagged_permanent(monkeypatch):
    exc = _status_error(anthropic.RateLimitError, 429)
    monkeypatch.setattr(llm_client, "get_client", lambda: _fake_client_raising(exc))
    with pytest.raises(anthropic.RateLimitError):
        _call()
