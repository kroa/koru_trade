"""API 유량 제한과 재시도.

한국투자증권 Open API 는 앱키 단위로 초당 호출 수를 제한한다
(실전 계좌가 모의 계좌보다 넉넉하다). 한도를 넘기면 오류가 나고,
계속 두들기면 앱키가 일시 차단될 수 있다.

이 모듈은 두 가지를 제공한다.

* :class:`RateLimiter` — 토큰 버킷. 호출 전에 :meth:`~RateLimiter.acquire` 를 부른다.
* :func:`retry_with_backoff` — 지수 백오프 + 지터. **주문 제출에는 쓰지 않는다.**
  주문은 멱등키로 보호하더라도 재시도 자체를 신중히 다뤄야 하므로,
  조회 계열에만 적용한다.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

__all__ = ["RateLimiter", "retry_with_backoff"]

T = TypeVar("T")


class RateLimiter:
    """토큰 버킷 방식 유량 제한기. 스레드 안전하다."""

    def __init__(self, max_calls: int, period_seconds: float = 1.0) -> None:
        """
        Args:
            max_calls: ``period_seconds`` 동안 허용할 최대 호출 수.
            period_seconds: 기준 시간(초).
        """
        if max_calls < 1:
            raise ValueError(f"max_calls 는 1 이상이어야 한다: {max_calls}")
        if period_seconds <= 0:
            raise ValueError(f"period_seconds 는 0보다 커야 한다: {period_seconds}")
        self._max = max_calls
        self._period = period_seconds
        self._lock = threading.Lock()
        self._timestamps: list[float] = []

    def acquire(self, *, timeout: float | None = None) -> None:
        """호출 슬롯을 확보한다. 필요하면 대기한다.

        Raises:
            TimeoutError: ``timeout`` 안에 슬롯을 얻지 못했을 때.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                cutoff = now - self._period
                self._timestamps = [t for t in self._timestamps if t > cutoff]
                if len(self._timestamps) < self._max:
                    self._timestamps.append(now)
                    return
                wait = self._timestamps[0] + self._period - now
            if deadline is not None and time.monotonic() + wait > deadline:
                raise TimeoutError(f"{timeout}초 안에 API 호출 슬롯을 얻지 못했다")
            time.sleep(max(0.001, min(wait, 1.0)))

    @property
    def current_usage(self) -> int:
        """현재 기간에 사용한 슬롯 수."""
        with self._lock:
            cutoff = time.monotonic() - self._period
            self._timestamps = [t for t in self._timestamps if t > cutoff]
            return len(self._timestamps)


def retry_with_backoff(
    fn: Callable[[], T],
    *,
    retries: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> T:
    """지수 백오프와 지터를 적용해 재시도한다.

    지터를 넣는 이유는 여러 프로세스가 동시에 실패했을 때
    똑같은 간격으로 재시도해 서버를 다시 무너뜨리는 것을 막기 위해서다.

    Args:
        fn: 인자 없는 호출 가능 객체.
        retries: 최초 시도 이후의 추가 시도 횟수.
        base_delay: 첫 대기 시간(초).
        max_delay: 대기 시간 상한(초).
        retry_on: 재시도할 예외 타입.
        sleep: 대기 함수. 테스트에서 주입한다.
        rng: 난수원. 테스트에서 주입해 결정론적으로 만든다.

    Raises:
        마지막 시도의 예외를 그대로 올린다.
    """
    if retries < 0:
        raise ValueError(f"retries 는 0 이상이어야 한다: {retries}")
    random_source = rng or random.SystemRandom()
    last: BaseException | None = None

    for attempt in range(retries + 1):
        try:
            return fn()
        except retry_on as exc:
            last = exc
            if attempt >= retries:
                break
            delay = min(base_delay * (2**attempt), max_delay)
            jitter = delay * random_source.uniform(0.0, 0.3)
            total = delay + jitter
            logger.warning(
                "호출 실패(%s). %.2f초 후 재시도 (%d/%d)",
                type(exc).__name__,
                total,
                attempt + 1,
                retries,
            )
            sleep(total)

    assert last is not None
    raise last
