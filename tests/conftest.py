"""공용 테스트 픽스처.

원칙: **테스트는 네트워크를 쓰지 않는다.** 실데이터가 필요하면
``tests/fixtures/koru_3y.csv`` 에 고정해 둔 스냅샷을 쓴다.
그래야 인터넷이 끊겨도, 시장이 움직여도 같은 결과가 나온다.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from koru_trade.config import RiskConfig, StrategyConfig
from koru_trade.models import Bar, Lot, Position
from koru_trade.pnl import CostModel, krw_cost
from koru_trade.risk import RiskState

FIXTURES = Path(__file__).parent / "fixtures"
BASE_TS = dt.datetime(2026, 1, 5)  # 월요일


@pytest.fixture
def cost() -> CostModel:
    """기본 비용 모델."""
    return CostModel()


@pytest.fixture
def zero_cost() -> CostModel:
    """비용이 전혀 없는 모델. 순수 수학 검증에 쓴다."""
    return CostModel(
        buy_fee_rate=0.0,
        sell_fee_rate=0.0,
        sec_fee_rate=0.0,
        finra_taf_per_share=0.0,
        fx_spread_buy=0.0,
        fx_spread_sell=0.0,
        slippage_rate=0.0,
    )


@pytest.fixture
def cfg() -> StrategyConfig:
    """기본 전략 설정."""
    return StrategyConfig()


@pytest.fixture
def loose_cfg(zero_cost: CostModel) -> StrategyConfig:
    """필터를 모두 열어 둔 설정. 매매 로직 자체를 시험할 때 쓴다."""
    return StrategyConfig(
        cost=zero_cost,
        max_atr_pct=0.99,
        min_rsi=0.0,
        max_rsi=100.0,
        min_adx=0.0,
        max_gap_pct=0.99,
        min_avg_dollar_volume=0.0,
        max_fx_decline=-0.99,
        risk=RiskConfig(
            max_position_krw=100_000_000.0,
            max_daily_notional_krw=100_000_000.0,
            daily_loss_limit_krw=100_000_000.0,
            max_consecutive_losses=999,
            max_trades_per_day=999,
            max_open_positions=1,
        ),
    )


@pytest.fixture
def fresh_risk() -> RiskState:
    """한도를 아무것도 쓰지 않은 리스크 상태."""
    return RiskState(trade_date=BASE_TS.date())


def make_bar(
    index: int = 0,
    *,
    close: float = 20.0,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    volume: float = 5_000_000.0,
    fx: float = 1_400.0,
    ts: dt.datetime | None = None,
) -> Bar:
    """봉 하나를 편하게 만든다. 지정하지 않은 값은 종가에서 파생한다."""
    o = close if open_ is None else open_
    return Bar(
        ts=ts or (BASE_TS + dt.timedelta(days=index)),
        open=o,
        high=high if high is not None else max(o, close) * 1.005,
        low=low if low is not None else min(o, close) * 0.995,
        close=close,
        volume=volume,
        fx_rate=fx,
    )


@pytest.fixture
def make_bar_factory() -> Callable[..., Bar]:
    return make_bar


def make_series(
    n: int,
    *,
    start: float = 20.0,
    drift: float = 0.0,
    amplitude: float = 0.0,
    fx_start: float = 1_400.0,
    fx_drift: float = 0.0,
    volume: float = 5_000_000.0,
) -> tuple[Bar, ...]:
    """결정론적 봉 시계열을 만든다.

    Args:
        n: 봉 개수.
        start: 시작 종가.
        drift: 봉당 복리 수익률(0.004 = 하루 +0.4%).
        amplitude: 사인파 진폭 비율. 변동성을 만들 때 쓴다.
        fx_start: 시작 환율.
        fx_drift: 봉당 환율 변화율.
        volume: 거래량.
    """
    bars: list[Bar] = []
    price = start
    fx = fx_start
    for i in range(n):
        wobble = 1.0 + amplitude * math.sin(i * 0.7)
        close = price * wobble
        prev_close = bars[-1].close if bars else close
        o = prev_close
        hi = max(o, close) * (1.0 + amplitude * 0.5 + 0.004)
        lo = min(o, close) * (1.0 - amplitude * 0.5 - 0.004)
        bars.append(
            Bar(
                ts=BASE_TS + dt.timedelta(days=i),
                open=o,
                high=hi,
                low=lo,
                close=close,
                volume=volume,
                fx_rate=fx,
            )
        )
        price *= 1.0 + drift
        fx *= 1.0 + fx_drift
    return tuple(bars)


@pytest.fixture
def make_series_factory() -> Callable[..., Sequence[Bar]]:
    return make_series


def open_position(
    *,
    qty: int = 100,
    price: float = 20.0,
    fx: float = 1_400.0,
    cost: CostModel | None = None,
    atr: float = 1.0,
    plan: tuple[int, ...] = (40, 35, 25),
    ts: dt.datetime | None = None,
    symbol: str = "KORU",
) -> Position:
    """1차 매수만 체결된 포지션을 만든다."""
    c = cost or CostModel()
    return Position(
        symbol,
        entry_atr_usd=atr,
        planned_tranche_qty=plan,
    ).with_lot(
        Lot(
            lot_id="T0",
            qty=qty,
            price_usd=price,
            fx_rate=fx,
            cost_krw=krw_cost(qty, price, fx, c),
            opened_at=ts or BASE_TS,
            tranche_index=0,
        )
    )


@pytest.fixture
def open_position_factory() -> Callable[..., Position]:
    return open_position


@pytest.fixture(scope="session")
def real_bars() -> tuple[Bar, ...]:
    """실제 KORU 3년 스냅샷. 네트워크를 쓰지 않는다."""
    from koru_trade.data import load_bars_from_csv

    path = FIXTURES / "koru_3y.csv"
    if not path.exists():
        pytest.skip(f"실데이터 픽스처가 없다: {path}")
    return load_bars_from_csv(path)
