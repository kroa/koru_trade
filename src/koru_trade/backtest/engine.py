"""이벤트 기반 백테스트 엔진.

체결 모델의 원칙
----------------
백테스트가 거짓말을 하는 경로는 거의 전부 체결 모델에 있다. 여기서는 다음을 지킨다.

1. **미래를 보지 않는다.** 봉 ``i`` 의 판단에는 ``bars[:i+1]`` 만 넘긴다.
2. **진입은 다음 봉 시가에 체결한다.** 종가를 보고 그 종가에 사는 것은 불가능하다.
   지정가 주문의 실제 의미를 그대로 흉내낸다.
   매수 지정가 ``L`` 은 다음 봉 시가가 ``L`` 이하면 시가에(가격 개선),
   시가는 위인데 저가가 ``L`` 이하면 ``L`` 에, 둘 다 아니면 미체결로 취소한다.
3. **손절과 익절은 봉 내부에서 체결한다.** 봇은 장중에 계속 보고 있으므로
   가격이 손절선을 스치면 체결된다. 갭으로 손절선을 뛰어넘고 열리면 시가에 체결된다.
4. **같은 봉에서 손절과 익절이 모두 닿으면 손절이 먼저 일어났다고 가정한다.**
   실제 순서를 알 수 없으므로 불리한 쪽을 택한다. 이 가정 하나가
   낙관 편향의 상당 부분을 제거한다.
5. **모든 비용을 원화 실현액에 반영한다.** 수수료, SEC/TAF, 환전 스프레드, 슬리피지.

이 규칙들 때문에 결과는 대체로 실제보다 **나쁘게** 나온다. 의도한 것이다.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from koru_trade.config import StrategyConfig
from koru_trade.models import (
    Action,
    Bar,
    Decision,
    ExitReason,
    Lot,
    OrderSide,
    Position,
    TradeRecord,
)
from koru_trade.pnl import krw_cost, krw_proceeds, position_krw_return, position_required_price_usd
from koru_trade.risk import RiskState
from koru_trade.strategy import decide, stop_price_usd

logger = logging.getLogger(__name__)

__all__ = ["BacktestResult", "DecisionLog", "FillEvent", "run_backtest"]


@dataclass(frozen=True, slots=True)
class FillEvent:
    """백테스트 내에서 발생한 체결 1건."""

    ts: dt.datetime
    side: OrderSide
    qty: int
    price_usd: float
    fx_rate: float
    cash_delta_krw: float
    """현금 증감. 매수는 음수, 매도는 양수."""

    reason: str
    tranche_index: int | None = None
    exit_reason: ExitReason | None = None


@dataclass(frozen=True, slots=True)
class DecisionLog:
    """봉마다 남기는 판단 기록. 감사추적이자 디버깅 수단이다."""

    ts: dt.datetime
    action: Action
    qty: int
    price_usd: float
    fx_rate: float
    krw_return: float
    rationale: str


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """백테스트 실행 결과."""

    trades: tuple[TradeRecord, ...]
    fills: tuple[FillEvent, ...]
    decisions: tuple[DecisionLog, ...]
    equity_curve: tuple[tuple[dt.datetime, float], ...]
    """(시각, 원화 평가자산) 시계열. 평가자산은 지금 전량 청산했을 때의 원화다."""

    config: StrategyConfig
    initial_capital_krw: float
    bars_processed: int
    unfilled_orders: int
    """지정가 미체결로 취소된 주문 수. 너무 많으면 체결 가정이 비현실적이라는 신호다."""

    @property
    def final_equity_krw(self) -> float:
        return self.equity_curve[-1][1] if self.equity_curve else self.initial_capital_krw

    @property
    def total_return(self) -> float:
        if self.initial_capital_krw <= 0:
            return 0.0
        return self.final_equity_krw / self.initial_capital_krw - 1.0


@dataclass
class _EngineState:
    """엔진 내부 가변 상태. 외부로 새어나가지 않는다."""

    cash_krw: float
    position: Position
    risk: RiskState
    pending: Decision | None = None
    pending_ts: dt.datetime | None = None
    lot_seq: int = 0
    trade_open_qty: int = 0
    trade_cost_krw: float = 0.0
    trade_proceeds_krw: float = 0.0
    trade_exit_notional_usd: float = 0.0
    trade_exit_qty: int = 0
    trade_exit_fx_notional: float = 0.0
    trade_entry_notional_usd: float = 0.0
    trade_entry_qty: int = 0
    trade_entry_fx_notional: float = 0.0
    trade_opened_at: dt.datetime | None = None
    trade_reasons: list[ExitReason] = field(default_factory=list)
    trade_tranches: int = 0


def run_backtest(
    bars: Sequence[Bar],
    cfg: StrategyConfig,
    *,
    initial_capital_krw: float | None = None,
    start_index: int | None = None,
) -> BacktestResult:
    """봉 시계열에 전략을 적용해 백테스트한다.

    Args:
        bars: 시간 오름차순 봉. ``fx_rate`` 가 각 봉에 채워져 있어야 한다.
        cfg: 전략 설정.
        initial_capital_krw: 초기 원화 자산. None 이면 ``cfg.capital_krw`` 의 2배를 쓴다
            (분할매수 전액 투입 후에도 여유가 남도록).
        start_index: 매매를 시작할 봉 인덱스. None 이면 ``cfg.warmup_bars``.

    Returns:
        :class:`BacktestResult`.
    """
    if len(bars) < 2:
        raise ValueError(f"백테스트에는 최소 2개의 봉이 필요하다: {len(bars)}개")
    _assert_sorted(bars)

    capital = initial_capital_krw if initial_capital_krw is not None else cfg.capital_krw * 2
    if capital <= 0:
        raise ValueError(f"초기 자산은 0보다 커야 한다: {capital}")

    begin = cfg.warmup_bars if start_index is None else start_index
    begin = max(1, min(begin, len(bars) - 1))

    st = _EngineState(
        cash_krw=capital,
        position=Position(cfg.symbol),
        risk=RiskState(trade_date=bars[begin].ts.date()),
    )
    trades: list[TradeRecord] = []
    fills: list[FillEvent] = []
    logs: list[DecisionLog] = []
    equity: list[tuple[dt.datetime, float]] = []
    unfilled = 0

    for i in range(begin, len(bars)):
        bar = bars[i]
        st.risk = st.risk.rolled_to(bar.ts.date())

        # --- (a) 직전 봉에서 낸 지정가 주문을 이 봉 시가에 체결 시도 ---------
        if st.pending is not None:
            filled = _try_fill_pending(st, bar, cfg, fills)
            if not filled:
                unfilled += 1
            st.pending = None
            st.pending_ts = None

        acted_this_bar = False

        # --- (b) 봉 내부 청산 검사 (손절 우선) -----------------------------
        if st.position.is_open:
            acted_this_bar = _intrabar_exits(st, bar, cfg, fills, trades)

        # --- (c) 최고 수익률 갱신 (트레일링 스톱 기준) ---------------------
        if st.position.is_open:
            high_r = position_krw_return(st.position, bar.high, bar.fx_rate, cfg.cost)
            if high_r > st.position.peak_krw_return:
                st.position = replace(st.position, peak_krw_return=high_r)

        # --- (d) 종가 기준 판단 -------------------------------------------
        window = bars[: i + 1]
        decision = decide(window, st.position, st.risk, cfg, now=bar.ts)
        krw_r = (
            position_krw_return(st.position, bar.close, bar.fx_rate, cfg.cost)
            if st.position.is_open
            else 0.0
        )
        logs.append(
            DecisionLog(
                ts=bar.ts,
                action=decision.action,
                qty=decision.qty,
                price_usd=bar.close,
                fx_rate=bar.fx_rate,
                krw_return=krw_r,
                rationale=decision.rationale,
            )
        )

        if not acted_this_bar:
            if decision.action is Action.EXIT and st.position.is_open:
                # 타임스톱·트레일링·본전스톱은 종가에 체결한다.
                _execute_sell(
                    st,
                    bar.ts,
                    qty=decision.qty,
                    price_usd=_slip(bar.close, cfg, buying=False),
                    fx=bar.fx_rate,
                    cfg=cfg,
                    reason=decision.reason or ExitReason.REGIME_EXIT,
                    label=decision.rationale,
                    fills=fills,
                    trades=trades,
                )
            elif decision.action is Action.TAKE_PROFIT and st.position.is_open:
                _apply_take_profit(st, bar.ts, decision, bar.close, bar.fx_rate, cfg, fills, trades)
            elif decision.action in (Action.ENTER, Action.SCALE_IN):
                # 다음 봉 시가에 지정가로 체결을 시도한다.
                st.pending = decision
                st.pending_ts = bar.ts

        equity.append((bar.ts, _equity(st, bar, cfg)))

    # --- 마지막 봉에서 잔여 포지션 강제 청산 -------------------------------
    if st.position.is_open:
        last = bars[-1]
        _execute_sell(
            st,
            last.ts,
            qty=st.position.qty,
            price_usd=_slip(last.close, cfg, buying=False),
            fx=last.fx_rate,
            cfg=cfg,
            reason=ExitReason.END_OF_BACKTEST,
            label="백테스트 종료로 잔여 포지션을 청산했다",
            fills=fills,
            trades=trades,
        )
        if equity:
            equity[-1] = (last.ts, st.cash_krw)

    return BacktestResult(
        trades=tuple(trades),
        fills=tuple(fills),
        decisions=tuple(logs),
        equity_curve=tuple(equity),
        config=cfg,
        initial_capital_krw=capital,
        bars_processed=len(bars) - begin,
        unfilled_orders=unfilled,
    )


# ---------------------------------------------------------------------------
# 체결 시뮬레이션
# ---------------------------------------------------------------------------


def _try_fill_pending(
    st: _EngineState, bar: Bar, cfg: StrategyConfig, fills: list[FillEvent]
) -> bool:
    """직전 봉에서 낸 매수 지정가를 이 봉에 체결 시도한다.

    Returns:
        체결되면 True, 미체결 취소면 False.
    """
    pending = st.pending
    if pending is None or pending.limit_price_usd is None:
        return False
    limit = pending.limit_price_usd

    if bar.open <= limit:
        price = bar.open  # 시가가 지정가보다 유리하다: 가격 개선
    elif bar.low <= limit:
        price = limit
    else:
        logger.debug("지정가 %.2f 미체결(저가 %.2f)", limit, bar.low)
        return False

    qty = pending.qty
    cost = krw_cost(qty, price, bar.fx_rate, cfg.cost)
    if cost > st.cash_krw:
        # 현금이 모자라면 살 수 있는 만큼만 산다.
        unit = krw_cost(1, price, bar.fx_rate, cfg.cost)
        qty = int(st.cash_krw // unit)
        if qty < 1:
            logger.debug("현금 부족으로 매수 취소")
            return False
        cost = krw_cost(qty, price, bar.fx_rate, cfg.cost)

    is_entry = pending.action is Action.ENTER
    tranche = 0 if is_entry else st.position.tranche_count

    if is_entry:
        plan = pending.entry_plan
        st.position = Position(
            cfg.symbol,
            entry_atr_usd=plan.entry_atr_usd if plan else 0.0,
            planned_tranche_qty=plan.tranche_qty if plan else (qty,),
        )
        st.trade_opened_at = bar.ts
        st.trade_cost_krw = 0.0
        st.trade_proceeds_krw = 0.0
        st.trade_entry_notional_usd = 0.0
        st.trade_entry_qty = 0
        st.trade_entry_fx_notional = 0.0
        st.trade_exit_notional_usd = 0.0
        st.trade_exit_qty = 0
        st.trade_exit_fx_notional = 0.0
        st.trade_reasons = []
        st.trade_tranches = 0
        st.trade_open_qty = 0
        st.risk = st.risk.with_entry(cost)
    else:
        st.risk = st.risk.with_scale_in(cost)

    st.lot_seq += 1
    lot = Lot(
        lot_id=f"L{st.lot_seq:05d}",
        qty=qty,
        price_usd=price,
        fx_rate=bar.fx_rate,
        cost_krw=cost,
        opened_at=bar.ts,
        tranche_index=tranche,
    )
    st.position = st.position.with_lot(lot)
    st.cash_krw -= cost
    st.trade_cost_krw += cost
    st.trade_entry_notional_usd += qty * price
    st.trade_entry_fx_notional += qty * price * bar.fx_rate
    st.trade_entry_qty += qty
    st.trade_tranches += 1
    st.trade_open_qty = max(st.trade_open_qty, st.position.qty)

    fills.append(
        FillEvent(
            ts=bar.ts,
            side=OrderSide.BUY,
            qty=qty,
            price_usd=price,
            fx_rate=bar.fx_rate,
            cash_delta_krw=-cost,
            reason=("진입" if is_entry else f"{tranche + 1}차 분할매수"),
            tranche_index=tranche,
        )
    )
    return True


def _intrabar_exits(
    st: _EngineState,
    bar: Bar,
    cfg: StrategyConfig,
    fills: list[FillEvent],
    trades: list[TradeRecord],
) -> bool:
    """봉 내부에서 손절과 익절을 검사한다.

    손절을 먼저 본다. 같은 봉에서 둘 다 닿았을 때 어느 쪽이 먼저인지 알 수 없으므로
    항상 불리한 쪽(손절)을 택한다.

    Returns:
        이 봉에서 체결이 발생했으면 True.
    """
    pos = st.position

    # (1) USD 손절선 --------------------------------------------------------
    stop = stop_price_usd(pos, cfg)
    # (2) 원화 하드스톱을 USD 가격으로 환산 ---------------------------------
    krw_stop_price = position_required_price_usd(
        pos, bar.fx_rate, cfg.hard_stop_krw_return, cfg.cost
    )

    trigger, reason = (
        (stop, ExitReason.HARD_STOP_USD)
        if stop >= krw_stop_price
        else (krw_stop_price, ExitReason.HARD_STOP_KRW)
    )

    if trigger > 0 and bar.low <= trigger:
        # 갭으로 손절선 아래에서 열렸으면 시가가 실제 체결가다.
        fill_price = min(bar.open, trigger)
        _execute_sell(
            st,
            bar.ts,
            qty=pos.qty,
            price_usd=_slip(fill_price, cfg, buying=False),
            fx=bar.fx_rate,
            cfg=cfg,
            reason=reason,
            label=(
                f"봉 내부 손절 체결: 저가 ${bar.low:.2f} <= 손절선 ${trigger:.2f} "
                f"({'USD' if reason is ExitReason.HARD_STOP_USD else '원화'} 기준)"
            ),
            fills=fills,
            trades=trades,
        )
        return True

    # (3) 익절 계단 ---------------------------------------------------------
    triggered: list[int] = []
    fill_price = 0.0
    for idx, step in enumerate(cfg.take_profit):
        if idx in st.position.tp_levels_hit:
            continue
        need = position_required_price_usd(st.position, bar.fx_rate, step.krw_return, cfg.cost)
        if bar.high >= need:
            triggered.append(idx)
            fill_price = max(fill_price, need)
    if not triggered:
        return False

    # 갭 상승으로 시가가 이미 필요가격 위면 시가에 체결된다(유리한 방향).
    exec_price = max(fill_price, bar.open) if bar.open > fill_price else fill_price
    after = set(st.position.tp_levels_hit) | set(triggered)
    if len(after) >= len(cfg.take_profit):
        qty = st.position.qty
    else:
        frac = sum(cfg.take_profit[i].sell_fraction for i in triggered)
        qty = min(st.position.qty, max(1, round(st.position.ladder_base_qty * frac)))

    levels = ", ".join(f"+{cfg.take_profit[i].krw_return:.0%}" for i in triggered)
    st.position = replace(st.position, tp_levels_hit=frozenset(after))
    _execute_sell(
        st,
        bar.ts,
        qty=qty,
        price_usd=_slip(exec_price, cfg, buying=False),
        fx=bar.fx_rate,
        cfg=cfg,
        reason=ExitReason.TAKE_PROFIT_LADDER,
        label=f"봉 내부 분할 익절 [{levels}] {qty}주 (체결 ${exec_price:.2f})",
        fills=fills,
        trades=trades,
    )
    return True


def _apply_take_profit(
    st: _EngineState,
    ts: dt.datetime,
    decision: Decision,
    price: float,
    fx: float,
    cfg: StrategyConfig,
    fills: list[FillEvent],
    trades: list[TradeRecord],
) -> None:
    """종가 기준으로 판정된 익절을 체결한다(봉 내부에서 놓친 경우의 안전망)."""
    after = set(st.position.tp_levels_hit) | set(decision.tp_levels)
    st.position = replace(st.position, tp_levels_hit=frozenset(after))
    _execute_sell(
        st,
        ts,
        qty=decision.qty,
        price_usd=_slip(price, cfg, buying=False),
        fx=fx,
        cfg=cfg,
        reason=ExitReason.TAKE_PROFIT_LADDER,
        label=decision.rationale,
        fills=fills,
        trades=trades,
    )


def _execute_sell(
    st: _EngineState,
    ts: dt.datetime,
    *,
    qty: int,
    price_usd: float,
    fx: float,
    cfg: StrategyConfig,
    reason: ExitReason,
    label: str,
    fills: list[FillEvent],
    trades: list[TradeRecord],
) -> None:
    """매도를 체결하고 포지션·현금·실현손익을 갱신한다."""
    qty = min(qty, st.position.qty)
    if qty <= 0:
        return

    proceeds = krw_proceeds(qty, price_usd, fx, cfg.cost)
    new_pos, removed_cost = st.position.reduced(qty)
    realized = proceeds - removed_cost

    st.position = replace(new_pos, realized_krw=st.position.realized_krw + realized)
    st.cash_krw += proceeds
    st.trade_proceeds_krw += proceeds
    st.trade_exit_notional_usd += qty * price_usd
    st.trade_exit_fx_notional += qty * price_usd * fx
    st.trade_exit_qty += qty
    if reason not in st.trade_reasons:
        st.trade_reasons.append(reason)

    fills.append(
        FillEvent(
            ts=ts,
            side=OrderSide.SELL,
            qty=qty,
            price_usd=price_usd,
            fx_rate=fx,
            cash_delta_krw=proceeds,
            reason=label,
            exit_reason=reason,
        )
    )

    if st.position.is_open:
        st.risk = st.risk.with_realized(realized, closed_trade=False)
        return

    # 매매 완결 -------------------------------------------------------------
    st.risk = st.risk.with_realized(realized, closed_trade=True)
    opened = st.trade_opened_at or ts
    entry_qty = max(1, st.trade_entry_qty)
    exit_qty = max(1, st.trade_exit_qty)
    trades.append(
        TradeRecord(
            symbol=cfg.symbol,
            opened_at=opened,
            closed_at=ts,
            tranches=st.trade_tranches,
            max_qty=st.trade_open_qty,
            entry_avg_price_usd=st.trade_entry_notional_usd / entry_qty,
            exit_avg_price_usd=st.trade_exit_notional_usd / exit_qty,
            entry_avg_fx=(
                st.trade_entry_fx_notional / st.trade_entry_notional_usd
                if st.trade_entry_notional_usd
                else 0.0
            ),
            exit_avg_fx=(
                st.trade_exit_fx_notional / st.trade_exit_notional_usd
                if st.trade_exit_notional_usd
                else 0.0
            ),
            cost_krw=st.trade_cost_krw,
            proceeds_krw=st.trade_proceeds_krw,
            exit_reasons=tuple(st.trade_reasons),
        )
    )
    st.position = Position(cfg.symbol)
    st.trade_opened_at = None

    daily = st.risk.realized_krw_today
    if daily <= -abs(cfg.risk.daily_loss_limit_krw):
        # 당일 손실 한도는 날짜가 바뀌면 자동으로 풀린다.
        st.risk = st.risk.with_halt(f"당일 손실 {daily:,.0f}원으로 한도 도달")
    if st.risk.consecutive_losses >= cfg.risk.max_consecutive_losses:
        # 연속 손실은 쿨다운으로 처리한다. 영구 정지로 두면 카운터를 되돌릴
        # 방법이 없어 봇이 죽는다(이길 수 없으니 카운터가 줄지 않는다).
        st.risk = st.risk.start_cooldown(cfg.risk.loss_cooldown_days)


def _slip(price: float, cfg: StrategyConfig, *, buying: bool) -> float:
    """슬리피지를 반영한 체결가. 항상 우리에게 불리한 방향으로 민다."""
    rate = cfg.cost.slippage_rate
    return price * (1.0 + rate) if buying else price * (1.0 - rate)


def _equity(st: _EngineState, bar: Bar, cfg: StrategyConfig) -> float:
    """현재 원화 평가자산. 보유분은 지금 청산했을 때 손에 쥐는 금액으로 평가한다."""
    if not st.position.is_open:
        return st.cash_krw
    mark = krw_proceeds(st.position.qty, bar.close, bar.fx_rate, cfg.cost)
    return st.cash_krw + mark


def _assert_sorted(bars: Sequence[Bar]) -> None:
    """봉이 시간 오름차순인지 확인한다. 정렬이 깨지면 백테스트 전체가 무의미해진다."""
    for i in range(1, len(bars)):
        if bars[i].ts <= bars[i - 1].ts:
            raise ValueError(
                f"봉이 시간 오름차순이 아니다: index {i - 1} ({bars[i - 1].ts}) >= "
                f"index {i} ({bars[i].ts})"
            )
