"""Retry + circuit breaker for gateway calls. Small on purpose, so you can read it all.

Retry (idempotent reads only):
  * Retries: connection failures and 502/503/504. Those are cases where the
    request most likely never reached, or was never processed by, the backend.
  * Does NOT retry: read timeouts (the backend may still be working, and a
    retry doubles the user's wait inside an LLM turn), 4xx (retrying won't
    change the answer), and any POST (a write must go through an
    Idempotency-Key, see Phase 4).
  * Exponential backoff with full jitter, so N replicas don't retry in lockstep.

Circuit breaker (one per downstream API):
  CLOSED --N consecutive failures--> OPEN --cooldown--> HALF_OPEN --1 trial--> CLOSED/OPEN
  While OPEN, calls fail immediately ("fail fast") instead of piling up on a
  sick backend. State is per process: each replica learns backend health on its
  own. That's fine, and it doesn't make the server stateful toward callers.

Java/Spring equivalent: Resilience4j `@Retry` (maxAttempts, waitDuration,
exponential backoff + randomized wait, retryExceptions) and `@CircuitBreaker`
(failureRateThreshold / slidingWindowType=COUNT_BASED,
waitDurationInOpenState, permittedNumberOfCallsInHalfOpenState=1), plus
`@TimeLimiter` for the timeout we set on the HTTP client.
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

RETRYABLE_STATUS = frozenset({502, 503, 504})


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpen(Exception):  # noqa: N818
    pass


@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 5
    cooldown_s: float = 30.0
    clock: Callable[[], float] = time.monotonic
    state: BreakerState = BreakerState.CLOSED
    _failures: int = 0
    _opened_at: float = 0.0
    _trial_in_flight: bool = False

    def before_call(self) -> None:
        if self.state is BreakerState.OPEN:
            if self.clock() - self._opened_at < self.cooldown_s:
                raise CircuitOpen(self.name)
            self.state = BreakerState.HALF_OPEN
        if self.state is BreakerState.HALF_OPEN:
            if self._trial_in_flight:
                raise CircuitOpen(self.name)
            self._trial_in_flight = True

    def on_success(self) -> None:
        self.state, self._failures, self._trial_in_flight = BreakerState.CLOSED, 0, False

    def abandon(self) -> None:
        """The call ended without a verdict on backend health (e.g. it was cancelled).

        Frees the half-open trial slot WITHOUT changing state, so the next call
        becomes the trial. Without this, a cancelled trial would leave the
        breaker refusing every call forever.
        """
        self._trial_in_flight = False

    def on_failure(self) -> None:
        self._trial_in_flight = False
        self._failures += 1
        if self.state is BreakerState.HALF_OPEN or self._failures >= self.failure_threshold:
            self.state, self._opened_at = BreakerState.OPEN, self.clock()


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 2  # 1 try + 1 retry: enough for a blip, bounded for an LLM turn
    base_delay_s: float = 0.2
    max_delay_s: float = 1.0

    def delay(self, attempt: int) -> float:
        cap = min(self.max_delay_s, self.base_delay_s * 2**attempt)
        return random.uniform(0, cap)  # noqa: S311 - jitter, not crypto


async def with_retry[T](
    call: Callable[[], Awaitable[T]],
    is_retryable: Callable[[BaseException | T], bool],
    policy: RetryPolicy,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Run `call`, retrying while `is_retryable(result_or_exception)` says so."""
    for attempt in range(policy.max_attempts):
        last = attempt == policy.max_attempts - 1
        try:
            result = await call()
        except Exception as exc:
            if last or not is_retryable(exc):
                raise
        else:
            if last or not is_retryable(result):
                return result
        await sleep(policy.delay(attempt))
    raise AssertionError("unreachable")
