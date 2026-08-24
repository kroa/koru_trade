"""리스크 한도와 킬스위치.

이 모듈의 존재 이유는 하나다. **전략이 아무리 좋아 보여도 계좌가 죽으면 끝**이다.
3배 레버리지 상품은 기초지수가 하루 -10% 면 -30% 다. 2019년 이후 KORU 는
연간 최대낙폭이 -43%~-83% 사이를 매년 기록했다. 한도 없이 돌리면 안 된다.

여기서 하는 검사는 전략 로직보다 **항상 우선**한다.
:func:`check_entry_allowed` 가 사유를 반환하면 진입 신호는 무조건 폐기된다.
반면 **청산은 절대 막지 않는다.** 킬스위치가 걸려도 보유 포지션은 정상적으로 정리된다.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, replace

from koru_trade.config import RiskConfig

__all__ = [
    "RiskState",
    "add_business_days",
    "check_entry_allowed",
    "check_scale_in_allowed",
]


@dataclass(frozen=True, slots=True)
class RiskState:
    """당일 리스크 집계 상태. 자정(미국 거래일 기준)마다 초기화된다."""

    trade_date: dt.date
    realized_krw_today: float = 0.0
    """당일 실현손익 누계. 음수면 손실."""

    notional_krw_today: float = 0.0
    """당일 신규 투입한 원화 금액 누계."""

    entries_today: int = 0
    """당일 신규 진입 횟수."""

    consecutive_losses: int = 0
    """연속 손실 마감 횟수. 이익으로 마감하면 0으로 초기화된다."""

    manual_halt: bool = False
    """운영자가 수동으로 건 정지. 코드가 자동 해제하지 않는다."""

    halt_reasons: tuple[str, ...] = field(default_factory=tuple)
    """발동한 킬스위치 사유 기록. 감사추적용."""

    cooldown_until: dt.date | None = None
    """연속 손실 한도에 걸려 쉬는 중이라면 재개 예정일.

    **회로차단기는 퓨즈가 아니라 리셋되는 것이어야 한다.**
    연속 손실 한도를 영구 정지로 만들면 카운터를 되돌릴 방법이 없어 봇이 죽는다.
    이길 수 없으니 카운터가 줄지 않고, 카운터가 줄지 않으니 거래할 수 없다.
    실제로 이 프로젝트의 백테스트가 2025-01-30 에 그렇게 멈춰서
    이후 1년 반의 진입 신호 44건이 전부 조용히 버려졌었다.
    """

    def rolled_to(self, new_date: dt.date) -> RiskState:
        """새 거래일로 넘어간 상태를 반환한다.

        일일 집계(손익·투입금액·진입횟수)와 당일 킬스위치 사유는 초기화한다.
        연속 손실 카운터와 수동 정지, 쿨다운은 날짜가 바뀌어도 유지된다
        (쿨다운은 만료되면 아래에서 스스로 풀린다).
        """
        if new_date <= self.trade_date:
            return self
        cooldown = self.cooldown_until
        if cooldown is not None and new_date >= cooldown:
            cooldown = None
        return RiskState(
            trade_date=new_date,
            realized_krw_today=0.0,
            notional_krw_today=0.0,
            entries_today=0,
            consecutive_losses=self.consecutive_losses,
            manual_halt=self.manual_halt,
            cooldown_until=cooldown,
            halt_reasons=(),
        )

    def with_entry(self, notional_krw: float) -> RiskState:
        """신규 진입을 기록한 상태를 반환한다."""
        return replace(
            self,
            notional_krw_today=self.notional_krw_today + notional_krw,
            entries_today=self.entries_today + 1,
        )

    def with_scale_in(self, notional_krw: float) -> RiskState:
        """추가 매수를 기록한 상태(진입 횟수는 늘지 않는다)."""
        return replace(self, notional_krw_today=self.notional_krw_today + notional_krw)

    def with_realized(self, pnl_krw: float, *, closed_trade: bool) -> RiskState:
        """실현손익을 반영한 상태를 반환한다.

        Args:
            pnl_krw: 실현손익(원). 음수면 손실.
            closed_trade: 이 실현으로 매매가 완결되었는지.
                연속 손실 카운터는 매매 완결 시에만 갱신한다.
                분할 익절 중간 체결마다 세면 의미가 없기 때문이다.
        """
        streak = self.consecutive_losses
        if closed_trade:
            streak = streak + 1 if pnl_krw < 0 else 0
        return replace(
            self,
            realized_krw_today=self.realized_krw_today + pnl_krw,
            consecutive_losses=streak,
        )

    def with_halt(self, reason: str) -> RiskState:
        """킬스위치 사유를 기록한 상태를 반환한다."""
        if reason in self.halt_reasons:
            return self
        return replace(self, halt_reasons=(*self.halt_reasons, reason))

    def start_cooldown(self, days: int) -> RiskState:
        """연속 손실 한도에 걸렸을 때 쿨다운을 시작한다.

        ``days`` 영업일 뒤로 재개일을 잡고 **연속 손실 카운터를 0으로 되돌린다.**
        카운터를 되돌리지 않으면 쿨다운이 끝나자마자 다시 걸려 영구 정지가 된다.

        Args:
            days: 쉬어갈 영업일 수. 0 이하면 아무것도 하지 않는다.
        """
        if days <= 0:
            return replace(self, consecutive_losses=0)
        return replace(
            self,
            consecutive_losses=0,
            cooldown_until=add_business_days(self.trade_date, days),
        )

    @property
    def in_cooldown(self) -> bool:
        """쿨다운 기간 중인지."""
        return self.cooldown_until is not None and self.trade_date < self.cooldown_until

    @property
    def is_halted(self) -> bool:
        return self.manual_halt or bool(self.halt_reasons) or self.in_cooldown


def add_business_days(start: dt.date, days: int) -> dt.date:
    """``start`` 에서 영업일 ``days`` 만큼 뒤의 날짜(주말 제외, 공휴일 무시).

    쿨다운을 달력일로 세면 금요일에 걸린 것과 화요일에 걸린 것이
    실제로 쉬는 거래일 수가 달라진다.
    """
    if days <= 0:
        return start
    cursor = start
    remaining = days
    while remaining > 0:
        cursor += dt.timedelta(days=1)
        if cursor.weekday() < 5:
            remaining -= 1
    return cursor


def check_entry_allowed(
    state: RiskState, cfg: RiskConfig, *, planned_notional_krw: float, open_positions: int
) -> str | None:
    """신규 진입이 허용되는지 검사한다.

    Args:
        state: 당일 리스크 상태.
        cfg: 리스크 한도 설정.
        planned_notional_krw: 이번 진입에서 투입할 예정 금액(전 차수 합).
        open_positions: 현재 보유 중인 포지션 수.

    Returns:
        차단 사유 문자열. 허용되면 None.
    """
    if state.manual_halt:
        return "운영자 수동 정지(manual_halt) 상태다"

    if state.halt_reasons:
        return f"킬스위치 발동 상태다: {', '.join(state.halt_reasons)}"

    if state.in_cooldown:
        return f"연속 손실 쿨다운 중이다. {state.cooldown_until} 부터 재개한다"

    if state.realized_krw_today <= -abs(cfg.daily_loss_limit_krw):
        return (
            f"당일 손실 한도 초과: 실현 {state.realized_krw_today:,.0f}원 "
            f"<= 한도 -{abs(cfg.daily_loss_limit_krw):,.0f}원"
        )

    if state.consecutive_losses >= cfg.max_consecutive_losses:
        return (
            f"연속 손실 {state.consecutive_losses}회로 한도"
            f"({cfg.max_consecutive_losses}회)에 도달했다"
        )

    if state.entries_today >= cfg.max_trades_per_day:
        return (
            f"당일 진입 횟수 {state.entries_today}회로 한도({cfg.max_trades_per_day}회)에 도달했다"
        )

    if open_positions >= cfg.max_open_positions:
        return f"동시 보유 포지션 한도({cfg.max_open_positions}개)에 도달했다"

    if planned_notional_krw > cfg.max_position_krw:
        return (
            f"1회 투입 예정액 {planned_notional_krw:,.0f}원이 "
            f"한도 {cfg.max_position_krw:,.0f}원을 초과한다"
        )

    remaining = cfg.max_daily_notional_krw - state.notional_krw_today
    if planned_notional_krw > remaining:
        return (
            f"당일 투입 한도 잔여 {remaining:,.0f}원보다 "
            f"예정액 {planned_notional_krw:,.0f}원이 크다"
        )

    return None


def check_scale_in_allowed(
    state: RiskState, cfg: RiskConfig, *, planned_notional_krw: float
) -> str | None:
    """추가 분할 매수가 허용되는지 검사한다.

    진입과 달리 포지션 수·진입 횟수 한도는 보지 않는다(이미 들어간 포지션이므로).
    다만 **당일 손실 한도와 투입 한도는 그대로 적용**한다.
    손실 한도에 걸린 상태에서 물타기를 허용하면 킬스위치가 무의미해진다.

    Returns:
        차단 사유 문자열. 허용되면 None.
    """
    if state.manual_halt:
        return "운영자 수동 정지(manual_halt) 상태다"

    if state.halt_reasons:
        return f"킬스위치 발동 상태다: {', '.join(state.halt_reasons)}"

    if state.in_cooldown:
        return f"연속 손실 쿨다운 중이다. {state.cooldown_until} 부터 재개한다"

    if state.realized_krw_today <= -abs(cfg.daily_loss_limit_krw):
        return (
            f"당일 손실 한도 초과 상태에서는 추가 매수를 하지 않는다: "
            f"{state.realized_krw_today:,.0f}원"
        )

    remaining = cfg.max_daily_notional_krw - state.notional_krw_today
    if planned_notional_krw > remaining:
        return (
            f"당일 투입 한도 잔여 {remaining:,.0f}원보다 "
            f"추가매수 예정액 {planned_notional_krw:,.0f}원이 크다"
        )

    return None
