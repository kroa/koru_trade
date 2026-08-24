"""전방 경로 시뮬레이션 (forward path simulation).

집계 백테스트는 "이 전략이 과거에 얼마를 벌었나" 를 알려주지만
사용자가 진짜 묻는 것은 **"지금 사면 어떻게 되나"** 다. 그 둘은 다른 질문이다.

이 모듈은 과거의 **모든 날짜**에 대해 "그날 샀다면 어떻게 됐을까" 를 전부 돌려
결과 분포를 만든다. 진입 필터를 통과했는지와 무관하게 전부 돌리므로,
"필터가 없었다면" 의 기저율(base rate)을 얻는다.
그 다음 현재와 비슷한 레짐의 표본만 골라내면
**"지금과 비슷한 상황에서 이 익절/손절 설계가 어떻게 끝났는가"** 가 나온다.

이것이 "지금 진입해도 문제없나" 에 대해 데이터로 답할 수 있는 가장 정직한 형태다.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from koru_trade import indicators as ind
from koru_trade.config import StrategyConfig
from koru_trade.models import Bar, Lot, Position
from koru_trade.pnl import (
    krw_cost,
    krw_proceeds,
    position_krw_return,
    position_required_price_usd,
)
from koru_trade.strategy import allocate_tranches, business_days_between, stop_price_usd

__all__ = [
    "ForwardOutcome",
    "OutcomeStats",
    "Terminal",
    "scan_forward_outcomes",
    "simulate_forward",
    "summarize_outcomes",
]


class Terminal(str, Enum):
    """전방 시뮬레이션이 끝난 사유."""

    TP_FULL = "TP_FULL"
    """익절 계단을 전부 소진했다(최고의 결과)."""

    STOP = "STOP"
    """손절선에 걸렸다."""

    TIMEOUT = "TIMEOUT"
    """익절도 손절도 없이 보유기간 상한에 도달해 청산했다."""

    TIMEOUT_PARTIAL = "TIMEOUT_PARTIAL"
    """일부 계단만 익절한 상태로 보유기간이 끝났다."""

    DATA_END = "DATA_END"
    """데이터가 끝나 강제 청산했다(표본에서 제외해야 한다)."""


@dataclass(frozen=True, slots=True)
class ForwardOutcome:
    """특정 날짜에 진입했을 때의 최종 결과."""

    entry_ts: dt.datetime
    entry_price_usd: float
    entry_fx: float
    exit_ts: dt.datetime
    exit_avg_price_usd: float
    exit_avg_fx: float
    krw_return: float
    """원화 실현수익률. 모든 비용 반영."""

    bars_held: int
    terminal: Terminal
    tp_levels_hit: int
    tranches_filled: int
    max_favorable_krw: float
    """보유 중 원화 수익률 최고치(MFE). 익절 문턱이 적절한지 판단하는 근거."""

    max_adverse_krw: float
    """보유 중 원화 수익률 최저치(MAE). 손절폭이 적절한지 판단하는 근거."""

    atr_pct_at_entry: float
    rsi_at_entry: float
    fx_change_at_entry: float
    trend_up_at_entry: bool

    @property
    def is_win(self) -> bool:
        return self.krw_return > 0


@dataclass(frozen=True, slots=True)
class OutcomeStats:
    """:class:`ForwardOutcome` 표본의 요약 통계."""

    n: int
    label: str
    win_rate: float
    mean_krw: float
    median_krw: float
    p05: float
    p25: float
    p75: float
    p95: float
    stdev_krw: float
    tp_full_rate: float
    stop_rate: float
    timeout_rate: float
    any_tp_rate: float
    """익절 계단을 하나라도 밟은 비율. 사용자 목표(원화 +5%) 도달률과 같다."""

    mean_mfe: float
    mean_mae: float
    mean_bars_held: float
    expectancy: float
    """기대 원화 수익률. 이 값이 음수면 진입하면 안 된다."""

    def format_table(self) -> str:
        rows = [
            ("표본 수", f"{self.n}건"),
            ("승률(원화 기준)", f"{self.win_rate:.1%}"),
            ("기대 원화수익률", f"{self.expectancy:+.2%}"),
            ("평균 / 중앙값", f"{self.mean_krw:+.2%} / {self.median_krw:+.2%}"),
            ("5% / 25% 분위", f"{self.p05:+.2%} / {self.p25:+.2%}"),
            ("75% / 95% 분위", f"{self.p75:+.2%} / {self.p95:+.2%}"),
            ("표준편차", f"{self.stdev_krw:.2%}"),
            ("1단 익절(+5%) 도달률", f"{self.any_tp_rate:.1%}"),
            ("전 계단 익절률", f"{self.tp_full_rate:.1%}"),
            ("손절률", f"{self.stop_rate:.1%}"),
            ("타임아웃률", f"{self.timeout_rate:.1%}"),
            ("평균 MFE / MAE", f"{self.mean_mfe:+.2%} / {self.mean_mae:+.2%}"),
            ("평균 보유 봉수", f"{self.mean_bars_held:.1f}"),
        ]
        width = max(len(k) for k, _ in rows)
        return "\n".join(f"    {k.ljust(width)}  {v}" for k, v in rows)


def simulate_forward(
    bars: Sequence[Bar], entry_index: int, cfg: StrategyConfig
) -> ForwardOutcome | None:
    """``entry_index`` 봉의 시가에 진입했다고 가정하고 끝까지 굴린다.

    진입 필터는 보지 않는다. 순수하게 "이 익절/손절 설계가 이 시점부터 어떻게 끝나는가"
    만 측정한다. 분할 매수, 원화 분할 익절, 손절, 타임스톱을 모두 :mod:`koru_trade.strategy`
    와 동일한 가격 계산 함수로 모델링한다.

    Args:
        bars: 전체 봉 시계열.
        entry_index: 진입할 봉의 인덱스. 이 봉의 **시가**에 1차 매수한다.
        cfg: 전략 설정.

    Returns:
        결과. 지표 계산에 필요한 이력이 부족하거나 1주도 살 수 없으면 None.
    """
    n = len(bars)
    if entry_index < 1 or entry_index >= n:
        return None

    history = bars[:entry_index]
    atr = ind.atr(history, cfg.atr_period)
    if atr is None or atr <= 0:
        return None

    entry_bar = bars[entry_index]
    entry_price = entry_bar.open * (1.0 + cfg.cost.slippage_rate)
    entry_fx = entry_bar.fx_rate

    unit = krw_cost(1, entry_price, entry_fx, cfg.cost)
    total_qty = int(cfg.capital_krw // unit)
    if total_qty < 1:
        return None
    plan = allocate_tranches(total_qty, [s.weight for s in cfg.scale_in])
    if not plan:
        return None

    pos = Position(
        cfg.symbol,
        entry_atr_usd=atr,
        planned_tranche_qty=plan,
    ).with_lot(
        Lot(
            lot_id="F0",
            qty=plan[0],
            price_usd=entry_price,
            fx_rate=entry_fx,
            cost_krw=krw_cost(plan[0], entry_price, entry_fx, cfg.cost),
            opened_at=entry_bar.ts,
            tranche_index=0,
        )
    )

    total_cost = pos.cost_krw
    total_proceeds = 0.0
    exit_notional_usd = 0.0
    exit_fx_notional = 0.0
    exit_qty = 0
    tp_hit: set[int] = set()
    mfe = 0.0
    mae = 0.0
    terminal = Terminal.DATA_END
    exit_ts = entry_bar.ts
    bars_held = 0

    for j in range(entry_index, n):
        bar = bars[j]
        bars_held = j - entry_index

        # --- 극단 추적 (MFE/MAE) -----------------------------------------
        mfe = max(mfe, position_krw_return(pos, bar.high, bar.fx_rate, cfg.cost))
        mae = min(mae, position_krw_return(pos, bar.low, bar.fx_rate, cfg.cost))

        # --- 손절 우선 ----------------------------------------------------
        usd_stop = stop_price_usd(pos, cfg)
        krw_stop = position_required_price_usd(pos, bar.fx_rate, cfg.hard_stop_krw_return, cfg.cost)
        trigger = max(usd_stop, krw_stop)
        if trigger > 0 and bar.low <= trigger and j > entry_index:
            fill = min(bar.open, trigger) * (1.0 - cfg.cost.slippage_rate)
            qty = pos.qty
            proceeds, pos = _sell(pos, qty, fill, bar.fx_rate, cfg)
            total_proceeds += proceeds
            exit_notional_usd += qty * fill
            exit_fx_notional += qty * fill * bar.fx_rate
            exit_qty += qty
            terminal = Terminal.STOP
            exit_ts = bar.ts
            break

        # --- 익절 계단 ----------------------------------------------------
        fired: list[int] = []
        need_price = 0.0
        for idx, step in enumerate(cfg.take_profit):
            if idx in tp_hit:
                continue
            need = position_required_price_usd(pos, bar.fx_rate, step.krw_return, cfg.cost)
            if bar.high >= need:
                fired.append(idx)
                need_price = max(need_price, need)
        if fired:
            exec_price = max(need_price, bar.open) if bar.open > need_price else need_price
            exec_price *= 1.0 - cfg.cost.slippage_rate
            tp_hit.update(fired)
            if len(tp_hit) >= len(cfg.take_profit):
                qty = pos.qty
            else:
                frac = sum(cfg.take_profit[i].sell_fraction for i in fired)
                qty = min(pos.qty, max(1, round(pos.ladder_base_qty * frac)))
            proceeds, pos = _sell(pos, qty, exec_price, bar.fx_rate, cfg)
            total_proceeds += proceeds
            exit_notional_usd += qty * exec_price
            exit_fx_notional += qty * exec_price * bar.fx_rate
            exit_qty += qty
            if not pos.is_open:
                terminal = Terminal.TP_FULL
                exit_ts = bar.ts
                break
            continue

        # --- 분할 매수 ----------------------------------------------------
        nxt = pos.tranche_count
        if not tp_hit and nxt < len(plan):
            trig = pos.first_entry_price_usd - atr * cfg.scale_in[nxt].atr_multiple
            reclaim_ok = (not cfg.scale_in_requires_reclaim) or bar.close >= bar.open
            if bar.low <= trig and trig > usd_stop and reclaim_ok:
                fill = min(bar.open, trig) * (1.0 + cfg.cost.slippage_rate)
                qty = plan[nxt]
                cost = krw_cost(qty, fill, bar.fx_rate, cfg.cost)
                pos = pos.with_lot(
                    Lot(
                        lot_id=f"F{nxt}",
                        qty=qty,
                        price_usd=fill,
                        fx_rate=bar.fx_rate,
                        cost_krw=cost,
                        opened_at=bar.ts,
                        tranche_index=nxt,
                    )
                )
                total_cost += cost

        # --- 타임 스톱 ----------------------------------------------------
        if business_days_between(entry_bar.ts, bar.ts) >= cfg.max_holding_days:
            fill = bar.close * (1.0 - cfg.cost.slippage_rate)
            qty = pos.qty
            proceeds, pos = _sell(pos, qty, fill, bar.fx_rate, cfg)
            total_proceeds += proceeds
            exit_notional_usd += qty * fill
            exit_fx_notional += qty * fill * bar.fx_rate
            exit_qty += qty
            terminal = Terminal.TIMEOUT_PARTIAL if tp_hit else Terminal.TIMEOUT
            exit_ts = bar.ts
            break
    else:
        # 루프가 끝까지 돌았다: 데이터 종료로 강제 청산
        if pos.is_open:
            last = bars[-1]
            fill = last.close * (1.0 - cfg.cost.slippage_rate)
            qty = pos.qty
            proceeds, pos = _sell(pos, qty, fill, last.fx_rate, cfg)
            total_proceeds += proceeds
            exit_notional_usd += qty * fill
            exit_fx_notional += qty * fill * last.fx_rate
            exit_qty += qty
            exit_ts = last.ts
        terminal = Terminal.DATA_END

    if total_cost <= 0 or exit_qty <= 0:
        return None

    atr_p = ind.atr_pct(history, cfg.atr_period) or 0.0
    rsi_v = ind.rsi([b.close for b in history], cfg.rsi_period) or 50.0
    lb = cfg.fx_lookback_days
    fx_chg = history[-1].fx_rate / history[-(lb + 1)].fx_rate - 1.0 if len(history) > lb else 0.0
    closes = [b.close for b in history]
    ef = ind.ema(closes, cfg.ema_fast)
    es = ind.ema(closes, cfg.ema_slow)
    trend_up = bool(ef and es and history[-1].close > ef > es)

    return ForwardOutcome(
        entry_ts=entry_bar.ts,
        entry_price_usd=entry_price,
        entry_fx=entry_fx,
        exit_ts=exit_ts,
        exit_avg_price_usd=exit_notional_usd / exit_qty,
        exit_avg_fx=exit_fx_notional / exit_notional_usd if exit_notional_usd else entry_fx,
        krw_return=total_proceeds / total_cost - 1.0,
        bars_held=bars_held,
        terminal=terminal,
        tp_levels_hit=len(tp_hit),
        tranches_filled=len(plan[: pos.tranche_count]) or 1,
        max_favorable_krw=mfe,
        max_adverse_krw=mae,
        atr_pct_at_entry=atr_p,
        rsi_at_entry=rsi_v,
        fx_change_at_entry=fx_chg,
        trend_up_at_entry=trend_up,
    )


def scan_forward_outcomes(
    bars: Sequence[Bar], cfg: StrategyConfig, *, stride: int = 1
) -> tuple[ForwardOutcome, ...]:
    """모든(또는 ``stride`` 간격의) 날짜에 대해 전방 시뮬레이션을 돌린다.

    데이터 끝에 걸려 강제 청산된 표본(:attr:`Terminal.DATA_END`)은 제외한다.
    보유기간이 잘려 결과가 왜곡되기 때문이다.
    """
    if stride < 1:
        raise ValueError(f"stride 는 1 이상이어야 한다: {stride}")
    start = cfg.warmup_bars
    out: list[ForwardOutcome] = []
    for i in range(start, len(bars), stride):
        res = simulate_forward(bars, i, cfg)
        if res is not None and res.terminal is not Terminal.DATA_END:
            out.append(res)
    return tuple(out)


def summarize_outcomes(outcomes: Sequence[ForwardOutcome], label: str = "전체") -> OutcomeStats:
    """표본을 요약 통계로 압축한다."""
    n = len(outcomes)
    if n == 0:
        return OutcomeStats(
            n=0,
            label=label,
            win_rate=0.0,
            mean_krw=0.0,
            median_krw=0.0,
            p05=0.0,
            p25=0.0,
            p75=0.0,
            p95=0.0,
            stdev_krw=0.0,
            tp_full_rate=0.0,
            stop_rate=0.0,
            timeout_rate=0.0,
            any_tp_rate=0.0,
            mean_mfe=0.0,
            mean_mae=0.0,
            mean_bars_held=0.0,
            expectancy=0.0,
        )
    rets = sorted(o.krw_return for o in outcomes)
    return OutcomeStats(
        n=n,
        label=label,
        win_rate=sum(1 for o in outcomes if o.is_win) / n,
        mean_krw=statistics.fmean(rets),
        median_krw=statistics.median(rets),
        p05=_quantile(rets, 0.05),
        p25=_quantile(rets, 0.25),
        p75=_quantile(rets, 0.75),
        p95=_quantile(rets, 0.95),
        stdev_krw=statistics.pstdev(rets) if n > 1 else 0.0,
        tp_full_rate=sum(1 for o in outcomes if o.terminal is Terminal.TP_FULL) / n,
        stop_rate=sum(1 for o in outcomes if o.terminal is Terminal.STOP) / n,
        timeout_rate=sum(
            1 for o in outcomes if o.terminal in (Terminal.TIMEOUT, Terminal.TIMEOUT_PARTIAL)
        )
        / n,
        any_tp_rate=sum(1 for o in outcomes if o.tp_levels_hit > 0) / n,
        mean_mfe=statistics.fmean(o.max_favorable_krw for o in outcomes),
        mean_mae=statistics.fmean(o.max_adverse_krw for o in outcomes),
        mean_bars_held=statistics.fmean(o.bars_held for o in outcomes),
        expectancy=statistics.fmean(rets),
    )


def _sell(
    pos: Position, qty: int, price: float, fx: float, cfg: StrategyConfig
) -> tuple[float, Position]:
    """``qty`` 주를 팔고 (원화 수취액, 남은 포지션) 을 반환한다."""
    qty = min(qty, pos.qty)
    proceeds = krw_proceeds(qty, price, fx, cfg.cost)
    new_pos, _ = pos.reduced(qty)
    return proceeds, new_pos


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    """선형 보간 분위수. 입력은 정렬되어 있어야 한다."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac
