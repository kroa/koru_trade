"""전략 엔진 검증 — 이 프로젝트에서 가장 중요한 테스트 파일.

사용자 요구사항이 실제로 코드에 구현되어 있는지 여기서 못 박는다.

* 원화 기준 5~20% 분할 익절
* 분할 매수(스케일인)
* 손절이 익절보다 항상 먼저 판정된다
* 리스크 한도가 진입을 막지만 청산은 막지 않는다
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest
from tests.conftest import BASE_TS, make_bar, make_series, open_position

from koru_trade.config import RiskConfig, ScaleInStep, StrategyConfig, TakeProfitStep
from koru_trade.models import Action, ExitReason, Position
from koru_trade.pnl import CostModel, position_krw_return, position_required_price_usd
from koru_trade.risk import RiskState
from koru_trade.strategy import (
    allocate_tranches,
    business_days_between,
    decide,
    evaluate_entry,
    plan_entry,
    scale_in_trigger_price,
    stop_price_usd,
)


class TestAllocateTranches:
    def test_비중대로_배분된다(self) -> None:
        assert allocate_tranches(100, [0.4, 0.35, 0.25]) == (40, 35, 25)

    def test_합계가_항상_총수량과_같다(self) -> None:
        for total in range(3, 300):
            alloc = allocate_tranches(total, [0.4, 0.35, 0.25])
            assert sum(alloc) == total

    def test_모든_차수가_최소_1주를_받는다(self) -> None:
        for total in range(3, 60):
            alloc = allocate_tranches(total, [0.4, 0.35, 0.25])
            assert all(q >= 1 for q in alloc), (total, alloc)

    def test_차수보다_수량이_적으면_1차에_몰아준다(self) -> None:
        assert allocate_tranches(2, [0.4, 0.35, 0.25]) == (2,)
        assert allocate_tranches(1, [0.5, 0.5]) == (1,)

    def test_0이하_수량은_빈_결과다(self) -> None:
        assert allocate_tranches(0, [0.5, 0.5]) == ()
        assert allocate_tranches(10, []) == ()

    def test_앞차수_비중이_뒤차수보다_크게_유지된다(self) -> None:
        """마틴게일 방지. 1차가 가장 커야 한다."""
        for total in (10, 37, 101, 999):
            alloc = allocate_tranches(total, [0.4, 0.35, 0.25])
            assert alloc[0] >= alloc[1] >= alloc[2]


class TestBusinessDays:
    def test_주말은_세지_않는다(self) -> None:
        mon = dt.datetime(2026, 8, 24)  # 월
        assert business_days_between(mon, dt.datetime(2026, 8, 28)) == 4  # 금
        assert business_days_between(mon, dt.datetime(2026, 8, 31)) == 5  # 다음 월

    def test_역순이면_0이다(self) -> None:
        assert business_days_between(dt.datetime(2026, 8, 28), dt.datetime(2026, 8, 24)) == 0

    def test_같은_날은_0이다(self) -> None:
        d = dt.datetime(2026, 8, 24)
        assert business_days_between(d, d) == 0


class TestStopPrice:
    def test_1차_진입가_기준으로_계산된다(self, cfg: StrategyConfig) -> None:
        pos = open_position(qty=100, price=20.0, atr=1.0)
        # 2.0 x ATR(1.0) / 20 = 10% -> min/max 클램프 안쪽
        assert stop_price_usd(pos, cfg) == pytest.approx(18.0)

    def test_상한으로_클램프된다(self, cfg: StrategyConfig) -> None:
        """ATR 이 폭발해도 손절폭이 max_stop_pct 를 넘지 않는다."""
        pos = open_position(qty=100, price=20.0, atr=10.0)
        assert stop_price_usd(pos, cfg) == pytest.approx(20.0 * (1 - cfg.max_stop_pct))

    def test_하한으로_클램프된다(self, cfg: StrategyConfig) -> None:
        pos = open_position(qty=100, price=20.0, atr=0.01)
        assert stop_price_usd(pos, cfg) == pytest.approx(20.0 * (1 - cfg.min_stop_pct))

    def test_분할매수해도_손절선이_내려가지_않는다(self, cfg: StrategyConfig) -> None:
        """물타기로 평단이 내려가도 손절선은 1차 진입가 기준으로 고정된다."""
        from koru_trade.models import Lot

        pos = open_position(qty=40, price=20.0, atr=1.0)
        before = stop_price_usd(pos, cfg)
        pos = pos.with_lot(Lot("B", 35, 16.0, 1400.0, 35 * 16.0 * 1400.0, BASE_TS, 1))
        assert pos.avg_price_usd < 20.0
        assert stop_price_usd(pos, cfg) == pytest.approx(before)

    def test_빈_포지션의_손절가는_0이다(self, cfg: StrategyConfig) -> None:
        assert stop_price_usd(Position("KORU"), cfg) == 0.0


class TestScaleInTrigger:
    def test_ATR_배수만큼_아래다(self, cfg: StrategyConfig) -> None:
        pos = open_position(price=20.0, atr=1.0)
        assert scale_in_trigger_price(pos, 0, cfg) == pytest.approx(20.0)
        assert scale_in_trigger_price(pos, 1, cfg) == pytest.approx(20.0 - 0.7)
        assert scale_in_trigger_price(pos, 2, cfg) == pytest.approx(20.0 - 1.4)

    def test_트리거는_항상_손절선보다_위다(self, cfg: StrategyConfig) -> None:
        """손절선 아래에서 추가 매수하면 사자마자 손절당한다."""
        pos = open_position(price=20.0, atr=1.0)
        stop = stop_price_usd(pos, cfg)
        for i in range(len(cfg.scale_in)):
            assert scale_in_trigger_price(pos, i, cfg) > stop

    def test_없는_차수는_거부된다(self, cfg: StrategyConfig) -> None:
        pos = open_position()
        with pytest.raises(ValueError, match="존재하지 않는 차수"):
            scale_in_trigger_price(pos, 99, cfg)


class TestEntryFilters:
    def test_데이터가_부족하면_즉시_차단된다(self, cfg: StrategyConfig) -> None:
        sig = evaluate_entry(make_series(5), cfg)
        assert not sig.allowed
        assert sig.checks[0].name == "데이터충분성"
        assert len(sig.checks) == 1  # 이후 필터는 평가하지 않는다

    def test_고변동성은_차단된다(self, cfg: StrategyConfig) -> None:
        bars = make_series(120, drift=0.003, amplitude=0.12)
        sig = evaluate_entry(bars, cfg)
        assert any(c.name == "변동성레짐" and not c.passed for c in sig.checks)

    def test_역배열은_차단된다(self, cfg: StrategyConfig) -> None:
        bars = make_series(120, drift=-0.005)
        sig = evaluate_entry(bars, cfg)
        assert any(c.name == "추세방향" and not c.passed for c in sig.checks)

    def test_과열_RSI는_차단된다(self, cfg: StrategyConfig) -> None:
        bars = make_series(120, drift=0.01)
        sig = evaluate_entry(bars, cfg)
        blocker = next(c for c in sig.checks if c.name == "모멘텀(RSI)")
        assert not blocker.passed
        assert "과열" in blocker.detail

    def test_환율_급락은_차단된다(self, cfg: StrategyConfig) -> None:
        """사용자 요구 '환율로 손해보지 않도록' 의 진입 단계 구현."""
        bars = make_series(120, drift=0.002, fx_drift=-0.004)
        sig = evaluate_entry(bars, cfg)
        blocker = next(c for c in sig.checks if c.name == "환율추세")
        assert not blocker.passed
        assert "원화 강세 역풍" in blocker.detail

    def test_유동성_부족은_차단된다(self, cfg: StrategyConfig) -> None:
        bars = make_series(120, drift=0.002, volume=1.0)
        sig = evaluate_entry(bars, cfg)
        assert any(c.name == "유동성" and not c.passed for c in sig.checks)

    def test_환율수준_상한은_설정할_때만_검사한다(self, cfg: StrategyConfig) -> None:
        bars = make_series(120, drift=0.002)
        assert not any(c.name == "환율수준" for c in evaluate_entry(bars, cfg).checks)
        capped = replace(cfg, fx_max_level=1000.0)
        blocker = next(c for c in evaluate_entry(bars, capped).checks if c.name == "환율수준")
        assert not blocker.passed

    def test_모든_조건이_맞으면_통과한다(self, cfg: StrategyConfig) -> None:
        """완만한 상승 + 적당한 변동성 + 안정 환율."""
        bars = make_series(150, drift=0.0022, amplitude=0.012)
        sig = evaluate_entry(bars, cfg)
        assert sig.allowed, sig.summary

    def test_요약_문자열이_차단_사유를_담는다(self, cfg: StrategyConfig) -> None:
        sig = evaluate_entry(make_series(120, drift=-0.005), cfg)
        assert "미충족" in sig.summary
        assert len(sig.blockers) >= 1


class TestPlanEntry:
    def test_자본에_맞는_수량이_계산된다(self, cfg: StrategyConfig) -> None:
        bars = make_series(120, drift=0.002)
        plan = plan_entry(bars, cfg)
        assert plan is not None
        assert plan.total_qty == sum(plan.tranche_qty)
        assert len(plan.tranche_qty) == len(cfg.scale_in)

    def test_자본이_1주값에_못미치면_None이다(self, cfg: StrategyConfig) -> None:
        bars = make_series(120, drift=0.002, start=20.0)
        tiny = replace(cfg, capital_krw=1000.0)
        assert plan_entry(bars, tiny) is None

    def test_ATR을_계산할_수_없으면_None이다(self, cfg: StrategyConfig) -> None:
        assert plan_entry(make_series(5), cfg) is None

    def test_빈_봉이면_None이다(self, cfg: StrategyConfig) -> None:
        assert plan_entry([], cfg) is None

    def test_투입예정액이_자본을_넘지_않는다(self, cfg: StrategyConfig) -> None:
        bars = make_series(120, drift=0.002)
        plan = plan_entry(bars, cfg)
        assert plan is not None
        assert plan.total_notional_krw <= cfg.capital_krw * 1.001


class TestDecideFlat:
    def test_봉이_없으면_HOLD다(self, cfg: StrategyConfig, fresh_risk: RiskState) -> None:
        d = decide([], Position("KORU"), fresh_risk, cfg)
        assert d.action is Action.HOLD

    def test_조건_충족시_ENTER한다(self, cfg: StrategyConfig, fresh_risk: RiskState) -> None:
        bars = make_series(150, drift=0.0022, amplitude=0.012)
        d = decide(bars, Position("KORU"), fresh_risk, cfg)
        assert d.action is Action.ENTER
        assert d.entry_plan is not None
        assert d.qty == d.entry_plan.tranche_qty[0]
        assert d.limit_price_usd is not None
        assert d.limit_price_usd > bars[-1].close  # 매수는 현재가보다 위

    def test_필터에_막히면_HOLD다(self, cfg: StrategyConfig, fresh_risk: RiskState) -> None:
        bars = make_series(150, drift=-0.004)
        d = decide(bars, Position("KORU"), fresh_risk, cfg)
        assert d.action is Action.HOLD
        assert "진입 보류" in d.rationale

    def test_리스크_한도에_막히면_BLOCKED다(self, cfg: StrategyConfig) -> None:
        bars = make_series(150, drift=0.0022, amplitude=0.012)
        halted = RiskState(trade_date=BASE_TS.date()).with_halt("테스트")
        d = decide(bars, Position("KORU"), halted, cfg)
        assert d.action is Action.BLOCKED
        assert "킬스위치" in d.rationale


class TestTakeProfitLadder:
    """사용자 핵심 요구: 원화 기준 5~20% 분할 익절."""

    def _at_return(self, pos: Position, target: float, cfg: StrategyConfig) -> tuple[float, float]:
        """목표 원화수익률이 나오는 (가격, 환율) 을 만든다."""
        fx = 1400.0
        price = position_required_price_usd(pos, fx, target, cfg.cost)
        return price, fx

    def test_5퍼센트에서_1단_익절한다(self, cfg: StrategyConfig, fresh_risk: RiskState) -> None:
        pos = open_position(qty=100, price=20.0, atr=1.0)
        price, fx = self._at_return(pos, 0.0501, cfg)
        bars = [make_bar(i, close=price, fx=fx) for i in range(3)]
        d = decide(bars, pos, fresh_risk, cfg)
        assert d.action is Action.TAKE_PROFIT
        assert d.tp_levels == (0,)
        assert d.qty == 30  # 기준수량 100주의 30%
        assert d.reason is ExitReason.TAKE_PROFIT_LADDER

    def test_이미_체결된_계단은_다시_실행되지_않는다(
        self, cfg: StrategyConfig, fresh_risk: RiskState
    ) -> None:
        pos = replace(
            open_position(qty=70, price=20.0, atr=1.0),
            tp_levels_hit=frozenset({0}),
            ladder_base_qty=100,
        )
        price, fx = self._at_return(pos, 0.06, cfg)
        bars = [make_bar(i, close=price, fx=fx) for i in range(3)]
        d = decide(bars, pos, fresh_risk, cfg)
        assert d.action is not Action.TAKE_PROFIT

    def test_갭_상승으로_여러_계단을_한번에_넘으면_합산_매도한다(
        self, cfg: StrategyConfig, fresh_risk: RiskState
    ) -> None:
        pos = open_position(qty=100, price=20.0, atr=1.0)
        price, fx = self._at_return(pos, 0.145, cfg)  # 5%, 9%, 14% 동시 도달
        bars = [make_bar(i, close=price, fx=fx) for i in range(3)]
        d = decide(bars, pos, fresh_risk, cfg)
        assert d.action is Action.TAKE_PROFIT
        assert d.tp_levels == (0, 1, 2)
        assert d.qty == 85  # 30+30+25

    def test_마지막_계단은_잔량_전부를_판다(
        self, cfg: StrategyConfig, fresh_risk: RiskState
    ) -> None:
        pos = replace(
            open_position(qty=17, price=20.0, atr=1.0),
            tp_levels_hit=frozenset({0, 1, 2}),
            ladder_base_qty=100,
        )
        price, fx = self._at_return(pos, 0.21, cfg)
        bars = [make_bar(i, close=price, fx=fx) for i in range(3)]
        d = decide(bars, pos, fresh_risk, cfg)
        assert d.action is Action.TAKE_PROFIT
        assert d.qty == 17  # 잔량 전부

    def test_문턱에_못미치면_익절하지_않는다(
        self, cfg: StrategyConfig, fresh_risk: RiskState
    ) -> None:
        pos = open_position(qty=100, price=20.0, atr=1.0)
        price, fx = self._at_return(pos, 0.049, cfg)
        bars = [make_bar(i, close=price, fx=fx) for i in range(3)]
        d = decide(bars, pos, fresh_risk, cfg)
        assert d.action is Action.HOLD

    def test_주가는_올랐지만_환율이_빠져서_원화_5퍼센트에_못미치면_익절하지_않는다(
        self, cfg: StrategyConfig, fresh_risk: RiskState
    ) -> None:
        """이 테스트가 사용자 요구사항의 핵심이다.

        USD 로는 +6% 인데 환율이 -3% 빠져서 원화로는 +2.3% 라면 팔면 안 된다.
        """
        pos = open_position(qty=100, price=20.0, fx=1400.0, atr=1.0)
        price, fx = 20.0 * 1.06, 1400.0 * 0.97
        krw_r = position_krw_return(pos, price, fx, cfg.cost)
        assert 0.0 < krw_r < 0.05  # USD 로는 이익이지만 원화로는 문턱 미달
        bars = [make_bar(i, close=price, fx=fx) for i in range(3)]
        assert decide(bars, pos, fresh_risk, cfg).action is Action.HOLD

    def test_주가는_그대로인데_환율이_올라서_원화_5퍼센트면_익절한다(
        self, cfg: StrategyConfig, fresh_risk: RiskState
    ) -> None:
        """반대 방향도 성립해야 한다. 환차익만으로도 목표에 도달하면 판다."""
        pos = open_position(qty=100, price=20.0, fx=1400.0, atr=1.0)
        bars = [make_bar(i, close=20.0, fx=1400.0 * 1.07) for i in range(3)]
        d = decide(bars, pos, fresh_risk, cfg)
        assert d.action is Action.TAKE_PROFIT


class TestStops:
    def test_USD_손절선에_닿으면_전량_청산한다(
        self, cfg: StrategyConfig, fresh_risk: RiskState
    ) -> None:
        """ATR 손절이 원화 하드스톱보다 좁을 때 USD 손절이 먼저 발동한다.

        ATR 0.5 -> 손절폭 2.0x0.5/20 = 5%. 원화 -10% 보다 훨씬 좁다.
        """
        pos = open_position(qty=100, price=20.0, atr=0.5)
        stop = stop_price_usd(pos, cfg)
        assert stop == pytest.approx(19.0)
        bars = [make_bar(i, close=18.95, fx=1400.0) for i in range(3)]
        d = decide(bars, pos, fresh_risk, cfg)
        assert d.action is Action.EXIT
        assert d.reason is ExitReason.HARD_STOP_USD
        assert d.qty == 100

    def test_두_손절선_중_더_가까운_쪽이_먼저_걸린다(
        self, cfg: StrategyConfig, fresh_risk: RiskState
    ) -> None:
        """설계 의도의 명시적 고정.

        원화 하드스톱(-10%)과 ATR 손절은 독립적으로 작동하며,
        **먼저 닿는 쪽**이 청산을 일으킨다. 기본 파라미터에서는
        ATR 손절이 넓게(최대 12%) 잡히면 원화 스톱이 바깥 경계 역할을 한다.
        """
        wide = open_position(qty=100, price=20.0, atr=5.0)  # 손절폭 12% 로 클램프
        bars = [make_bar(i, close=17.9, fx=1400.0) for i in range(3)]
        d = decide(bars, wide, fresh_risk, cfg)
        assert d.action is Action.EXIT
        # 가격 -10.5% 는 ATR 손절선(-12%) 위지만 원화로는 -10% 아래다.
        assert d.reason is ExitReason.HARD_STOP_KRW

    def test_원화_하드스톱이_USD보다_먼저_걸릴_수_있다(
        self, cfg: StrategyConfig, fresh_risk: RiskState
    ) -> None:
        """주가는 손절선 위인데 환율이 무너지면 원화 스톱이 먼저 발동한다."""
        pos = open_position(qty=100, price=20.0, fx=1400.0, atr=1.0)
        bars = [make_bar(i, close=19.5, fx=1400.0 * 0.88) for i in range(3)]
        assert stop_price_usd(pos, cfg) < 19.5
        d = decide(bars, pos, fresh_risk, cfg)
        assert d.action is Action.EXIT
        assert d.reason is ExitReason.HARD_STOP_KRW

    def test_손절이_익절보다_먼저_판정된다(
        self, cfg: StrategyConfig, fresh_risk: RiskState
    ) -> None:
        """수익률이 -10% 이하이면서 동시에 익절 문턱을 넘는 상황은 불가능하지만,
        판정 순서 자체는 코드로 못 박아 둔다."""
        pos = open_position(qty=100, price=20.0, atr=1.0)
        bars = [make_bar(i, close=17.0, fx=1400.0) for i in range(3)]
        d = decide(bars, pos, fresh_risk, cfg)
        assert d.action is Action.EXIT

    def test_본전_스톱은_1단_익절_후_작동한다(
        self, cfg: StrategyConfig, fresh_risk: RiskState
    ) -> None:
        pos = replace(
            open_position(qty=70, price=20.0, atr=1.0),
            tp_levels_hit=frozenset({0}),
            ladder_base_qty=100,
        )
        breakeven = position_required_price_usd(pos, 1400.0, 0.0, cfg.cost)
        bars = [make_bar(i, close=breakeven * 0.999, fx=1400.0) for i in range(3)]
        d = decide(bars, pos, fresh_risk, cfg)
        assert d.action is Action.EXIT
        assert d.reason is ExitReason.BREAKEVEN_STOP

    def test_익절_전에는_본전_스톱이_작동하지_않는다(
        self, cfg: StrategyConfig, fresh_risk: RiskState
    ) -> None:
        pos = open_position(qty=100, price=20.0, atr=1.0)
        breakeven = position_required_price_usd(pos, 1400.0, 0.0, cfg.cost)
        bars = [make_bar(i, close=breakeven * 0.999, fx=1400.0) for i in range(3)]
        assert decide(bars, pos, fresh_risk, cfg).action is not Action.EXIT

    def test_트레일링_스톱은_2단_익절_후_작동한다(
        self, cfg: StrategyConfig, fresh_risk: RiskState
    ) -> None:
        pos = replace(
            open_position(qty=40, price=20.0, atr=1.0),
            tp_levels_hit=frozenset({0, 1}),
            ladder_base_qty=100,
            peak_krw_return=0.15,
        )
        # 최고 +15% 에서 35% 반납 -> 트레일 기준 +9.75%
        price = position_required_price_usd(pos, 1400.0, 0.09, cfg.cost)
        bars = [make_bar(i, close=price, fx=1400.0) for i in range(3)]
        d = decide(bars, pos, fresh_risk, cfg)
        assert d.action is Action.EXIT
        assert d.reason is ExitReason.TRAILING_STOP

    def test_타임_스톱이_보유기간_초과시_청산한다(
        self, cfg: StrategyConfig, fresh_risk: RiskState
    ) -> None:
        pos = open_position(qty=100, price=20.0, atr=1.0, ts=BASE_TS)
        later = BASE_TS + dt.timedelta(days=9)  # 영업일 7일
        bars = [make_bar(close=20.1, fx=1400.0, ts=later)]
        d = decide(bars, pos, fresh_risk, cfg, now=later)
        assert d.action is Action.EXIT
        assert d.reason is ExitReason.TIME_STOP

    def test_보유기간_내에는_타임스톱이_작동하지_않는다(
        self, cfg: StrategyConfig, fresh_risk: RiskState
    ) -> None:
        pos = open_position(qty=100, price=20.0, atr=1.0, ts=BASE_TS)
        soon = BASE_TS + dt.timedelta(days=2)
        bars = [make_bar(close=20.1, fx=1400.0, ts=soon)]
        assert decide(bars, pos, fresh_risk, cfg, now=soon).action is not Action.EXIT


class TestScaleIn:
    def test_트리거_도달시_추가_매수한다(self, cfg: StrategyConfig, fresh_risk: RiskState) -> None:
        pos = open_position(qty=40, price=20.0, atr=1.0, plan=(40, 35, 25))
        trigger = scale_in_trigger_price(pos, 1, cfg)
        bars = [make_bar(i, close=trigger, open_=trigger * 0.99, fx=1400.0) for i in range(3)]
        d = decide(bars, pos, fresh_risk, cfg)
        assert d.action is Action.SCALE_IN
        assert d.qty == 35

    def test_음봉이면_받지_않는다(self, cfg: StrategyConfig, fresh_risk: RiskState) -> None:
        """떨어지는 칼 방지. 종가가 시가보다 낮으면 하락이 진행 중이다."""
        pos = open_position(qty=40, price=20.0, atr=1.0, plan=(40, 35, 25))
        trigger = scale_in_trigger_price(pos, 1, cfg)
        bars = [make_bar(i, close=trigger, open_=trigger * 1.02, fx=1400.0) for i in range(3)]
        assert decide(bars, pos, fresh_risk, cfg).action is Action.HOLD

    def test_반등확인_조건을_끄면_음봉에도_받는다(
        self, cfg: StrategyConfig, fresh_risk: RiskState
    ) -> None:
        loose = replace(cfg, scale_in_requires_reclaim=False)
        pos = open_position(qty=40, price=20.0, atr=1.0, plan=(40, 35, 25))
        trigger = scale_in_trigger_price(pos, 1, loose)
        bars = [make_bar(i, close=trigger, open_=trigger * 1.02, fx=1400.0) for i in range(3)]
        assert decide(bars, pos, fresh_risk, loose).action is Action.SCALE_IN

    def test_손절선_아래에서는_절대_추가_매수하지_않는다(
        self, cfg: StrategyConfig, fresh_risk: RiskState
    ) -> None:
        """이 규칙이 없으면 손절선 아래에서 물타기를 하다 계좌가 사라진다."""
        pos = open_position(qty=40, price=20.0, atr=5.0, plan=(40, 35, 25))
        stop = stop_price_usd(pos, cfg)
        bars = [make_bar(i, close=stop * 0.99, open_=stop * 0.98, fx=1400.0) for i in range(3)]
        d = decide(bars, pos, fresh_risk, cfg)
        assert d.action is not Action.SCALE_IN

    def test_익절이_시작되면_추가_매수하지_않는다(
        self, cfg: StrategyConfig, fresh_risk: RiskState
    ) -> None:
        pos = replace(
            open_position(qty=40, price=20.0, atr=1.0, plan=(40, 35, 25)),
            tp_levels_hit=frozenset({0}),
            ladder_base_qty=70,
        )
        trigger = scale_in_trigger_price(pos, 1, cfg)
        bars = [make_bar(i, close=trigger, open_=trigger * 0.99, fx=1400.0) for i in range(3)]
        assert decide(bars, pos, fresh_risk, cfg).action is not Action.SCALE_IN

    def test_계획한_차수를_모두_채우면_더_사지_않는다(
        self, cfg: StrategyConfig, fresh_risk: RiskState
    ) -> None:
        from koru_trade.models import Lot

        pos = open_position(qty=40, price=20.0, atr=1.0, plan=(40, 35, 25))
        for i, (q, p) in enumerate([(35, 19.3), (25, 18.6)], start=1):
            pos = pos.with_lot(Lot(f"L{i}", q, p, 1400.0, q * p * 1400.0, BASE_TS, i))
        bars = [make_bar(i, close=17.5, open_=17.4, fx=1400.0) for i in range(3)]
        assert decide(bars, pos, fresh_risk, cfg).action is not Action.SCALE_IN

    def test_리스크_한도에_걸리면_BLOCKED다(self, cfg: StrategyConfig) -> None:
        pos = open_position(qty=40, price=20.0, atr=1.0, plan=(40, 35, 25))
        halted = RiskState(trade_date=BASE_TS.date()).with_halt("테스트")
        trigger = scale_in_trigger_price(pos, 1, cfg)
        bars = [make_bar(i, close=trigger, open_=trigger * 0.99, fx=1400.0) for i in range(3)]
        d = decide(bars, pos, halted, cfg)
        assert d.action is Action.BLOCKED
        assert "분할매수" in d.rationale


class TestExitAlwaysAllowed:
    def test_킬스위치가_걸려도_손절은_실행된다(self, cfg: StrategyConfig) -> None:
        """진입은 막아도 청산은 절대 막으면 안 된다."""
        pos = open_position(qty=100, price=20.0, atr=1.0)
        halted = RiskState(trade_date=BASE_TS.date()).with_halt("한도 초과")
        bars = [make_bar(i, close=17.0, fx=1400.0) for i in range(3)]
        d = decide(bars, pos, halted, cfg)
        assert d.action is Action.EXIT

    def test_킬스위치가_걸려도_익절은_실행된다(self, cfg: StrategyConfig) -> None:
        pos = open_position(qty=100, price=20.0, atr=1.0)
        halted = RiskState(trade_date=BASE_TS.date()).with_halt("한도 초과")
        price = position_required_price_usd(pos, 1400.0, 0.06, cfg.cost)
        bars = [make_bar(i, close=price, fx=1400.0) for i in range(3)]
        assert decide(bars, pos, halted, cfg).action is Action.TAKE_PROFIT


class TestDeterminism:
    def test_같은_입력은_같은_결정을_준다(self, cfg: StrategyConfig, fresh_risk: RiskState) -> None:
        """백테스트와 실거래가 같은 코드를 쓰는 근거."""
        bars = make_series(150, drift=0.0022, amplitude=0.012)
        pos = Position("KORU")
        first = decide(bars, pos, fresh_risk, cfg)
        for _ in range(5):
            again = decide(bars, pos, fresh_risk, cfg)
            assert again.action is first.action
            assert again.qty == first.qty
            assert again.rationale == first.rationale

    def test_모든_결정에_판단근거가_있다(self, cfg: StrategyConfig, fresh_risk: RiskState) -> None:
        for drift in (-0.005, 0.0, 0.002, 0.01):
            bars = make_series(150, drift=drift, amplitude=0.01)
            d = decide(bars, Position("KORU"), fresh_risk, cfg)
            assert d.rationale
            assert len(d.rationale) > 10


class TestConfigValidation:
    def test_익절_문턱이_5퍼센트_미만이면_거부된다(self) -> None:
        with pytest.raises(ValueError, match="5%~20%"):
            TakeProfitStep(krw_return=0.03, sell_fraction=0.3)

    def test_익절_문턱이_20퍼센트_초과면_거부된다(self) -> None:
        with pytest.raises(ValueError, match="5%~20%"):
            TakeProfitStep(krw_return=0.25, sell_fraction=0.3)

    def test_분할매수_비중_합이_1이_아니면_거부된다(self) -> None:
        with pytest.raises(ValueError, match="weight 합계"):
            StrategyConfig(
                scale_in=(ScaleInStep(0.0, 0.5), ScaleInStep(1.0, 0.3)),
            )

    def test_익절_비율_합이_1을_넘으면_거부된다(self) -> None:
        with pytest.raises(ValueError, match=r"1\.0을 초과"):
            StrategyConfig(
                take_profit=(
                    TakeProfitStep(0.05, 0.6),
                    TakeProfitStep(0.10, 0.6),
                ),
            )

    def test_1차_분할매수는_즉시_진입이어야_한다(self) -> None:
        with pytest.raises(ValueError, match="1차는 atr_multiple=0"):
            StrategyConfig(scale_in=(ScaleInStep(0.5, 1.0),))

    def test_익절_문턱이_오름차순이_아니면_거부된다(self) -> None:
        with pytest.raises(ValueError, match="오름차순"):
            StrategyConfig(
                take_profit=(TakeProfitStep(0.10, 0.5), TakeProfitStep(0.05, 0.5)),
            )

    def test_왕복비용보다_낮은_익절문턱은_거부된다(self) -> None:
        """비용이 5% 를 넘으면 첫 계단이 구조적으로 손실이다."""
        expensive = CostModel(buy_fee_rate=0.03, sell_fee_rate=0.03)
        with pytest.raises(ValueError, match="구조적으로 손실"):
            StrategyConfig(cost=expensive)

    def test_자본이_1회_한도를_넘으면_거부된다(self) -> None:
        with pytest.raises(ValueError, match="max_position_krw"):
            StrategyConfig(
                capital_krw=10_000_000,
                risk=RiskConfig(max_position_krw=5_000_000),
            )

    def test_하드스톱이_양수면_거부된다(self) -> None:
        with pytest.raises(ValueError, match="음수여야"):
            StrategyConfig(hard_stop_krw_return=0.1)

    def test_EMA_기간_순서가_뒤집히면_거부된다(self) -> None:
        with pytest.raises(ValueError, match="ema_fast"):
            StrategyConfig(ema_fast=30, ema_slow=10)

    def test_기본_설정의_익절_문턱이_5에서_20퍼센트_범위다(self) -> None:
        """사용자 요구사항이 기본값으로 지켜지는지 직접 확인한다."""
        cfg = StrategyConfig()
        thresholds = [s.krw_return for s in cfg.take_profit]
        assert min(thresholds) == pytest.approx(0.05)
        assert max(thresholds) == pytest.approx(0.20)
        assert sum(s.sell_fraction for s in cfg.take_profit) == pytest.approx(1.0)


class TestIntradayStops:
    """분봉 전용 청산 규칙. 영업일 기준 타임스톱은 분봉에서 작동하지 않는다."""

    @staticmethod
    def _intraday_bars(n: int, start_hour: int = 10, minutes: int = 5):
        """ET 벽시계 기준 분봉."""
        base = dt.datetime(2026, 8, 26, start_hour, 0)
        return [
            make_bar(close=20.0, fx=1400.0, ts=base + dt.timedelta(minutes=minutes * i))
            for i in range(n)
        ]

    def test_봉_수_상한에_도달하면_청산한다(
        self, cfg: StrategyConfig, fresh_risk: RiskState
    ) -> None:
        c = replace(cfg, max_holding_bars=6)
        bars = self._intraday_bars(10)
        pos = open_position(qty=100, price=20.0, atr=1.0, ts=bars[0].ts)
        d = decide(bars, pos, fresh_risk, c, now=bars[-1].ts)
        assert d.action is Action.EXIT
        assert d.reason is ExitReason.TIME_STOP
        assert "봉" in d.rationale

    def test_상한_전에는_유지한다(self, cfg: StrategyConfig, fresh_risk: RiskState) -> None:
        c = replace(cfg, max_holding_bars=20)
        bars = self._intraday_bars(10)
        pos = open_position(qty=100, price=20.0, atr=1.0, ts=bars[0].ts)
        assert decide(bars, pos, fresh_risk, c, now=bars[-1].ts).action is not Action.EXIT

    def test_봉_상한이_영업일_상한을_대체한다(
        self, cfg: StrategyConfig, fresh_risk: RiskState
    ) -> None:
        """분봉에서 영업일은 0이라 max_holding_days 로는 절대 청산되지 않는다."""
        bars = self._intraday_bars(50)
        pos = open_position(qty=100, price=20.0, atr=1.0, ts=bars[0].ts)
        # 봉 상한 없음 -> 하루 안이라 타임스톱 안 걸림
        no_bars = replace(cfg, max_holding_days=1)
        assert decide(bars, pos, fresh_risk, no_bars, now=bars[-1].ts).action is not Action.EXIT
        # 봉 상한 설정 -> 걸림
        with_bars = replace(cfg, max_holding_bars=10)
        assert decide(bars, pos, fresh_risk, with_bars, now=bars[-1].ts).action is Action.EXIT

    def test_장_마감_전에_전량_청산한다(self, cfg: StrategyConfig, fresh_risk: RiskState) -> None:
        """오버나이트 갭을 피하는 진짜 단타 규칙."""
        c = replace(cfg, close_minutes_before_session_end=10)
        bars = [make_bar(close=20.0, fx=1400.0, ts=dt.datetime(2026, 8, 26, 15, 50))]
        pos = open_position(qty=100, price=20.0, atr=1.0, ts=dt.datetime(2026, 8, 26, 10, 0))
        d = decide(bars, pos, fresh_risk, c, now=bars[-1].ts)
        assert d.action is Action.EXIT
        assert d.qty == 100
        assert "마감" in d.rationale

    def test_마감_전이_아니면_유지한다(self, cfg: StrategyConfig, fresh_risk: RiskState) -> None:
        c = replace(cfg, close_minutes_before_session_end=10)
        bars = [make_bar(close=20.0, fx=1400.0, ts=dt.datetime(2026, 8, 26, 14, 0))]
        pos = open_position(qty=100, price=20.0, atr=1.0, ts=dt.datetime(2026, 8, 26, 10, 0))
        assert decide(bars, pos, fresh_risk, c, now=bars[-1].ts).action is not Action.EXIT

    def test_마감_청산은_기본적으로_꺼져있다(
        self, cfg: StrategyConfig, fresh_risk: RiskState
    ) -> None:
        """일봉 경로에서는 봉 시각이 자정이라 켜져 있으면 항상 청산돼 버린다."""
        assert cfg.close_minutes_before_session_end is None
        bars = [make_bar(close=20.0, fx=1400.0, ts=dt.datetime(2026, 8, 26, 15, 59))]
        pos = open_position(qty=100, price=20.0, atr=1.0, ts=dt.datetime(2026, 8, 26, 10, 0))
        assert decide(bars, pos, fresh_risk, cfg, now=bars[-1].ts).action is not Action.EXIT

    def test_잘못된_설정은_거부된다(self) -> None:
        with pytest.raises(ValueError, match="max_holding_bars"):
            StrategyConfig(max_holding_bars=0)
        with pytest.raises(ValueError, match="close_minutes_before_session_end"):
            StrategyConfig(close_minutes_before_session_end=500)
        with pytest.raises(ValueError, match="session_end_et"):
            StrategyConfig(session_end_et="이상한값")
