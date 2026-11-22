"""Failure taxonomy for agent runs: what failed, independent of where."""

import openai
import psycopg
from langchain_core.exceptions import OutputParserException
from langgraph.errors import GraphRecursionError
from pydantic import ValidationError

# Ordered from most to least specific; the first match wins.
_CLASSES: list[tuple[str, type | tuple[type, ...]]] = [
    ("timeout", openai.APITimeoutError),
    ("connect", openai.APIConnectionError),
    ("auth", (openai.AuthenticationError, openai.PermissionDeniedError)),
    ("rate_limit", openai.RateLimitError),
    ("bad_request", (openai.BadRequestError, openai.UnprocessableEntityError, openai.NotFoundError)),
    ("upstream_5xx", openai.InternalServerError),
    ("upstream_other", openai.APIStatusError),
    ("db", psycopg.Error),
    ("parse", (OutputParserException, ValidationError)),
    ("recursion_limit", GraphRecursionError),
]


# Exception names recorded before ValidationError was classified as a parse failure.
_PARSE_EXCEPTIONS = {"ValidationError", "OutputParserException"}


def failure_type(error: dict) -> str:
    """Failure class of a recorded error, normalized across harness versions."""
    return "parse" if error.get("exception") in _PARSE_EXCEPTIONS else error["type"]


def classify(exc: BaseException) -> dict:
    """Return the failure class, HTTP status (if any) and a short message."""
    kind = "other"
    for name, types in _CLASSES:
        if isinstance(exc, types):
            kind = name
            break
    else:
        if isinstance(exc, ValueError) and "no parsable" in str(exc):
            kind = "parse"
        elif isinstance(exc, TimeoutError):
            kind = "timeout"
    return {
        "type": kind,
        "status": getattr(exc, "status_code", None),
        "exception": type(exc).__name__,
        "message": str(exc)[:300],
    }
