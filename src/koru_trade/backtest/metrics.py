"""성과 지표.

지표는 전부 **원화 기준**이다. USD 기준 수익률로 잘 나온 전략이
원화로는 손실인 경우가 3배 레버리지 + 환율 환경에서는 흔하다.

승률만 보지 마라. 이 전략처럼 익절 문턱(+5%)이 손절폭(-10%)보다 좁으면
승률 70% 에도 손실이 날 수 있다. :attr:`PerformanceReport.profit_factor` 와
:attr:`PerformanceReport.expectancy_krw` 를 함께 봐야 한다.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass

from koru_trade.models import ExitReason, TradeRecord

__all__ = ["PerformanceReport", "compute_metrics"]

TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True, slots=True)
class PerformanceReport:
    """백테스트 성과 요약. 모든 금액은 원화."""

    n_trades: int
    n_wins: int
    n_losses: int
    win_rate: float
    total_return: float
    cagr: float
    max_drawdown: float
    max_drawdown_days: int
    sharpe: float
    sortino: float
    calmar: float
    profit_factor: float
    expectancy_krw: float
    avg_win_krw: float
    avg_loss_krw: float
    largest_win_krw: float
    largest_loss_krw: float
    avg_holding_days: float
    max_consecutive_losses: int
    avg_krw_return: float
    avg_usd_return: float
    avg_fx_return: float
    exit_reason_counts: dict[str, int]
    final_equity_krw: float
    initial_capital_krw: float

    @property
    def is_viable(self) -> bool:
        """실전에 올릴 만한 최소 기준을 통과했는지.

        기준은 보수적이다. 하나라도 못 넘으면 실거래로 넘어가면 안 된다.

        * 표본 20건 이상 (그 이하는 통계가 아니라 일화다)
        * Profit Factor 1.3 이상
        * 최대낙폭 25% 이내
        * 기대값이 양수
        """
        return (
            self.n_trades >= 20
            and self.profit_factor >= 1.3
            and self.max_drawdown >= -0.25
            and self.expectancy_krw > 0
        )

    @property
    def verdict(self) -> str:
        """사람이 읽는 한 줄 판정."""
        if self.n_trades == 0:
            return "매매가 한 건도 발생하지 않았다. 진입 필터가 너무 빡빡하거나 데이터가 짧다"
        problems: list[str] = []
        if self.n_trades < 20:
            problems.append(f"표본 {self.n_trades}건으로 통계적 유의성이 없다")
        if self.profit_factor < 1.3:
            problems.append(f"Profit Factor {self.profit_factor:.2f} < 1.3")
        if self.max_drawdown < -0.25:
            problems.append(f"최대낙폭 {self.max_drawdown:.1%} 가 -25% 를 넘는다")
        if self.expectancy_krw <= 0:
            problems.append(f"1회 기대값 {self.expectancy_krw:,.0f}원으로 음수다")
        if not problems:
            return "최소 기준 통과. 다만 모의투자 검증 없이 실거래에 올리지 마라"
        return "실거래 부적합 — " + " / ".join(problems)

    def format_table(self) -> str:
        """콘솔 출력용 표."""
        rows = [
            ("매매 횟수", f"{self.n_trades}건 (승 {self.n_wins} / 패 {self.n_losses})"),
            ("승률", f"{self.win_rate:.1%}"),
            ("총 수익률(원화)", f"{self.total_return:+.2%}"),
            ("CAGR", f"{self.cagr:+.2%}"),
            ("최대낙폭(MDD)", f"{self.max_drawdown:.2%} ({self.max_drawdown_days}일)"),
            ("Sharpe", f"{self.sharpe:.2f}"),
            ("Sortino", f"{self.sortino:.2f}"),
            ("Calmar", f"{self.calmar:.2f}"),
            ("Profit Factor", f"{self.profit_factor:.2f}"),
            ("1회 기대값", f"{self.expectancy_krw:,.0f}원"),
            ("평균 이익 / 손실", f"{self.avg_win_krw:,.0f}원 / {self.avg_loss_krw:,.0f}원"),
            ("최대 이익 / 손실", f"{self.largest_win_krw:,.0f}원 / {self.largest_loss_krw:,.0f}원"),
            ("평균 보유기간", f"{self.avg_holding_days:.1f}일"),
            ("최대 연속 손실", f"{self.max_consecutive_losses}회"),
            ("평균 원화수익률", f"{self.avg_krw_return:+.2%}"),
            ("  └ 주가 기여", f"{self.avg_usd_return:+.2%}"),
            ("  └ 환율 기여", f"{self.avg_fx_return:+.2%}"),
            (
                "최종 자산",
                f"{self.final_equity_krw:,.0f}원 (초기 {self.initial_capital_krw:,.0f}원)",
            ),
        ]
        width = max(len(k) for k, _ in rows)
        lines = [f"  {k.ljust(width)}  {v}" for k, v in rows]
        if self.exit_reason_counts:
            lines.append("  " + "-" * (width + 20))
            lines.append("  청산 사유 분포:")
            for reason, count in sorted(self.exit_reason_counts.items(), key=lambda kv: -kv[1]):
                lines.append(f"    {_reason_ko(reason).ljust(width)}  {count}건")
        return "\n".join(lines)


def compute_metrics(
    trades: Sequence[TradeRecord],
    equity_curve: Sequence[tuple[dt.datetime, float]],
    initial_capital_krw: float,
) -> PerformanceReport:
    """매매 기록과 자산곡선에서 성과 지표를 계산한다."""
    wins = [t for t in trades if t.is_win]
    losses = [t for t in trades if not t.is_win]
    n = len(trades)

    gross_profit = sum(t.pnl_krw for t in wins)
    gross_loss = abs(sum(t.pnl_krw for t in losses))
    net = gross_profit - gross_loss

    equity_values = [v for _, v in equity_curve]
    final_equity = equity_values[-1] if equity_values else initial_capital_krw
    total_return = final_equity / initial_capital_krw - 1.0 if initial_capital_krw > 0 else 0.0

    mdd, mdd_days = _max_drawdown(equity_curve)
    years = _years_span(equity_curve)
    cagr = (
        ((final_equity / initial_capital_krw) ** (1 / years) - 1.0)
        if years > 0 and initial_capital_krw > 0 and final_equity > 0
        else 0.0
    )

    daily_returns = _daily_returns(equity_values)
    sharpe = _sharpe(daily_returns)
    sortino = _sortino(daily_returns)
    calmar = cagr / abs(mdd) if mdd < 0 else 0.0

    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = math.inf if gross_profit > 0 else 0.0

    reason_counts: dict[str, int] = {}
    for t in trades:
        for r in t.exit_reasons:
            reason_counts[r.value] = reason_counts.get(r.value, 0) + 1

    return PerformanceReport(
        n_trades=n,
        n_wins=len(wins),
        n_losses=len(losses),
        win_rate=len(wins) / n if n else 0.0,
        total_return=total_return,
        cagr=cagr,
        max_drawdown=mdd,
        max_drawdown_days=mdd_days,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        profit_factor=profit_factor,
        expectancy_krw=net / n if n else 0.0,
        avg_win_krw=gross_profit / len(wins) if wins else 0.0,
        avg_loss_krw=-gross_loss / len(losses) if losses else 0.0,
        largest_win_krw=max((t.pnl_krw for t in wins), default=0.0),
        largest_loss_krw=min((t.pnl_krw for t in losses), default=0.0),
        avg_holding_days=sum(t.holding_days for t in trades) / n if n else 0.0,
        max_consecutive_losses=_max_streak(trades),
        avg_krw_return=sum(t.krw_return for t in trades) / n if n else 0.0,
        avg_usd_return=sum(t.usd_return for t in trades) / n if n else 0.0,
        avg_fx_return=sum(t.fx_return for t in trades) / n if n else 0.0,
        exit_reason_counts=reason_counts,
        final_equity_krw=final_equity,
        initial_capital_krw=initial_capital_krw,
    )


def _max_drawdown(curve: Sequence[tuple[dt.datetime, float]]) -> tuple[float, int]:
    """최대낙폭과 그 낙폭이 지속된 일수.

    Returns:
        (최대낙폭 비율(음수), 고점에서 저점까지의 달력일수)
    """
    peak = -math.inf
    peak_ts: dt.datetime | None = None
    worst = 0.0
    worst_days = 0
    for ts, v in curve:
        if v > peak:
            peak = v
            peak_ts = ts
        if peak > 0:
            dd = v / peak - 1.0
            if dd < worst:
                worst = dd
                worst_days = (ts - peak_ts).days if peak_ts else 0
    return worst, worst_days


def _years_span(curve: Sequence[tuple[dt.datetime, float]]) -> float:
    if len(curve) < 2:
        return 0.0
    return (curve[-1][0] - curve[0][0]).days / 365.25


def _daily_returns(values: Sequence[float]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(values)):
        prev = values[i - 1]
        if prev > 0:
            out.append(values[i] / prev - 1.0)
    return out


def _sharpe(returns: Sequence[float], risk_free: float = 0.0) -> float:
    """연율화 Sharpe. 무위험수익률은 기본 0으로 둔다(단기 매매라 영향이 작다)."""
    if len(returns) < 2:
        return 0.0
    excess = [r - risk_free / TRADING_DAYS_PER_YEAR for r in returns]
    mean = sum(excess) / len(excess)
    var = sum((r - mean) ** 2 for r in excess) / (len(excess) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return 0.0
    return mean / sd * math.sqrt(TRADING_DAYS_PER_YEAR)


def _sortino(returns: Sequence[float], risk_free: float = 0.0) -> float:
    """하방 변동성만으로 나눈 Sortino. 상승 변동성에 벌점을 주지 않는다."""
    if len(returns) < 2:
        return 0.0
    target = risk_free / TRADING_DAYS_PER_YEAR
    excess = [r - target for r in returns]
    mean = sum(excess) / len(excess)
    downside = [min(0.0, r) ** 2 for r in excess]
    dd = math.sqrt(sum(downside) / len(downside))
    if dd == 0:
        return 0.0
    return mean / dd * math.sqrt(TRADING_DAYS_PER_YEAR)


def _max_streak(trades: Sequence[TradeRecord]) -> int:
    """최대 연속 손실 횟수."""
    best = 0
    cur = 0
    for t in trades:
        if t.is_win:
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return best


_REASON_KO = {
    ExitReason.TAKE_PROFIT_LADDER.value: "분할 익절",
    ExitReason.HARD_STOP_USD.value: "USD 손절",
    ExitReason.HARD_STOP_KRW.value: "원화 손절",
    ExitReason.TRAILING_STOP.value: "트레일링 스톱",
    ExitReason.BREAKEVEN_STOP.value: "본전 스톱",
    ExitReason.TIME_STOP.value: "타임 스톱",
    ExitReason.REGIME_EXIT.value: "레짐 청산",
    ExitReason.KILL_SWITCH.value: "킬스위치",
    ExitReason.END_OF_BACKTEST.value: "백테스트 종료",
}


def _reason_ko(reason: str) -> str:
    return _REASON_KO.get(reason, reason)
