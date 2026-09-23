"""추세 꺾임 청산 검증.

본전 스톱은 익절 1단(+5%) 이후, 트레일링은 2단(+7%) 이후에만 켜진다. 그래서
첫 문턱을 못 찍고 돌아선 매매는 아무 보호도 못 받고 타임 스톱까지 끌려간다.
이 규칙이 그 구간을 메운다.

핵심 계약 두 가지를 못 박는다.

1. **이익일 때만 나온다.** 손실 구간은 손절선의 일이다. 추세가 꺾였다고
   손실을 확정하면 정상 노이즈에 매번 털린다.
2. **여기서 말하는 이익은 매도 비용을 뺀 뒤의 이익이다.**
   ``position_krw_return`` 이 ``krw_proceeds`` 를 쓰므로 매도수수료·SEC·
   주당 정액 TAF·매도 환전 스프레드가 전부 차감된 값이다. 이게 깨지면
   "이익 보전" 이라면서 손실을 확정하는 장치가 된다.
"""

from __future__ import annotations

import dataclasses as dc

import pytest
from tests.conftest import BASE_TS, make_bar, make_series, open_position

from koru_trade.config import StrategyConfig
from koru_trade.models import Action, Bar, ExitReason, Position
from koru_trade.pnl import CostModel, position_krw_return
from koru_trade.risk import RiskState
from koru_trade.strategy import decide

ENTRY = 20.0
FX = 1_400.0


def _bars_then(last_close: float, *, n: int = 60, level: float = ENTRY) -> tuple[Bar, ...]:
    """``level`` 부근에서 평평하다가 마지막 봉만 ``last_close`` 인 시계열.

    평평하면 EMA 가 ``level`` 에 수렴하므로, 마지막 종가를 그 위아래로 놓는 것만으로
    "추세가 꺾였다/아니다" 를 정확히 만들 수 있다.
    """
    flat = [make_bar(i, close=level, fx=FX) for i in range(n - 1)]
    return (*flat, make_bar(n - 1, close=last_close, fx=FX))


def _cfg(floor: float | None, **kw: object) -> StrategyConfig:
    return dc.replace(StrategyConfig(trend_break_min_krw_return=floor), **kw)  # type: ignore[arg-type]


def _pos(price: float, *, n: int = 60, **kw: object) -> Position:
    """마지막 봉 직전에 연 포지션.

    개시일을 시계열 앞쪽에 두면 타임 스톱이 먼저 걸려 검사하려던 분기에
    도달하지 못한다.
    """
    import datetime as _dt

    return open_position(price=price, fx=FX, ts=BASE_TS + _dt.timedelta(days=n - 2), **kw)  # type: ignore[arg-type]


def _decide(bars: tuple[Bar, ...], pos: Position, cfg: StrategyConfig):
    return decide(bars, pos, RiskState(trade_date=BASE_TS.date()), cfg)


class TestFiresOnlyWhenProfitable:
    def test_이익_중_추세가_꺾이면_청산한다(self) -> None:
        # EMA 는 20 부근, 진입가는 18 -> 종가 19.4 면 이익이면서 EMA 아래다.
        bars = _bars_then(19.4)
        pos = _pos(18.0)
        d = _decide(bars, pos, _cfg(0.0))
        assert d.action is Action.EXIT
        assert d.reason is ExitReason.TREND_BREAK
        assert d.qty == pos.qty

    def test_손실_중에는_추세가_꺾여도_안_판다(self) -> None:
        """손실 확정은 손절선의 일이다. 여기서 털면 노이즈에 다 잘린다."""
        bars = _bars_then(19.4)
        pos = _pos(ENTRY)  # 20 에 사서 19.4 -> 손실
        assert position_krw_return(pos, 19.4, FX, CostModel()) < 0
        d = _decide(bars, pos, _cfg(0.0))
        assert d.reason is not ExitReason.TREND_BREAK

    def test_추세가_살아있으면_이익이어도_안_판다(self) -> None:
        bars = _bars_then(20.6)  # EMA 위
        pos = _pos(18.0)
        d = _decide(bars, pos, _cfg(0.0))
        assert d.reason is not ExitReason.TREND_BREAK

    def test_문턱_아래_이익이면_안_판다(self) -> None:
        bars = _bars_then(19.4)
        pos = _pos(19.0)  # 이익이지만 아주 얇다
        r = position_krw_return(pos, 19.4, FX, CostModel())
        assert 0 < r < 0.05, f"얇은 이익이어야 이 검사가 의미가 있다: {r:.2%}"
        d = _decide(bars, pos, _cfg(r + 0.01))  # 실제 수익률보다 높은 문턱
        assert d.reason is not ExitReason.TREND_BREAK

    def test_기본값은_꺼짐이다(self) -> None:
        """기존 사용자의 동작을 말없이 바꾸지 않는다."""
        assert StrategyConfig().trend_break_min_krw_return is None
        bars = _bars_then(19.4)
        pos = _pos(18.0)
        assert _decide(bars, pos, _cfg(None)).reason is not ExitReason.TREND_BREAK


class TestProfitIsNetOfSellCosts:
    """'이익' 의 정의가 매도 비용을 뺀 뒤인지 확인한다."""

    def test_매도비용을_빼면_손실인_가격에서는_안_판다(self) -> None:
        """총비용 왕복만큼만 오른 자리. 명목은 플러스지만 팔면 마이너스다."""
        cost = CostModel()
        pos = _pos(18.0, cost=cost)
        # 원화 수익률이 0 이 되는 가격을 이분법으로 찾는다.
        lo, hi = 18.0, 19.0
        for _ in range(60):
            mid = (lo + hi) / 2
            if position_krw_return(pos, mid, FX, cost) < 0:
                lo = mid
            else:
                hi = mid
        breakeven = hi
        just_below = breakeven * 0.999
        assert position_krw_return(pos, just_below, FX, cost) < 0

        bars = _bars_then(just_below, level=breakeven * 1.03)
        d = _decide(bars, pos, _cfg(0.0))
        assert d.reason is not ExitReason.TREND_BREAK, (
            "매도 비용을 빼면 손실인데 '이익 보전' 이라며 청산했다"
        )

    def test_문턱을_올리면_그만큼_여유를_요구한다(self) -> None:
        cost = CostModel()
        pos = _pos(19.0, cost=cost)
        bars = _bars_then(19.4)
        r = position_krw_return(pos, 19.4, FX, cost)
        assert _decide(bars, pos, _cfg(max(0.0, r - 0.001))).reason is ExitReason.TREND_BREAK
        assert _decide(bars, pos, _cfg(r + 0.001)).reason is not ExitReason.TREND_BREAK


class TestPriority:
    def test_하드스톱이_추세꺾임보다_먼저다(self) -> None:
        """둘 다 해당하면 손실 차단이 우선이어야 한다."""
        bars = _bars_then(10.0)
        pos = _pos(ENTRY)
        d = _decide(bars, pos, _cfg(0.0))
        assert d.reason in {ExitReason.HARD_STOP_USD, ExitReason.HARD_STOP_KRW}

    def test_익절_계단보다_추세꺾임이_먼저다(self) -> None:
        """둘 다 해당하면 전량 청산이 이긴다.

        추세가 꺾인 마당에 잔량을 남겨 다음 계단을 기다리는 것은 이 규칙의
        목적과 어긋난다. 계단은 추세가 살아 있을 때 나눠 파는 장치다.
        """
        bars = _bars_then(19.4, level=20.0)
        pos = _pos(14.0)  # 충분히 싸게 사서 +5% 훌쩍 넘김
        r = position_krw_return(pos, 19.4, FX, CostModel())
        assert r >= StrategyConfig().take_profit[0].krw_return

        assert _decide(bars, pos, _cfg(None)).reason is ExitReason.TAKE_PROFIT_LADDER
        d = _decide(bars, pos, _cfg(0.0))
        assert d.reason is ExitReason.TREND_BREAK
        assert d.qty == pos.qty, "잔량까지 전부 판다"


class TestConfigValidation:
    def test_음수는_거부한다(self) -> None:
        with pytest.raises(ValueError, match="0 이상"):
            StrategyConfig(trend_break_min_krw_return=-0.01)

    def test_1_이상은_거부한다(self) -> None:
        with pytest.raises(ValueError, match="1 미만"):
            StrategyConfig(trend_break_min_krw_return=1.0)

    def test_직렬화에_실린다(self) -> None:
        d = StrategyConfig(trend_break_min_krw_return=0.003).to_dict()
        assert d["trend_break_min_krw_return"] == pytest.approx(0.003)

    def test_영은_허용한다(self) -> None:
        assert StrategyConfig(trend_break_min_krw_return=0.0).trend_break_min_krw_return == 0.0


class TestLabels:
    def test_사유_라벨이_등록되어_있다(self) -> None:
        """빠지면 알림과 리포트에 영문 원문이 그대로 나간다."""
        from koru_trade.backtest.metrics import _REASON_KO
        from koru_trade.notify.format import _EXIT_KO

        assert ExitReason.TREND_BREAK in _EXIT_KO
        assert ExitReason.TREND_BREAK.value in _REASON_KO


class TestBacktestIntegration:
    def test_켜도_백테스트가_돈다(self, real_bars: tuple[Bar, ...]) -> None:
        from koru_trade.backtest import run_backtest

        cfg = StrategyConfig(trend_break_min_krw_return=0.003)
        res = run_backtest(real_bars, cfg)
        assert res.trades

    def test_짧은_시계열에서도_터지지_않는다(self) -> None:
        """EMA 가 아직 없을 때 None 을 그대로 비교하면 TypeError 가 난다."""
        bars = make_series(3)
        pos = open_position(price=1.0, fx=FX, ts=BASE_TS)
        assert _decide(bars, pos, _cfg(0.0)) is not None
