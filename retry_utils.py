"""Shared retry helpers for Canvas / Gemini / Google / Discord boundaries."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _status_code(exc: BaseException) -> int | None:
    """Best-effort extract of an HTTP status from common client exceptions."""
    resp = getattr(exc, "resp", None) or getattr(exc, "response", None)
    if resp is not None:
        code = getattr(resp, "status", None) or getattr(resp, "status_code", None)
        if code is not None:
            return int(code)
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if code is not None:
        try:
            return int(code)
        except (TypeError, ValueError):
            return None
    return None


def is_retryable(exc: BaseException) -> bool:
    code = _status_code(exc)
    if code in RETRYABLE_STATUS:
        return True
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "timeout" in name or "timed out" in text:
        return True
    if "connection" in name or "temporarily" in text:
        return True
    if "429" in text or "rate limit" in text or "quota" in text:
        return True
    if any(s in text for s in ("500", "502", "503", "504")):
        return True
    return False


def with_retries(
    *,
    max_attempts: int = 5,
    base_delay: float = 0.8,
    max_delay: float = 30.0,
    retry_on: Callable[[BaseException], bool] | None = None,
    label: str = "api_call",
) -> Callable[[F], F]:
    """
    Exponential backoff + jitter for transient API failures.

    Retries on 429/5xx and common network errors by default.
    """

    predicate = retry_on or is_retryable

    def decorator(fn: F) -> F:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            attempt = 0
            while True:
                attempt += 1
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 — boundary retry
                    if attempt >= max_attempts or not predicate(exc):
                        raise
                    delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                    delay *= 0.5 + random.random()  # full-jitter-ish
                    logger.warning(
                        "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                        label,
                        attempt,
                        max_attempts,
                        exc,
                        delay,
                    )
                    time.sleep(delay)

        return wrapper  # type: ignore[return-value]

    return decorator
