"""Retry mechanism with exponential backoff for the podcast automation system."""

from __future__ import annotations

import asyncio
import functools
from typing import Callable, Sequence, Type

from src.utils.logger import get_logger

logger = get_logger("retry")


def async_retry(
    max_attempts: int = 3,
    backoff_base: int = 2,
    retryable_exceptions: Sequence[Type[Exception]] = (Exception,),
    on_retry: Callable | None = None,
):
    """Decorator for async functions with exponential backoff retry.

    Args:
        max_attempts: Maximum number of retry attempts.
        backoff_base: Base for exponential backoff calculation.
        retryable_exceptions: Tuple of exception types that trigger a retry.
        on_retry: Optional callback invoked on each retry with (attempt, exception).
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except tuple(retryable_exceptions) as exc:
                    last_exception = exc
                    if attempt == max_attempts:
                        logger.error(
                            "Function '%s' failed after %d attempts: %s",
                            func.__name__,
                            max_attempts,
                            str(exc),
                        )
                        raise
                    wait_time = backoff_base ** attempt
                    logger.warning(
                        "Function '%s' attempt %d/%d failed: %s. "
                        "Retrying in %ds...",
                        func.__name__,
                        attempt,
                        max_attempts,
                        str(exc),
                        wait_time,
                    )
                    if on_retry:
                        on_retry(attempt, exc)
                    await asyncio.sleep(wait_time)
            raise last_exception  # type: ignore[misc]

        return wrapper

    return decorator
