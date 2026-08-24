"""리스크 한도와 킬스위치 검증.

여기 있는 테스트가 통과하지 않으면 봇을 절대 켜면 안 된다.
전략이 틀리면 돈을 잃지만, 킬스위치가 틀리면 계좌가 사라진다.
"""

from __future__ import annotations

import datetime as dt

import pytest

from koru_trade.config import RiskConfig
from koru_trade.risk import (
    RiskState,
    add_business_days,
    check_entry_allowed,
    check_scale_in_allowed,
)

TODAY = dt.date(2026, 8, 24)
TOMORROW = dt.date(2026, 8, 25)


@pytest.fixture
def rcfg() -> RiskConfig:
    return RiskConfig(
        max_position_krw=5_000_000,
        max_daily_notional_krw=3_000_000,
        daily_loss_limit_krw=300_000,
        max_consecutive_losses=3,
        max_trades_per_day=3,
        max_open_positions=1,
    )


@pytest.fixture
def state() -> RiskState:
    return RiskState(trade_date=TODAY)


class TestRiskConfig:
    def test_0이하_한도는_거부된다(self) -> None:
        with pytest.raises(ValueError, match="0보다 커야"):
            RiskConfig(daily_loss_limit_krw=0)

    def test_모순된_한도는_거부된다(self) -> None:
        """일일 투입 한도가 1회 한도 x 횟수 한도보다 크면 서로 모순이다."""
        with pytest.raises(ValueError, match="모순"):
            RiskConfig(
                max_position_krw=1_000_000,
                max_daily_notional_krw=10_000_000,
                max_trades_per_day=2,
            )


class TestRiskStateRoll:
    def test_날짜가_바뀌면_일일집계가_초기화된다(self, state: RiskState) -> None:
        used = state.with_entry(1_000_000).with_realized(-50_000, closed_trade=True)
        rolled = used.rolled_to(TOMORROW)
        assert rolled.realized_krw_today == 0.0
        assert rolled.notional_krw_today == 0.0
        assert rolled.entries_today == 0

    def test_연속손실은_날짜가_바뀌어도_유지된다(self, state: RiskState) -> None:
        """하루 잤다고 전략이 고쳐지지는 않는다."""
        s = state.with_realized(-10_000, closed_trade=True).with_realized(
            -10_000, closed_trade=True
        )
        assert s.consecutive_losses == 2
        assert s.rolled_to(TOMORROW).consecutive_losses == 2

    def test_수동정지는_날짜가_바뀌어도_유지된다(self, state: RiskState) -> None:
        from dataclasses import replace

        s = replace(state, manual_halt=True)
        assert s.rolled_to(TOMORROW).manual_halt

    def test_과거_날짜로는_되돌아가지_않는다(self, state: RiskState) -> None:
        s = state.with_entry(100_000)
        assert s.rolled_to(dt.date(2026, 1, 1)) is s

    def test_킬스위치_사유는_날짜가_바뀌면_해제된다(self, state: RiskState) -> None:
        s = state.with_halt("당일 손실 한도")
        assert s.is_halted
        assert not s.rolled_to(TOMORROW).is_halted


class TestRealizedTracking:
    def test_이익으로_마감하면_연속손실이_초기화된다(self, state: RiskState) -> None:
        s = state.with_realized(-10_000, closed_trade=True)
        assert s.consecutive_losses == 1
        s = s.with_realized(+20_000, closed_trade=True)
        assert s.consecutive_losses == 0

    def test_분할익절_중간체결은_연속손실을_세지_않는다(self, state: RiskState) -> None:
        """1단 익절만 하고 아직 포지션이 남아 있으면 매매가 끝난 게 아니다."""
        s = state.with_realized(-5_000, closed_trade=False)
        assert s.consecutive_losses == 0
        assert s.realized_krw_today == -5_000

    def test_손익은_누적된다(self, state: RiskState) -> None:
        s = state.with_realized(10_000, closed_trade=False)
        s = s.with_realized(-3_000, closed_trade=True)
        assert s.realized_krw_today == pytest.approx(7_000)


class TestEntryGuard:
    def test_한도_내에서는_허용된다(self, state: RiskState, rcfg: RiskConfig) -> None:
        assert (
            check_entry_allowed(state, rcfg, planned_notional_krw=1_000_000, open_positions=0)
            is None
        )

    def test_수동정지면_차단된다(self, state: RiskState, rcfg: RiskConfig) -> None:
        from dataclasses import replace

        s = replace(state, manual_halt=True)
        reason = check_entry_allowed(s, rcfg, planned_notional_krw=1, open_positions=0)
        assert reason is not None
        assert "수동 정지" in reason

    def test_당일_손실_한도_초과시_차단된다(self, state: RiskState, rcfg: RiskConfig) -> None:
        s = state.with_realized(-300_000, closed_trade=True)
        reason = check_entry_allowed(s, rcfg, planned_notional_krw=1, open_positions=0)
        assert reason is not None
        assert "손실 한도" in reason

    def test_손실_한도_직전에는_허용된다(self, state: RiskState, rcfg: RiskConfig) -> None:
        s = state.with_realized(-299_999, closed_trade=True)
        assert check_entry_allowed(s, rcfg, planned_notional_krw=1_000, open_positions=0) is None

    def test_연속손실_한도에서_차단된다(self, state: RiskState, rcfg: RiskConfig) -> None:
        s = state
        for _ in range(3):
            s = s.with_realized(-1_000, closed_trade=True)
        reason = check_entry_allowed(s, rcfg, planned_notional_krw=1, open_positions=0)
        assert reason is not None
        assert "연속 손실" in reason

    def test_당일_진입횟수_한도에서_차단된다(self, state: RiskState, rcfg: RiskConfig) -> None:
        s = state
        for _ in range(3):
            s = s.with_entry(100_000)
        reason = check_entry_allowed(s, rcfg, planned_notional_krw=1, open_positions=0)
        assert reason is not None
        assert "진입 횟수" in reason

    def test_이미_포지션이_있으면_차단된다(self, state: RiskState, rcfg: RiskConfig) -> None:
        reason = check_entry_allowed(state, rcfg, planned_notional_krw=1, open_positions=1)
        assert reason is not None
        assert "동시 보유" in reason

    def test_1회_투입_한도_초과시_차단된다(self, state: RiskState, rcfg: RiskConfig) -> None:
        reason = check_entry_allowed(state, rcfg, planned_notional_krw=5_000_001, open_positions=0)
        assert reason is not None
        assert "1회 투입" in reason

    def test_당일_투입_잔여를_넘으면_차단된다(self, state: RiskState, rcfg: RiskConfig) -> None:
        s = state.with_entry(2_500_000)
        reason = check_entry_allowed(s, rcfg, planned_notional_krw=600_000, open_positions=0)
        assert reason is not None
        assert "당일 투입 한도" in reason

    def test_킬스위치_발동_상태면_차단된다(self, state: RiskState, rcfg: RiskConfig) -> None:
        s = state.with_halt("테스트 사유")
        reason = check_entry_allowed(s, rcfg, planned_notional_krw=1, open_positions=0)
        assert reason is not None
        assert "킬스위치" in reason


class TestScaleInGuard:
    def test_한도_내에서는_허용된다(self, state: RiskState, rcfg: RiskConfig) -> None:
        assert check_scale_in_allowed(state, rcfg, planned_notional_krw=500_000) is None

    def test_진입횟수_한도는_추가매수를_막지_않는다(
        self, state: RiskState, rcfg: RiskConfig
    ) -> None:
        """이미 들어간 포지션에 계획대로 채우는 것은 새 진입이 아니다."""
        s = state
        for _ in range(3):
            s = s.with_entry(100_000)
        assert check_scale_in_allowed(s, rcfg, planned_notional_krw=100_000) is None

    def test_손실_한도에_걸리면_물타기를_막는다(self, state: RiskState, rcfg: RiskConfig) -> None:
        """여기가 뚫리면 킬스위치 전체가 무의미해진다."""
        s = state.with_realized(-300_000, closed_trade=True)
        reason = check_scale_in_allowed(s, rcfg, planned_notional_krw=1)
        assert reason is not None
        assert "추가 매수를 하지 않는다" in reason

    def test_당일_투입_한도를_넘으면_차단된다(self, state: RiskState, rcfg: RiskConfig) -> None:
        s = state.with_entry(2_900_000)
        reason = check_scale_in_allowed(s, rcfg, planned_notional_krw=200_000)
        assert reason is not None
        assert "당일 투입 한도" in reason

    def test_수동정지와_킬스위치가_추가매수도_막는다(
        self, state: RiskState, rcfg: RiskConfig
    ) -> None:
        from dataclasses import replace

        assert check_scale_in_allowed(
            replace(state, manual_halt=True), rcfg, planned_notional_krw=1
        )
        assert check_scale_in_allowed(state.with_halt("x"), rcfg, planned_notional_krw=1)


class TestHaltBookkeeping:
    def test_같은_사유는_중복_기록되지_않는다(self, state: RiskState) -> None:
        s = state.with_halt("사유A").with_halt("사유A")
        assert s.halt_reasons == ("사유A",)

    def test_다른_사유는_누적된다(self, state: RiskState) -> None:
        s = state.with_halt("사유A").with_halt("사유B")
        assert s.halt_reasons == ("사유A", "사유B")

    def test_투입금액과_진입횟수가_구분되어_집계된다(self, state: RiskState) -> None:
        s = state.with_entry(1_000_000).with_scale_in(500_000)
        assert s.notional_krw_today == pytest.approx(1_500_000)
        assert s.entries_today == 1  # 추가매수는 진입 횟수가 아니다


class TestCooldown:
    """연속 손실 한도는 퓨즈가 아니라 회로차단기여야 한다.

    영구 정지로 만들면 카운터를 되돌릴 방법이 없어 봇이 죽는다.
    이길 수 없으니 카운터가 줄지 않고, 카운터가 줄지 않으니 거래할 수 없다.
    실제로 이 프로젝트의 백테스트가 2025-01-30 에 그렇게 멈춰서
    이후 진입 신호 44건이 조용히 버려졌었다. 이 테스트들이 재발을 막는다.
    """

    def test_쿨다운이_연속손실_카운터를_리셋한다(self, state: RiskState) -> None:
        """리셋하지 않으면 쿨다운이 끝나자마자 다시 걸려 영구 정지가 된다."""
        s = state
        for _ in range(3):
            s = s.with_realized(-10_000, closed_trade=True)
        assert s.consecutive_losses == 3
        s = s.start_cooldown(5)
        assert s.consecutive_losses == 0
        assert s.cooldown_until is not None

    def test_쿨다운_중에는_진입이_차단된다(self, state: RiskState, rcfg: RiskConfig) -> None:
        s = state.start_cooldown(5)
        reason = check_entry_allowed(s, rcfg, planned_notional_krw=1, open_positions=0)
        assert reason is not None
        assert "쿨다운" in reason

    def test_쿨다운_중에는_추가매수도_차단된다(self, state: RiskState, rcfg: RiskConfig) -> None:
        s = state.start_cooldown(5)
        assert check_scale_in_allowed(s, rcfg, planned_notional_krw=1) is not None

    def test_쿨다운이_만료되면_스스로_풀린다(self, state: RiskState, rcfg: RiskConfig) -> None:
        """이것이 없으면 데드락이다. 반드시 자동으로 풀려야 한다."""
        s = state.start_cooldown(5)
        assert s.in_cooldown
        resumed = s.rolled_to(s.cooldown_until)
        assert not resumed.in_cooldown
        assert resumed.cooldown_until is None
        assert check_entry_allowed(resumed, rcfg, planned_notional_krw=1, open_positions=0) is None

    def test_쿨다운_만료_전에는_유지된다(self, state: RiskState) -> None:
        s = state.start_cooldown(5)
        mid = s.rolled_to(s.trade_date + dt.timedelta(days=1))
        assert mid.in_cooldown
        assert mid.cooldown_until == s.cooldown_until

    def test_쿨다운_일수가_0이면_카운터만_리셋한다(self, state: RiskState) -> None:
        s = state.with_realized(-1, closed_trade=True).start_cooldown(0)
        assert s.consecutive_losses == 0
        assert s.cooldown_until is None
        assert not s.in_cooldown

    def test_쿨다운도_halted로_집계된다(self, state: RiskState) -> None:
        assert state.start_cooldown(3).is_halted


class TestAddBusinessDays:
    def test_주말을_건너뛴다(self) -> None:
        friday = dt.date(2026, 8, 21)
        assert friday.weekday() == 4
        assert add_business_days(friday, 1) == dt.date(2026, 8, 24)  # 월요일
        assert add_business_days(friday, 5) == dt.date(2026, 8, 28)  # 다음 금요일

    def test_0이하면_그대로다(self) -> None:
        d = dt.date(2026, 8, 24)
        assert add_business_days(d, 0) == d
        assert add_business_days(d, -3) == d

    def test_결과는_항상_영업일이다(self) -> None:
        start = dt.date(2026, 8, 24)
        for n in range(1, 30):
            assert add_business_days(start, n).weekday() < 5
