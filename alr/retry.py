"""Retry + circuit breaker — addresses "no retries, no circuit breaker"
gap. Stdlib-only (time.sleep / monotonic), works for both sync and async
callables.

Circuit breaker: after `failure_threshold` consecutive failures, the
breaker OPENS and fails fast (no more real calls) for `reset_after_s`
seconds, then allows one trial call (HALF_OPEN) before fully closing
again. This stops one dead Ollama/Anthropic endpoint from making every
request in a traffic burst hang for the full timeout.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Optional, Tuple, Type, TypeVar

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    pass


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    reset_after_s: float = 30.0
    _state: CircuitState = field(default=CircuitState.CLOSED)
    _consecutive_failures: int = field(default=0)
    _opened_at: Optional[float] = field(default=None)

    def before_call(self) -> None:
        if self._state == CircuitState.OPEN:
            if self._opened_at is not None and (time.monotonic() - self._opened_at) >= self.reset_after_s:
                self._state = CircuitState.HALF_OPEN
            else:
                raise CircuitOpenError("Circuit breaker is open — failing fast without a real call.")

    def on_success(self) -> None:
        self._consecutive_failures = 0
        self._state = CircuitState.CLOSED
        self._opened_at = None

    def on_failure(self) -> None:
        self._consecutive_failures += 1
        if self._state == CircuitState.HALF_OPEN or self._consecutive_failures >= self.failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()

    @property
    def state(self) -> CircuitState:
        return self._state


def retry_sync(
    fn: Callable[[], T],
    retryable_exceptions: Tuple[Type[BaseException], ...],
    max_attempts: int = 3,
    base_delay_s: float = 0.5,
    breaker: Optional[CircuitBreaker] = None,
) -> T:
    last_exc: Optional[BaseException] = None
    for attempt in range(1, max_attempts + 1):
        if breaker is not None:
            breaker.before_call()
        try:
            result = fn()
            if breaker is not None:
                breaker.on_success()
            return result
        except retryable_exceptions as e:
            last_exc = e
            if breaker is not None:
                breaker.on_failure()
            if attempt < max_attempts:
                time.sleep(base_delay_s * (2 ** (attempt - 1)))
    assert last_exc is not None
    raise last_exc


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    retryable_exceptions: Tuple[Type[BaseException], ...],
    max_attempts: int = 3,
    base_delay_s: float = 0.5,
    breaker: Optional[CircuitBreaker] = None,
) -> T:
    last_exc: Optional[BaseException] = None
    for attempt in range(1, max_attempts + 1):
        if breaker is not None:
            breaker.before_call()
        try:
            result = await fn()
            if breaker is not None:
                breaker.on_success()
            return result
        except retryable_exceptions as e:
            last_exc = e
            if breaker is not None:
                breaker.on_failure()
            if attempt < max_attempts:
                await asyncio.sleep(base_delay_s * (2 ** (attempt - 1)))
    assert last_exc is not None
    raise last_exc
