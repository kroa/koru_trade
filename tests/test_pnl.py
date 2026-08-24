"""원화 손익 엔진 검증.

여기서 지키는 불변식이 깨지면 익절/손절 판정 전체가 틀린다.
사용자 요구사항("환율로 손해보지 않도록 원화 5~20% 익절")이 성립하는 근거가 이 파일이다.
"""

from __future__ import annotations

import math

import pytest
from tests.conftest import BASE_TS

from koru_trade.models import Lot, Position
from koru_trade.pnl import (
    CostModel,
    FxMode,
    breakeven_sell_price_usd,
    buy_fx_rate,
    cost_factor,
    decompose,
    krw_cost,
    krw_proceeds,
    krw_return,
    position_krw_return,
    position_required_price_usd,
    required_sell_price_usd,
    sell_fx_rate,
)


class TestCostModel:
    def test_기본값은_왕복비용이_1퍼센트_미만이다(self, cost: CostModel) -> None:
        assert 0.0 < cost.round_trip_drag < 0.01

    def test_비용계수는_항상_1보다_작다(self, cost: CostModel) -> None:
        assert cost_factor(cost) < 1.0

    def test_비용이_0이면_계수가_정확히_1이다(self, zero_cost: CostModel) -> None:
        assert cost_factor(zero_cost) == pytest.approx(1.0)
        assert zero_cost.round_trip_drag == pytest.approx(0.0)

    def test_HOLD_USD_모드는_환전스프레드를_적용하지_않는다(self) -> None:
        convert = CostModel(fx_mode=FxMode.CONVERT)
        hold = CostModel(fx_mode=FxMode.HOLD_USD)
        assert convert.effective_buy_spread > 0
        assert hold.effective_buy_spread == 0.0
        assert hold.round_trip_drag < convert.round_trip_drag

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("buy_fee_rate", -0.01),
            ("sell_fee_rate", 1.0),
            ("fx_spread_buy", 1.5),
            ("slippage_rate", -0.001),
        ],
    )
    def test_잘못된_요율은_생성_시점에_거부된다(self, field: str, value: float) -> None:
        with pytest.raises(ValueError, match="0 이상 1 미만"):
            CostModel(**{field: value})

    def test_음수_TAF는_거부된다(self) -> None:
        with pytest.raises(ValueError, match="음수일 수 없다"):
            CostModel(finra_taf_per_share=-1.0)


class TestFxRates:
    def test_매수환율은_기준율보다_비싸고_매도환율은_싸다(self, cost: CostModel) -> None:
        mid = 1400.0
        assert buy_fx_rate(mid, cost) > mid
        assert sell_fx_rate(mid, cost) < mid

    def test_스프레드가_0이면_양쪽_모두_기준율이다(self, zero_cost: CostModel) -> None:
        assert buy_fx_rate(1400.0, zero_cost) == 1400.0
        assert sell_fx_rate(1400.0, zero_cost) == 1400.0


class TestKrwReturn:
    def test_비용이_없고_가격_환율이_그대로면_수익률은_0이다(self, zero_cost: CostModel) -> None:
        r = krw_return(100, 20.0, 1400.0, 20.0, 1400.0, zero_cost)
        assert r == pytest.approx(0.0, abs=1e-12)

    def test_비용이_있으면_같은_가격에서_반드시_손실이다(self, cost: CostModel) -> None:
        r = krw_return(100, 20.0, 1400.0, 20.0, 1400.0, cost)
        assert r < 0
        # round_trip_drag 는 TAF 를 뺀 근사, round_trip_drag_at 은 정확한 값이다.
        assert r == pytest.approx(-cost.round_trip_drag_at(20.0), rel=1e-9)
        assert r == pytest.approx(-cost.round_trip_drag, abs=1e-4)

    def test_주가_10퍼센트_상승은_비용_없으면_원화_10퍼센트다(self, zero_cost: CostModel) -> None:
        r = krw_return(100, 20.0, 1400.0, 22.0, 1400.0, zero_cost)
        assert r == pytest.approx(0.10)

    def test_환율_10퍼센트_하락은_주가가_그대로여도_원화_손실이다(
        self, zero_cost: CostModel
    ) -> None:
        r = krw_return(100, 20.0, 1400.0, 20.0, 1260.0, zero_cost)
        assert r == pytest.approx(-0.10)

    def test_주가상승과_환율하락은_곱셈으로_상쇄된다(self, zero_cost: CostModel) -> None:
        # 주가 +10%, 환율 -10% -> (1.1)(0.9) - 1 = -1%
        r = krw_return(100, 20.0, 1400.0, 22.0, 1260.0, zero_cost)
        assert r == pytest.approx(1.1 * 0.9 - 1.0)

    def test_수량이_달라져도_수익률은_같다(self, cost: CostModel) -> None:
        """주당 정액 수수료(TAF) 때문에 완전히 같지는 않지만 오차가 1bp 미만이어야 한다."""
        small = krw_return(10, 20.0, 1400.0, 22.0, 1400.0, cost)
        large = krw_return(10_000, 20.0, 1400.0, 22.0, 1400.0, cost)
        assert small == pytest.approx(large, abs=1e-4)

    @pytest.mark.parametrize("bad", [(0, 20.0, 1400.0), (10, 0.0, 1400.0), (10, 20.0, 0.0)])
    def test_잘못된_입력은_거부된다(self, bad: tuple[int, float, float], cost: CostModel) -> None:
        qty, price, fx = bad
        with pytest.raises(ValueError):
            krw_cost(qty, price, fx, cost)


class TestRequiredSellPrice:
    """목표 원화 수익률 -> 필요 USD 가격 역산. 익절 계단의 수학적 근거."""

    @pytest.mark.parametrize("target", [0.05, 0.09, 0.14, 0.20])
    def test_역산한_가격에_팔면_정확히_목표수익률이_나온다(
        self, target: float, cost: CostModel
    ) -> None:
        buy, fx = 20.0, 1400.0
        need = required_sell_price_usd(buy, fx, fx, target, cost)
        actual = krw_return(1000, buy, fx, need, fx, cost)
        assert actual == pytest.approx(target, abs=1e-4)

    def test_환율이_내려가면_필요가격이_올라간다(self, cost: CostModel) -> None:
        buy, fx0 = 20.0, 1400.0
        flat = required_sell_price_usd(buy, fx0, fx0, 0.05, cost)
        weaker = required_sell_price_usd(buy, fx0, fx0 * 0.95, 0.05, cost)
        assert weaker > flat
        # 환율이 5% 빠지면 필요가격은 약 1/0.95 배가 된다.
        # 주당 정액 수수료(TAF)는 환율에 비례하지 않으므로 정확히 1/0.95 는 아니다.
        assert weaker / flat == pytest.approx(1 / 0.95, rel=1e-5)

    def test_환율이_올라가면_필요가격이_내려간다(self, cost: CostModel) -> None:
        buy, fx0 = 20.0, 1400.0
        flat = required_sell_price_usd(buy, fx0, fx0, 0.05, cost)
        stronger = required_sell_price_usd(buy, fx0, fx0 * 1.05, 0.05, cost)
        assert stronger < flat

    def test_본전가에_팔면_수익률이_0이다(self, cost: CostModel) -> None:
        be = breakeven_sell_price_usd(20.0, 1400.0, 1400.0, cost)
        assert krw_return(1000, 20.0, 1400.0, be, 1400.0, cost) == pytest.approx(0.0, abs=1e-6)

    def test_본전가는_매수가보다_높다(self, cost: CostModel) -> None:
        """비용이 있으므로 산 가격에 팔면 손해다. 당연하지만 명시적으로 못 박는다."""
        assert breakeven_sell_price_usd(20.0, 1400.0, 1400.0, cost) > 20.0

    def test_목표가_마이너스_100퍼센트_이하면_거부된다(self, cost: CostModel) -> None:
        with pytest.raises(ValueError, match="-100%"):
            required_sell_price_usd(20.0, 1400.0, 1400.0, -1.0, cost)


class TestPositionReturn:
    def test_단일랏_포지션의_수익률은_krw_return과_같다(self, cost: CostModel) -> None:
        pos = Position("KORU").with_lot(
            Lot("A", 100, 20.0, 1400.0, krw_cost(100, 20.0, 1400.0, cost), BASE_TS, 0)
        )
        assert position_krw_return(pos, 22.0, 1400.0, cost) == pytest.approx(
            krw_return(100, 20.0, 1400.0, 22.0, 1400.0, cost)
        )

    def test_빈_포지션은_0을_반환한다(self, cost: CostModel) -> None:
        assert position_krw_return(Position("KORU"), 22.0, 1400.0, cost) == 0.0

    def test_서로_다른_환율에_산_랏들이_원가에_정확히_누적된다(self, zero_cost: CostModel) -> None:
        """분할 매수의 핵심 검증.

        1400원에 100주, 1300원에 100주를 샀다면 원화 원가는 각각의 합이고,
        평가 수익률은 그 합을 분모로 해야 한다.
        """
        pos = Position("KORU")
        pos = pos.with_lot(Lot("A", 100, 20.0, 1400.0, 100 * 20.0 * 1400.0, BASE_TS, 0))
        pos = pos.with_lot(Lot("B", 100, 20.0, 1300.0, 100 * 20.0 * 1300.0, BASE_TS, 1))
        assert pos.cost_krw == pytest.approx(100 * 20.0 * (1400.0 + 1300.0))
        # 환율 1350(가중평균), 가격 그대로면 수익률 0
        r = position_krw_return(pos, 20.0, 1350.0, zero_cost)
        assert r == pytest.approx(0.0, abs=1e-12)

    @pytest.mark.parametrize("target", [0.05, 0.10, 0.20, -0.10])
    def test_포지션_필요가격_역산이_정확하다(self, target: float, cost: CostModel) -> None:
        pos = Position("KORU")
        pos = pos.with_lot(Lot("A", 40, 20.0, 1400.0, krw_cost(40, 20.0, 1400.0, cost), BASE_TS, 0))
        pos = pos.with_lot(Lot("B", 35, 18.5, 1390.0, krw_cost(35, 18.5, 1390.0, cost), BASE_TS, 1))
        need = position_required_price_usd(pos, 1395.0, target, cost)
        actual = position_krw_return(pos, need, 1395.0, cost)
        assert actual == pytest.approx(target, abs=1e-6)

    def test_빈_포지션의_필요가격은_0이다(self, cost: CostModel) -> None:
        assert position_required_price_usd(Position("KORU"), 1400.0, 0.05, cost) == 0.0


class TestDecompose:
    def test_곱셈_분해가_정확히_성립한다(self, cost: CostModel) -> None:
        d = decompose(20.0, 1400.0, 22.0, 1350.0, cost)
        rebuilt = (1 + d.price_return) * (1 + d.fx_return) * (1 + d.cost_drag) - 1
        assert rebuilt == pytest.approx(d.krw_return)

    def test_로그기여도는_덧셈으로_분해된다(self, cost: CostModel) -> None:
        d = decompose(20.0, 1400.0, 22.0, 1350.0, cost)
        assert d.log_price + d.log_fx + d.log_cost == pytest.approx(math.log1p(d.krw_return))

    def test_환율기여도가_음수면_원화수익률이_주가수익률보다_낮다(self, cost: CostModel) -> None:
        d = decompose(20.0, 1400.0, 22.0, 1300.0, cost)
        assert d.fx_return < 0
        assert d.krw_return < d.price_return

    def test_as_dict가_네_항목을_반환한다(self, cost: CostModel) -> None:
        d = decompose(20.0, 1400.0, 22.0, 1350.0, cost).as_dict()
        assert set(d) == {"krw_return", "price_return", "fx_return", "cost_drag"}


class TestProceeds:
    def test_비용이_매도대금을_넘으면_0으로_막는다(self) -> None:
        """극단적으로 비싼 수수료 설정에서도 음수 수취액이 나오면 안 된다."""
        brutal = CostModel(sell_fee_rate=0.9, sec_fee_rate=0.09, finra_taf_per_share=100.0)
        assert krw_proceeds(1, 0.01, 1400.0, brutal) == 0.0

    def test_매도수취액은_명목금액보다_항상_작다(self, cost: CostModel) -> None:
        notional_krw = 100 * 20.0 * 1400.0
        assert krw_proceeds(100, 20.0, 1400.0, cost) < notional_krw

    def test_매수원가는_명목금액보다_항상_크다(self, cost: CostModel) -> None:
        notional_krw = 100 * 20.0 * 1400.0
        assert krw_cost(100, 20.0, 1400.0, cost) > notional_krw
