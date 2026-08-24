"""진입 게이트 — "지금 들어가도 되는가" 판정.

세 가지 서로 다른 증거를 모아 하나의 판정을 낸다.

1. **규칙 검사** — :func:`~koru_trade.strategy.evaluate_entry` 의 진입 필터.
   현재 시점이 전략이 허용하는 상태인가?
2. **레짐 진단** — 변동성, 레버리지 감쇠 추정치, 추세, 환율 방향.
   지금이 어떤 시장인가?
3. **조건부 기저율** — 과거에 **지금과 비슷한 레짐**에서 진입했다면 어떻게 끝났는가.
   :mod:`koru_trade.backtest.pathsim` 의 전방 시뮬레이션 표본을 조건부로 걸러낸다.

셋 중 하나라도 명백히 나쁘면 진입하지 않는다.
특히 3번은 "규칙은 통과했는데 실제로는 지는 상황" 을 잡아내는 마지막 방어선이다.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from koru_trade import indicators as ind
from koru_trade.backtest.pathsim import (
    ForwardOutcome,
    OutcomeStats,
    scan_forward_outcomes,
    summarize_outcomes,
)
from koru_trade.config import StrategyConfig
from koru_trade.models import Bar
from koru_trade.pnl import cost_factor, required_sell_price_usd
from koru_trade.strategy import EntrySignal, evaluate_entry, plan_entry

__all__ = ["EntryGateReport", "RegimeSnapshot", "Verdict", "evaluate_gate"]


@dataclass(frozen=True, slots=True)
class RegimeSnapshot:
    """현재 시장 상태 진단."""

    ts: dt.datetime
    price_usd: float
    fx_rate: float
    krw_price: float
    atr_usd: float
    atr_pct: float
    realized_vol_20: float
    realized_vol_60: float
    decay_annual: float
    """레버리지 변동성 감쇠 추정(연율, 음수). 이 값만큼은 가만히 있어도 녹는다."""

    decay_per_day: float
    rsi: float
    adx: float
    ema_fast: float
    ema_slow: float
    dist_from_high_20: float
    dist_from_low_20: float
    fx_change_20: float
    fx_percentile: float
    avg_dollar_volume: float
    price_needed_tp1: float
    """원화 +5% 를 달성하는 데 필요한 USD 가격(현재 환율 기준)."""

    price_needed_tp_last: float
    """마지막 익절 계단(원화 +20%)에 필요한 USD 가격."""

    def format_table(self) -> str:
        rows = [
            ("기준 시각", self.ts.strftime("%Y-%m-%d")),
            ("현재가", f"${self.price_usd:.2f}"),
            ("환율(USD/KRW)", f"{self.fx_rate:,.1f}"),
            ("원화 환산 주당", f"{self.krw_price:,.0f}원"),
            ("ATR(14)", f"${self.atr_usd:.2f} (종가의 {self.atr_pct:.2%})"),
            ("실현변동성 20/60일", f"{self.realized_vol_20:.1%} / {self.realized_vol_60:.1%}"),
            ("레버리지 감쇠 추정", f"연 {self.decay_annual:.1%} (일 {self.decay_per_day:.3%})"),
            ("RSI(14) / ADX(14)", f"{self.rsi:.1f} / {self.adx:.1f}"),
            ("EMA 빠름/느림", f"${self.ema_fast:.2f} / ${self.ema_slow:.2f}"),
            ("20일 고점 대비", f"{self.dist_from_high_20:+.1%}"),
            ("20일 저점 대비", f"{self.dist_from_low_20:+.1%}"),
            ("환율 20일 변화", f"{self.fx_change_20:+.2%}"),
            ("환율 이력 백분위", f"{self.fx_percentile:.0%}"),
            ("20일 평균 거래대금", f"${self.avg_dollar_volume:,.0f}"),
            (
                "원화 +5% 필요가격",
                f"${self.price_needed_tp1:.2f} "
                f"(현재가 대비 {self.price_needed_tp1 / self.price_usd - 1:+.2%})",
            ),
            (
                "원화 +20% 필요가격",
                f"${self.price_needed_tp_last:.2f} "
                f"(현재가 대비 {self.price_needed_tp_last / self.price_usd - 1:+.2%})",
            ),
        ]
        width = max(len(k) for k, _ in rows)
        return "\n".join(f"    {k.ljust(width)}  {v}" for k, v in rows)


class Verdict(str, Enum):
    """최종 판정."""

    ENTER = "ENTER"
    """모든 근거가 진입을 지지한다."""

    WAIT = "WAIT"
    """지금은 아니다. 조건이 갖춰지면 다시 본다."""

    AVOID = "AVOID"
    """레짐 자체가 이 전략과 맞지 않는다. 파라미터를 손봐도 나아지지 않는다."""


@dataclass(frozen=True, slots=True)
class EntryGateReport:
    """진입 게이트 종합 리포트."""

    snapshot: RegimeSnapshot
    signal: EntrySignal
    unconditional: OutcomeStats
    conditional: OutcomeStats
    recent: OutcomeStats
    verdict: Verdict
    reasons: tuple[str, ...]
    planned_qty: tuple[int, ...]
    planned_notional_krw: float
    stop_price_usd: float

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ENTER

    def format_report(self) -> str:
        """콘솔 출력용 전체 리포트."""
        lines: list[str] = []
        lines.append("=" * 74)
        lines.append(f"  KORU 진입 판정 리포트  ({self.snapshot.ts:%Y-%m-%d} 기준)")
        lines.append("=" * 74)
        lines.append("")
        lines.append("[1] 시장 레짐 진단")
        lines.append(self.snapshot.format_table())
        lines.append("")
        lines.append("[2] 진입 규칙 검사")
        for c in self.signal.checks:
            lines.append(f"    [{c.mark}] {c.name}")
            lines.append(f"           {c.detail}")
        lines.append("")
        lines.append(f"    -> {self.signal.summary}")
        lines.append("")
        lines.append("[3] 과거 동일 설계 진입 결과 (전방 시뮬레이션)")
        for stats in (self.unconditional, self.recent, self.conditional):
            lines.append(f"  - {stats.label}")
            if stats.n == 0:
                lines.append("      표본 없음")
            else:
                lines.append(stats.format_table())
            lines.append("")
        lines.append("[4] 진입 계획 (참고)")
        if self.planned_qty:
            lines.append(f"    분할 매수 수량   {self.planned_qty} (총 {sum(self.planned_qty)}주)")
            lines.append(f"    투입 예정 원화   {self.planned_notional_krw:,.0f}원")
            lines.append(f"    손절선           ${self.stop_price_usd:.2f}")
        else:
            lines.append("    자본으로 1주도 매수할 수 없다")
        lines.append("")
        lines.append("=" * 74)
        lines.append(f"  최종 판정: {self.verdict.value}")
        lines.append("=" * 74)
        for r in self.reasons:
            lines.append(f"  - {r}")
        return "\n".join(lines)


def evaluate_gate(
    bars: Sequence[Bar],
    cfg: StrategyConfig,
    *,
    outcomes: Sequence[ForwardOutcome] | None = None,
    recent_days: int = 120,
    min_conditional_samples: int = 25,
) -> EntryGateReport:
    """현재 시점의 진입 가부를 종합 판정한다.

    Args:
        bars: 시간 오름차순 봉. 마지막 봉이 "지금" 이다.
        cfg: 전략 설정.
        outcomes: 미리 계산한 전방 시뮬레이션 표본. None 이면 여기서 계산한다.
        recent_days: "최근 구간" 표본으로 볼 봉 수.
        min_conditional_samples: 조건부 표본이 이보다 적으면 신뢰하지 않는다.

    Returns:
        :class:`EntryGateReport`.
    """
    if len(bars) < cfg.warmup_bars:
        raise ValueError(f"판정에는 최소 {cfg.warmup_bars}개의 봉이 필요하다: {len(bars)}개")

    snap = _snapshot(bars, cfg)
    signal = evaluate_entry(bars, cfg)
    samples = tuple(outcomes) if outcomes is not None else scan_forward_outcomes(bars, cfg)

    uncond = summarize_outcomes(samples, f"전체 구간 무조건 진입 (표본 {len(samples)}건)")

    cutoff = bars[max(0, len(bars) - recent_days)].ts
    recent_samples = [o for o in samples if o.entry_ts >= cutoff]
    recent = summarize_outcomes(
        recent_samples, f"최근 {recent_days}봉 구간 (표본 {len(recent_samples)}건)"
    )

    cond_samples = _similar_regime(samples, snap)
    conditional = summarize_outcomes(
        cond_samples, f"현재와 유사한 레짐 (표본 {len(cond_samples)}건)"
    )

    plan = plan_entry(bars, cfg)
    verdict, reasons = _judge(snap, signal, uncond, conditional, cfg, min_conditional_samples)

    return EntryGateReport(
        snapshot=snap,
        signal=signal,
        unconditional=uncond,
        conditional=conditional,
        recent=recent,
        verdict=verdict,
        reasons=tuple(reasons),
        planned_qty=plan.tranche_qty if plan else (),
        planned_notional_krw=plan.total_notional_krw if plan else 0.0,
        stop_price_usd=plan.stop_price_usd if plan else 0.0,
    )


def _snapshot(bars: Sequence[Bar], cfg: StrategyConfig) -> RegimeSnapshot:
    """현재 레짐을 계산한다."""
    last = bars[-1]
    closes = [b.close for b in bars]
    window = bars[-20:]

    atr_v = ind.atr(bars, cfg.atr_period) or 0.0
    atr_p = ind.atr_pct(bars, cfg.atr_period) or 0.0
    vol20 = ind.realized_vol(closes, 20) or 0.0
    vol60 = ind.realized_vol(closes, 60) or 0.0
    decay = ind.leverage_decay_estimate(closes, 3.0, 20) or 0.0

    lb = cfg.fx_lookback_days
    fx_chg = last.fx_rate / bars[-(lb + 1)].fx_rate - 1.0 if len(bars) > lb else 0.0
    fx_hist = [b.fx_rate for b in bars]
    fx_pct = sum(1 for v in fx_hist if v < last.fx_rate) / len(fx_hist)

    hi20 = max(b.high for b in window)
    lo20 = min(b.low for b in window)

    c = cost_factor(cfg.cost)
    tp1 = cfg.take_profit[0].krw_return
    tp_last = cfg.take_profit[-1].krw_return

    return RegimeSnapshot(
        ts=last.ts,
        price_usd=last.close,
        fx_rate=last.fx_rate,
        krw_price=last.close * last.fx_rate,
        atr_usd=atr_v,
        atr_pct=atr_p,
        realized_vol_20=vol20,
        realized_vol_60=vol60,
        decay_annual=decay,
        decay_per_day=decay / 252.0,
        rsi=ind.rsi(closes, cfg.rsi_period) or 50.0,
        adx=ind.adx(bars, 14) or 0.0,
        ema_fast=ind.ema(closes, cfg.ema_fast) or last.close,
        ema_slow=ind.ema(closes, cfg.ema_slow) or last.close,
        dist_from_high_20=last.close / hi20 - 1.0 if hi20 else 0.0,
        dist_from_low_20=last.close / lo20 - 1.0 if lo20 else 0.0,
        fx_change_20=fx_chg,
        fx_percentile=fx_pct,
        avg_dollar_volume=sum(b.close * b.volume for b in window) / len(window),
        price_needed_tp1=required_sell_price_usd(
            last.close, last.fx_rate, last.fx_rate, tp1, cfg.cost
        )
        if c > 0
        else 0.0,
        price_needed_tp_last=required_sell_price_usd(
            last.close, last.fx_rate, last.fx_rate, tp_last, cfg.cost
        )
        if c > 0
        else 0.0,
    )


def _similar_regime(
    samples: Sequence[ForwardOutcome], snap: RegimeSnapshot
) -> list[ForwardOutcome]:
    """현재와 비슷한 레짐의 표본만 골라낸다.

    유사도 기준은 셋이다. 셋 다 만족하는 표본만 남긴다.

    * **변동성**: 진입 시점 ATR% 가 현재의 0.6~1.7배
    * **추세**: 정배열 여부가 현재와 일치
    * **환율**: 20일 환율 변화의 방향(약세/강세)이 현재와 일치

    표본이 너무 적어지면 변동성 조건만 남기고 완화한다.
    """
    cur_atr = snap.atr_pct
    trend_up_now = snap.price_usd > snap.ema_fast > snap.ema_slow
    fx_falling_now = snap.fx_change_20 < 0

    def vol_ok(o: ForwardOutcome) -> bool:
        if cur_atr <= 0:
            return True
        return 0.6 * cur_atr <= o.atr_pct_at_entry <= 1.7 * cur_atr

    strict = [
        o
        for o in samples
        if vol_ok(o)
        and o.trend_up_at_entry == trend_up_now
        and (o.fx_change_at_entry < 0) == fx_falling_now
    ]
    if len(strict) >= 25:
        return strict

    relaxed = [o for o in samples if vol_ok(o) and o.trend_up_at_entry == trend_up_now]
    if len(relaxed) >= 25:
        return relaxed
    return [o for o in samples if vol_ok(o)]


def _judge(
    snap: RegimeSnapshot,
    signal: EntrySignal,
    uncond: OutcomeStats,
    cond: OutcomeStats,
    cfg: StrategyConfig,
    min_samples: int,
) -> tuple[Verdict, list[str]]:
    """세 종류의 증거를 하나의 판정으로 합친다."""
    reasons: list[str] = []
    fatal = False

    # --- 규칙 검사 ---------------------------------------------------------
    if signal.allowed:
        reasons.append(f"진입 규칙 {len(signal.checks)}개를 모두 통과했다")
    else:
        for c in signal.blockers:
            reasons.append(f"[규칙 위반] {c.name}: {c.detail}")

    # --- 레짐 진단 ---------------------------------------------------------
    if snap.atr_pct > cfg.max_atr_pct:
        fatal = True
        reasons.append(
            f"[치명] 일평균 진폭(ATR)이 주가의 {snap.atr_pct:.1%} 로 "
            f"1단 익절 목표({cfg.take_profit[0].krw_return:.0%})보다 크다. "
            f"익절과 손절이 같은 날 무작위로 결정되므로 전략이 성립하지 않는다"
        )
    if snap.decay_annual < -0.60:
        fatal = True
        reasons.append(
            f"[치명] 레버리지 변동성 감쇠가 연 {snap.decay_annual:.0%}"
            f"(일 {snap.decay_per_day:.2%})로 추정된다. "
            f"방향이 맞아도 보유 자체가 비용이다"
        )
    elif snap.decay_annual < -0.30:
        reasons.append(
            f"[경고] 변동성 감쇠 연 {snap.decay_annual:.0%}. 보유 기간을 최대한 짧게 가져가야 한다"
        )

    if snap.fx_change_20 < cfg.max_fx_decline:
        reasons.append(
            f"[경고] 환율이 20일간 {snap.fx_change_20:+.2%} 하락했다. "
            f"원화 +{cfg.take_profit[0].krw_return:.0%} 달성에 필요한 주가 상승분이 "
            f"{snap.price_needed_tp1 / snap.price_usd - 1:+.2%} 로 늘어나 있다"
        )

    # --- 조건부 기저율 -----------------------------------------------------
    if cond.n < min_samples:
        reasons.append(
            f"[주의] 현재와 유사한 레짐의 과거 표본이 {cond.n}건뿐이다. 통계적 판단 근거가 약하다"
        )
    else:
        if cond.expectancy <= 0:
            fatal = True
            reasons.append(
                f"[치명] 유사 레짐 {cond.n}건의 기대 원화수익률이 "
                f"{cond.expectancy:+.2%} 로 음수다. 과거 이 상황에서 이 설계는 돈을 잃었다"
            )
        else:
            reasons.append(
                f"유사 레짐 {cond.n}건 기대값 {cond.expectancy:+.2%}, "
                f"승률 {cond.win_rate:.0%}, 손절률 {cond.stop_rate:.0%}"
            )
        if cond.stop_rate > 0.45:
            reasons.append(
                f"[경고] 유사 레짐에서 손절률이 {cond.stop_rate:.0%} 다. "
                f"손절폭이 현재 변동성에 비해 좁다"
            )

    if uncond.n >= min_samples and uncond.expectancy <= 0:
        fatal = True
        reasons.append(
            f"[치명] 전체 구간 기대값도 {uncond.expectancy:+.2%} 로 음수다. "
            f"레짐 문제가 아니라 설계 문제다"
        )

    # --- 종합 -------------------------------------------------------------
    if fatal:
        return Verdict.AVOID, reasons
    if not signal.allowed:
        return Verdict.WAIT, reasons
    return Verdict.ENTER, reasons
