"""워크포워드 분석, 몬테카를로 부트스트랩, 파라미터 민감도.

단일 백테스트 결과 하나는 증거로서 거의 가치가 없다.
파라미터를 조금만 바꿔도 결과가 뒤집힌다면 그것은 전략이 아니라 곡선 맞추기다.
이 모듈은 그 사실을 드러내는 세 가지 검증을 제공한다.

* :func:`walk_forward` — 기간을 여러 구간으로 잘라 각각 따로 평가한다.
  어떤 구간에서만 벌었다면 그 구간의 시장 특성에 기댄 것이다.
* :func:`bootstrap_confidence` — 매매 결과를 복원추출해 성과의 신뢰구간을 만든다.
  "평균 +1.2%" 가 운인지 실력인지 구분한다.
* :func:`parameter_sweep` — 핵심 파라미터를 흔들어 결과가 얼마나 민감한지 본다.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, replace

from koru_trade.backtest.engine import run_backtest
from koru_trade.backtest.metrics import PerformanceReport, compute_metrics
from koru_trade.config import StrategyConfig
from koru_trade.models import Bar

__all__ = [
    "BootstrapResult",
    "FoldResult",
    "SweepRow",
    "bootstrap_confidence",
    "format_sweep",
    "format_walkforward",
    "parameter_sweep",
    "walk_forward",
]


@dataclass(frozen=True, slots=True)
class FoldResult:
    """워크포워드 1개 구간의 결과."""

    index: int
    start_label: str
    end_label: str
    n_bars: int
    report: PerformanceReport


def walk_forward(
    bars: Sequence[Bar], cfg: StrategyConfig, *, n_folds: int = 4
) -> tuple[FoldResult, ...]:
    """기간을 ``n_folds`` 개로 균등 분할해 각각 백테스트한다.

    각 구간은 지표 계산을 위해 앞쪽 ``warmup_bars`` 만큼 겹쳐서 데이터를 받되,
    **매매는 자기 구간 안에서만** 하도록 ``start_index`` 를 지정한다.

    Args:
        bars: 전체 봉.
        cfg: 전략 설정.
        n_folds: 분할 수.

    Returns:
        구간별 결과.
    """
    if n_folds < 2:
        raise ValueError(f"n_folds 는 2 이상이어야 한다: {n_folds}")
    warm = cfg.warmup_bars
    usable = len(bars) - warm
    if usable < n_folds * 20:
        raise ValueError(
            f"봉이 부족하다. {n_folds}개 구간으로 나누려면 "
            f"최소 {warm + n_folds * 20}개가 필요한데 {len(bars)}개뿐이다"
        )

    size = usable // n_folds
    out: list[FoldResult] = []
    for i in range(n_folds):
        trade_start = warm + i * size
        trade_end = warm + (i + 1) * size if i < n_folds - 1 else len(bars)
        window = bars[:trade_end]
        res = run_backtest(window, cfg, start_index=trade_start)
        rep = compute_metrics(res.trades, res.equity_curve, res.initial_capital_krw)
        out.append(
            FoldResult(
                index=i + 1,
                start_label=f"{bars[trade_start].ts:%Y-%m-%d}",
                end_label=f"{bars[trade_end - 1].ts:%Y-%m-%d}",
                n_bars=trade_end - trade_start,
                report=rep,
            )
        )
    return tuple(out)


def format_walkforward(folds: Sequence[FoldResult]) -> str:
    """워크포워드 결과를 표로."""
    lines = ["=" * 74, "  워크포워드 분석 — 구간별로 따로 평가", "=" * 74]
    header = (
        f"  {'구간':<4}{'시작':<12}{'종료':<12}{'매매':>5}{'승률':>8}"
        f"{'수익률':>10}{'MDD':>9}{'PF':>7}"
    )
    lines.append(header)
    lines.append("  " + "-" * 70)
    for f in folds:
        r = f.report
        pf = f"{r.profit_factor:.2f}" if math.isfinite(r.profit_factor) else "inf"
        lines.append(
            f"  {f.index:<4}{f.start_label:<12}{f.end_label:<12}"
            f"{r.n_trades:>5}{r.win_rate:>7.0%}"
            f"{r.total_return:>+10.2%}{r.max_drawdown:>+9.1%}{pf:>7}"
        )
    lines.append("  " + "-" * 70)

    profitable = sum(1 for f in folds if f.report.total_return > 0)
    total_trades = sum(f.report.n_trades for f in folds)
    lines.append(f"  수익 구간 {profitable}/{len(folds)}, 총 매매 {total_trades}건")
    if profitable <= len(folds) // 2:
        lines.append(
            "  -> 절반 이하의 구간에서만 수익이 났다. "
            "특정 시장 국면에 의존하는 전략일 가능성이 높다"
        )
    elif total_trades < len(folds) * 5:
        lines.append("  -> 구간당 매매 표본이 너무 적어 판단이 어렵다")
    else:
        lines.append("  -> 구간 전반에 걸쳐 일관성이 있다")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """부트스트랩 신뢰구간."""

    n_samples: int
    n_sims: int
    mean: float
    ci_low: float
    ci_high: float
    prob_positive: float
    """평균 수익률이 양수일 확률(부트스트랩 표본 기준)."""

    worst_5pct_path: float
    """최악 5% 시나리오의 누적 수익률(연속 매매 시뮬레이션)."""

    median_path: float
    ruin_probability: float
    """누적 손실이 -50% 를 넘을 확률."""

    def format_table(self) -> str:
        rows = [
            ("표본 / 반복", f"{self.n_samples}건 / {self.n_sims:,}회"),
            ("1회 평균 수익률", f"{self.mean:+.2%}"),
            ("95% 신뢰구간", f"{self.ci_low:+.2%} ~ {self.ci_high:+.2%}"),
            ("평균이 양수일 확률", f"{self.prob_positive:.1%}"),
            ("누적 경로 중앙값", f"{self.median_path:+.1%}"),
            ("최악 5% 경로", f"{self.worst_5pct_path:+.1%}"),
            ("파산 위험(-50% 초과)", f"{self.ruin_probability:.1%}"),
        ]
        width = max(len(k) for k, _ in rows)
        lines = [f"    {k.ljust(width)}  {v}" for k, v in rows]
        if self.ci_low <= 0 <= self.ci_high:
            lines.append(
                "    -> 신뢰구간이 0을 포함한다. 이 전략의 우위는 통계적으로 입증되지 않았다"
            )
        if self.ruin_probability > 0.05:
            lines.append(f"    -> 파산 위험 {self.ruin_probability:.0%} 는 감당할 수준이 아니다")
        return "\n".join(lines)


def bootstrap_confidence(
    returns: Sequence[float],
    *,
    n_sims: int = 2000,
    path_length: int = 30,
    seed: int = 20260824,
) -> BootstrapResult:
    """매매 수익률을 복원추출해 신뢰구간과 누적 경로 분포를 만든다.

    Args:
        returns: 1회 매매당 원화 수익률 목록.
        n_sims: 반복 횟수.
        path_length: 누적 경로 시뮬레이션에서 연속으로 이을 매매 수.
        seed: 난수 시드. **고정한다** — 돌릴 때마다 결과가 바뀌면 검증이 불가능하다.

    Returns:
        :class:`BootstrapResult`.
    """
    data = [r for r in returns if math.isfinite(r)]
    n = len(data)
    if n < 2:
        return BootstrapResult(n, n_sims, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    # 부트스트랩용 난수다. 암호 용도가 아니며 재현성을 위해 시드를 고정한다.
    rng = random.Random(seed)  # noqa: S311
    means: list[float] = []
    paths: list[float] = []
    ruins = 0

    for _ in range(n_sims):
        sample = [data[rng.randrange(n)] for _ in range(n)]
        means.append(statistics.fmean(sample))

        equity = 1.0
        worst = 1.0
        for _ in range(path_length):
            equity *= 1.0 + data[rng.randrange(n)]
            worst = min(worst, equity)
        paths.append(equity - 1.0)
        if worst <= 0.5:
            ruins += 1

    means.sort()
    paths.sort()
    return BootstrapResult(
        n_samples=n,
        n_sims=n_sims,
        mean=statistics.fmean(data),
        ci_low=_quantile(means, 0.025),
        ci_high=_quantile(means, 0.975),
        prob_positive=sum(1 for m in means if m > 0) / n_sims,
        worst_5pct_path=_quantile(paths, 0.05),
        median_path=_quantile(paths, 0.5),
        ruin_probability=ruins / n_sims,
    )


@dataclass(frozen=True, slots=True)
class SweepRow:
    """파라미터 민감도 1행."""

    parameter: str
    value: str
    n_trades: int
    win_rate: float
    total_return: float
    max_drawdown: float
    profit_factor: float


def parameter_sweep(bars: Sequence[Bar], base: StrategyConfig) -> tuple[SweepRow, ...]:
    """핵심 파라미터를 흔들어 결과의 민감도를 본다.

    결과가 파라미터에 극도로 민감하면 그 전략은 실전에서 재현되지 않는다.
    """
    rows: list[SweepRow] = []

    variations: list[tuple[str, str, StrategyConfig]] = []
    for v in (0.05, 0.08, 0.12, 0.20, 0.50):
        variations.append(("max_atr_pct", f"{v:.0%}", replace(base, max_atr_pct=v)))
    for v in (1.5, 2.0, 2.5, 3.0):
        variations.append(("stop_atr_multiple", f"{v}", replace(base, stop_atr_multiple=v)))
    for v in (3, 5, 8, 12):
        variations.append(("max_holding_days", f"{v}일", replace(base, max_holding_days=v)))
    for v in (-0.08, -0.10, -0.15, -0.20):
        variations.append(
            ("hard_stop_krw_return", f"{v:.0%}", replace(base, hard_stop_krw_return=v))
        )
    for v in (-0.01, -0.03, -0.06, -0.99):
        variations.append(("max_fx_decline", f"{v:.0%}", replace(base, max_fx_decline=v)))
    for v in (0.0, 10.0, 18.0, 25.0):
        variations.append(("min_adx", f"{v:.0f}", replace(base, min_adx=v)))

    for name, label, cfg in variations:
        try:
            res = run_backtest(bars, cfg)
        except ValueError:
            continue
        rep = compute_metrics(res.trades, res.equity_curve, res.initial_capital_krw)
        rows.append(
            SweepRow(
                parameter=name,
                value=label,
                n_trades=rep.n_trades,
                win_rate=rep.win_rate,
                total_return=rep.total_return,
                max_drawdown=rep.max_drawdown,
                profit_factor=rep.profit_factor,
            )
        )
    return tuple(rows)


def format_sweep(rows: Sequence[SweepRow]) -> str:
    """민감도 결과를 표로."""
    lines = ["=" * 74, "  파라미터 민감도 — 값을 바꾸면 결과가 얼마나 흔들리는가", "=" * 74]
    lines.append(
        f"  {'파라미터':<24}{'값':<8}{'매매':>5}{'승률':>7}{'수익률':>10}{'MDD':>9}{'PF':>7}"
    )
    lines.append("  " + "-" * 70)
    current = ""
    for r in rows:
        if r.parameter != current:
            if current:
                lines.append("  " + "-" * 70)
            current = r.parameter
        pf = f"{r.profit_factor:.2f}" if math.isfinite(r.profit_factor) else "inf"
        lines.append(
            f"  {r.parameter:<24}{r.value:<8}{r.n_trades:>5}{r.win_rate:>6.0%}"
            f"{r.total_return:>+10.2%}{r.max_drawdown:>+9.1%}{pf:>7}"
        )
    lines.append("  " + "-" * 70)
    lines.append("  같은 파라미터 안에서 수익률 부호가 왔다갔다 하면 그 값에 과적합된 것이다")
    return "\n".join(lines)


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac
