"""대시보드가 켠 시점 시세에 멈춰 있지 않는지 검증.

`DashboardServer` 는 생성 시점의 봉을 `self._bars` 에 담아두고 그대로 그렸다.
TTL 캐시는 스냅샷만 다시 만들 뿐 시세는 새로 받지 않아서, 서버를 하루 켜 두면
어제 가격을 오늘 값인 것처럼 보여줬다. 죽은 화면과 멈춘 화면은 구분이 안 된다.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest
from tests.conftest import make_series

from koru_trade.config import StrategyConfig
from koru_trade.models import Bar
from koru_trade.web.server import DashboardServer

MARKER = 99.99
"""새 시세가 반영됐는지 확인하는 눈에 띄는 가격. 다른 곳에서 나올 수 없다."""


def _extended(bars: Sequence[Bar]) -> tuple[Bar, ...]:
    """마지막 봉 뒤에 종가 99.99 짜리 봉을 하나 붙인다."""
    import datetime as dt

    last = bars[-1]
    fresh = Bar(
        ts=last.ts + dt.timedelta(days=1),
        open=MARKER,
        high=MARKER,
        low=MARKER,
        close=MARKER,
        volume=last.volume,
        fx_rate=last.fx_rate,
    )
    return (*bars, fresh)


@pytest.fixture
def bars() -> tuple[Bar, ...]:
    return make_series(150, drift=0.001)


def _payload(server: DashboardServer) -> str:
    """스냅샷 JSON 을 새로 만들어 문자열로 돌려준다."""
    text = server._build()
    json.loads(text)  # 깨진 JSON 을 내보내면 화면이 통째로 빈다
    return text


class TestBarsProvider:
    def test_provider가_없으면_켤_때_받은_시세를_계속_쓴다(
        self, bars: tuple[Bar, ...], cfg: StrategyConfig
    ) -> None:
        with DashboardServer(bars, cfg) as server:
            assert str(MARKER) not in _payload(server)
            assert str(MARKER) not in _payload(server)

    def test_provider가_있으면_다시_만들_때_새_시세를_받는다(
        self, bars: tuple[Bar, ...], cfg: StrategyConfig
    ) -> None:
        feed = {"bars": bars}

        with DashboardServer(bars, cfg, bars_provider=lambda: feed["bars"]) as server:
            assert str(MARKER) not in _payload(server)
            feed["bars"] = _extended(bars)  # 장이 하루 더 지났다
            assert str(MARKER) in _payload(server)

    def test_시세를_못_받으면_직전_봉으로_그린다(
        self, bars: tuple[Bar, ...], cfg: StrategyConfig
    ) -> None:
        """네트워크가 끊겼다고 대시보드까지 죽으면 안 된다."""

        def broken() -> Sequence[Bar]:
            raise ConnectionError("시세 서버 연결 끊김")

        with DashboardServer(bars, cfg, bars_provider=broken) as server:
            text = _payload(server)
        assert text

    def test_빈_시세는_무시한다(self, bars: tuple[Bar, ...], cfg: StrategyConfig) -> None:
        """빈 목록을 그대로 받으면 스냅샷 생성이 터진다."""
        with DashboardServer(bars, cfg, bars_provider=lambda: ()) as server:
            assert _payload(server)

    def test_받은_시세가_다음_호출에도_유지된다(
        self, bars: tuple[Bar, ...], cfg: StrategyConfig
    ) -> None:
        """한 번 받아온 봉은 provider 가 실패해도 남아 있어야 한다."""
        calls = {"n": 0}

        def flaky() -> Sequence[Bar]:
            calls["n"] += 1
            if calls["n"] == 1:
                return _extended(bars)
            raise TimeoutError("일시적 장애")

        with DashboardServer(bars, cfg, bars_provider=flaky) as server:
            assert str(MARKER) in _payload(server)
            assert str(MARKER) in _payload(server)


def test_web_명령에_refresh_인자가_있다() -> None:
    """구현에서 배선이 빠지는 회귀를 막는다."""
    import inspect

    from koru_trade import cli

    parser_src = inspect.getsource(cli._build_parser)
    assert '"--refresh"' in parser_src
    web_src = inspect.getsource(cli._cmd_web)
    assert "bars_provider=" in web_src
