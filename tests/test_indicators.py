"""기술적 지표 검증.

지표는 순수 함수이므로 알려진 입력에 대한 알려진 출력으로 못 박을 수 있다.
"데이터가 부족하면 None" 규약이 깨지면 워밍업 구간에서 전략이 폭주한다.
"""

from __future__ import annotations

import math

import pytest
from tests.conftest import BASE_TS, make_bar, make_series

from koru_trade import indicators as ind
from koru_trade.models import Bar


class TestSmaEma:
    def test_sma는_단순평균이다(self) -> None:
        assert ind.sma([1, 2, 3, 4, 5], 5) == pytest.approx(3.0)
        assert ind.sma([1, 2, 3, 4, 5], 2) == pytest.approx(4.5)

    def test_데이터가_부족하면_None이다(self) -> None:
        assert ind.sma([1, 2], 5) is None
        assert ind.ema([1, 2], 5) is None

    def test_상수열의_ema는_그_상수다(self) -> None:
        assert ind.ema([7.0] * 30, 10) == pytest.approx(7.0)

    def test_ema는_최근값에_더_민감하다(self) -> None:
        """최근에 값이 튀면 EMA 가 SMA 보다 먼저 반응한다.

        (등차수열에서는 두 값이 수렴하므로 계단 변화로 시험한다.)
        """
        stepped = [10.0] * 25 + [20.0] * 3
        ema = ind.ema(stepped, 10)
        sma = ind.sma(stepped, 10)
        assert ema is not None and sma is not None
        assert ema > sma

    @pytest.mark.parametrize("fn", [ind.sma, ind.ema])
    def test_0이하_period는_거부된다(self, fn) -> None:
        with pytest.raises(ValueError, match="period"):
            fn([1, 2, 3], 0)


class TestRsi:
    def test_계속_오르면_100이다(self) -> None:
        assert ind.rsi([float(i) for i in range(1, 40)], 14) == pytest.approx(100.0)

    def test_계속_내리면_0에_가깝다(self) -> None:
        value = ind.rsi([float(i) for i in range(40, 1, -1)], 14)
        assert value is not None
        assert value < 1.0

    def test_변화가_없으면_100이다(self) -> None:
        """상승도 하락도 없으면 평균 하락이 0이라 100 을 반환한다(경계 규약)."""
        assert ind.rsi([5.0] * 30, 14) == pytest.approx(100.0)

    def test_데이터_부족시_None(self) -> None:
        assert ind.rsi([1.0] * 14, 14) is None

    def test_값은_항상_0과_100_사이다(self) -> None:
        bars = make_series(200, drift=0.001, amplitude=0.05)
        value = ind.rsi([b.close for b in bars], 14)
        assert value is not None
        assert 0.0 <= value <= 100.0


class TestAtr:
    def test_일정한_진폭이면_ATR이_그_진폭이다(self) -> None:
        bars = [
            Bar(BASE_TS.replace(day=1 + i), 20.0, 21.0, 19.0, 20.0, 1000, 1400.0) for i in range(20)
        ]
        assert ind.atr(bars, 14) == pytest.approx(2.0)

    def test_ATR비율은_종가로_나눈_값이다(self) -> None:
        bars = [
            Bar(BASE_TS.replace(day=1 + i), 20.0, 21.0, 19.0, 20.0, 1000, 1400.0) for i in range(20)
        ]
        assert ind.atr_pct(bars, 14) == pytest.approx(0.10)

    def test_데이터_부족시_None(self) -> None:
        assert ind.atr(make_series(10), 14) is None
        assert ind.atr_pct(make_series(10), 14) is None

    def test_true_range는_갭을_반영한다(self) -> None:
        bars = [
            Bar(BASE_TS, 20.0, 21.0, 19.0, 20.0, 1000, 1400.0),
            Bar(BASE_TS.replace(day=6), 30.0, 31.0, 29.0, 30.0, 1000, 1400.0),
        ]
        trs = ind.true_ranges(bars)
        assert trs[0] == pytest.approx(2.0)
        # 두 번째 봉은 전일 종가 20 대비 고가 31 -> TR 11
        assert trs[1] == pytest.approx(11.0)

    def test_변동성이_크면_ATR도_크다(self) -> None:
        calm = ind.atr_pct(make_series(60, amplitude=0.005), 14)
        wild = ind.atr_pct(make_series(60, amplitude=0.08), 14)
        assert calm is not None and wild is not None
        assert wild > calm


class TestBollinger:
    def test_상수열은_밴드가_붙는다(self) -> None:
        result = ind.bollinger([10.0] * 30, 20)
        assert result is not None
        lo, mid, hi = result
        assert lo == pytest.approx(mid) == pytest.approx(hi) == pytest.approx(10.0)

    def test_밴드_순서가_유지된다(self) -> None:
        result = ind.bollinger([float(i) for i in range(30)], 20)
        assert result is not None
        lo, mid, hi = result
        assert lo < mid < hi

    def test_데이터_부족시_None(self) -> None:
        assert ind.bollinger([1.0] * 10, 20) is None


class TestAdx:
    def test_강한_추세는_ADX가_높다(self) -> None:
        bars = make_series(120, drift=0.01)
        value = ind.adx(bars, 14)
        assert value is not None
        assert value > 30.0

    def test_데이터_부족시_None(self) -> None:
        assert ind.adx(make_series(20), 14) is None

    def test_값은_0과_100_사이다(self) -> None:
        for amp in (0.0, 0.02, 0.1):
            value = ind.adx(make_series(150, drift=0.002, amplitude=amp), 14)
            assert value is not None
            assert 0.0 <= value <= 100.0


class TestVolatility:
    def test_상수열은_변동성이_0이다(self) -> None:
        assert ind.realized_vol([10.0] * 40, 20) == pytest.approx(0.0)

    def test_데이터_부족시_None(self) -> None:
        assert ind.realized_vol([1.0] * 10, 20) is None

    def test_레버리지_감쇠는_항상_음수다(self) -> None:
        bars = make_series(60, amplitude=0.05)
        decay = ind.leverage_decay_estimate([b.close for b in bars], 3.0, 20)
        assert decay is not None
        assert decay < 0

    def test_변동성이_2배면_감쇠는_4배다(self) -> None:
        """감쇠는 변동성의 제곱에 비례한다. 3배 레버리지의 핵심 성질이다."""
        low = make_series(60, amplitude=0.02)
        high = make_series(60, amplitude=0.04)
        d_low = ind.leverage_decay_estimate([b.close for b in low], 3.0, 20)
        d_high = ind.leverage_decay_estimate([b.close for b in high], 3.0, 20)
        assert d_low is not None and d_high is not None
        assert d_high / d_low == pytest.approx(4.0, rel=0.15)

    def test_레버리지가_1이면_감쇠가_0이다(self) -> None:
        bars = make_series(60, amplitude=0.05)
        assert ind.leverage_decay_estimate([b.close for b in bars], 1.0, 20) == pytest.approx(0.0)


class TestMisc:
    def test_최대낙폭이_계산된다(self) -> None:
        assert ind.max_drawdown([100, 120, 60, 90]) == pytest.approx(-0.5)

    def test_빈_입력의_낙폭은_0이다(self) -> None:
        assert ind.max_drawdown([]) == 0.0

    def test_상승만_하면_낙폭이_0이다(self) -> None:
        assert ind.max_drawdown([1, 2, 3, 4]) == pytest.approx(0.0)

    def test_갭비율이_계산된다(self) -> None:
        bars = [make_bar(0, close=20.0), make_bar(1, close=22.0, open_=21.0)]
        assert ind.gap_pct(bars) == pytest.approx(0.05)

    def test_봉이_하나면_갭은_None이다(self) -> None:
        assert ind.gap_pct([make_bar(0)]) is None

    def test_연속하락일수가_계산된다(self) -> None:
        assert ind.consecutive_down_days([5, 4, 3, 2]) == 3
        assert ind.consecutive_down_days([2, 3, 4, 5]) == 0
        assert ind.consecutive_down_days([5, 4, 6, 5]) == 1

    def test_수익률_시계열은_1개_짧다(self) -> None:
        r = ind.returns([100.0, 110.0, 99.0])
        assert len(r) == 2
        assert r[0] == pytest.approx(0.10)
        assert r[1] == pytest.approx(-0.10)

    def test_0으로_나누기가_방어된다(self) -> None:
        assert ind.returns([0.0, 10.0]) == [0.0]


class TestRealData:
    """실데이터에서도 지표가 정상 범위 안에 있는지."""

    def test_실데이터_지표가_유한하다(self, real_bars: tuple[Bar, ...]) -> None:
        closes = [b.close for b in real_bars]
        for value in (
            ind.rsi(closes, 14),
            ind.atr(real_bars, 14),
            ind.atr_pct(real_bars, 14),
            ind.adx(real_bars, 14),
            ind.realized_vol(closes, 20),
        ):
            assert value is not None
            assert math.isfinite(value)

    def test_실데이터_ATR비율이_양수다(self, real_bars: tuple[Bar, ...]) -> None:
        value = ind.atr_pct(real_bars, 14)
        assert value is not None
        assert value > 0
