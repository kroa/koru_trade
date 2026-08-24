"""대시보드에 뿌릴 데이터를 한 번에 모은다.

두 종류의 데이터를 **명확히 구분해서** 담는다. 섞으면 사용자가
"이게 내 실제 매매인가, 시뮬레이션인가" 를 구분할 수 없게 되고,
그것은 금융 대시보드에서 가장 위험한 혼동이다.

``source="live"``
    상태 DB(:class:`~koru_trade.live.state.StateStore`)에 기록된 **실제 실행 이력**.
    DRY_RUN 으로 돌렸다면 주문은 나가지 않았지만 판단 기록은 여기 남는다.
``source="backtest"``
    과거 데이터에 전략을 적용한 **시뮬레이션 결과**. 실제로 체결된 것이 아니다.

이 모듈은 HTTP 를 모른다. 순수하게 데이터만 만든다. 그래야 테스트할 수 있다.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from koru_trade import indicators as ind
from koru_trade.backtest import compute_metrics, run_backtest
from koru_trade.config import StrategyConfig
from koru_trade.entry_gate import evaluate_gate
from koru_trade.live.state import StateStore
from koru_trade.models import Bar, OrderSide, Position
from koru_trade.pnl import krw_proceeds, position_krw_return, position_required_price_usd
from koru_trade.risk import RiskState
from koru_trade.strategy import stop_price_usd
from koru_trade.version import __version__

__all__ = [
    "DashboardSnapshot",
    "build_snapshot",
    "snapshot_to_json",
]

MAX_SIGNALS = 300
"""신호 이력 표시 상한. 이보다 많으면 최신 것만 남긴다."""

PRICE_HISTORY_BARS = 90
"""가격 스파크라인에 쓸 최근 봉 수."""


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    """대시보드 한 화면에 필요한 모든 것."""

    generated_at: str
    version: str
    symbol: str
    data_range: dict[str, Any]
    market: dict[str, Any]
    verdict: dict[str, Any]
    strategy: dict[str, Any]
    live: dict[str, Any]
    performance: dict[str, Any]
    trades: list[dict[str, Any]]
    signals: list[dict[str, Any]]
    equity: list[dict[str, Any]]
    price_history: list[dict[str, Any]] = field(default_factory=list)
    regime_history: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_snapshot(
    bars: Sequence[Bar],
    cfg: StrategyConfig,
    *,
    store: StateStore | None = None,
    now: dt.datetime | None = None,
) -> DashboardSnapshot:
    """봉 시계열과 설정에서 대시보드 스냅샷을 만든다.

    Args:
        bars: 시간 오름차순 봉. 마지막이 "현재" 다.
        cfg: 전략 설정.
        store: 실거래 상태 저장소. None 이면 실계좌 구역은 비어 있다.
        now: 생성 시각. None 이면 현재 시각.

    Returns:
        :class:`DashboardSnapshot`.
    """
    if not bars:
        raise ValueError("봉 데이터가 비어 있다")

    last = bars[-1]
    stamp = now or dt.datetime.now()
    notes: list[str] = []

    report = evaluate_gate(bars, cfg)
    snap = report.snapshot

    result = run_backtest(bars, cfg)
    perf = compute_metrics(result.trades, result.equity_curve, result.initial_capital_krw)

    live = _live_section(cfg, last, store, notes)
    signals = _signals(result, store)

    if not signals:
        notes.append("아직 기록된 신호가 없다. 진입 필터가 한 번도 통과하지 않았다는 뜻이다.")
    if store is None:
        notes.append(
            "실거래 상태 DB 가 없어 실계좌 구역이 비어 있다. "
            "`python -m koru_trade tick` 을 한 번이라도 돌리면 채워진다."
        )

    return DashboardSnapshot(
        generated_at=stamp.isoformat(timespec="seconds"),
        version=__version__,
        symbol=cfg.symbol,
        data_range={
            "start": bars[0].ts.date().isoformat(),
            "end": last.ts.date().isoformat(),
            "bars": len(bars),
        },
        market=_market_section(bars, snap, cfg),
        verdict={
            "code": report.verdict.value,
            "label": _verdict_label(report.verdict.value),
            "allowed": report.allowed,
            "reasons": list(report.reasons),
            "checks": [
                {
                    "name": c.name,
                    "passed": c.passed,
                    "detail": c.detail,
                }
                for c in report.signal.checks
            ],
            "blockers": [c.name for c in report.signal.blockers],
            "plan": {
                "tranche_qty": list(report.planned_qty),
                "total_qty": sum(report.planned_qty),
                "notional_krw": report.planned_notional_krw,
                "stop_price_usd": report.stop_price_usd,
            },
        },
        strategy=_strategy_section(cfg),
        live=live,
        performance=_performance_section(perf, result.unfilled_orders),
        trades=_trades(result),
        signals=signals,
        equity=[
            {"ts": ts.date().isoformat(), "value": round(value)}
            for ts, value in result.equity_curve
        ],
        price_history=[
            {
                "ts": b.ts.date().isoformat(),
                "close": round(b.close, 4),
                "krw": round(b.close * b.fx_rate),
            }
            for b in bars[-PRICE_HISTORY_BARS:]
        ],
        regime_history=_regime_history(bars, cfg, signals),
        notes=notes,
    )


# ---------------------------------------------------------------------------
# 구역별 조립
# ---------------------------------------------------------------------------


def _market_section(bars: Sequence[Bar], snap: Any, cfg: StrategyConfig) -> dict[str, Any]:
    """현재 시세와 레짐 지표."""
    last = bars[-1]
    closes = [b.close for b in bars]
    prev = bars[-2].close if len(bars) > 1 else last.close
    window = bars[-20:]
    return {
        "as_of": last.ts.date().isoformat(),
        "price_usd": last.close,
        "change_pct": (last.close / prev - 1.0) if prev else 0.0,
        "fx_rate": last.fx_rate,
        "krw_price": last.close * last.fx_rate,
        "atr_usd": snap.atr_usd,
        "atr_pct": snap.atr_pct,
        "atr_limit": cfg.max_atr_pct,
        "realized_vol_20": snap.realized_vol_20,
        "realized_vol_60": snap.realized_vol_60,
        "decay_annual": snap.decay_annual,
        "decay_per_day": snap.decay_per_day,
        "rsi": snap.rsi,
        "adx": snap.adx,
        "ema_fast": snap.ema_fast,
        "ema_slow": snap.ema_slow,
        "fx_change_20": snap.fx_change_20,
        "fx_limit": cfg.max_fx_decline,
        "dist_from_high_20": snap.dist_from_high_20,
        "dist_from_low_20": snap.dist_from_low_20,
        "avg_dollar_volume": snap.avg_dollar_volume,
        "price_needed_tp1": snap.price_needed_tp1,
        "price_needed_tp_last": snap.price_needed_tp_last,
        "uplift_needed_tp1": (snap.price_needed_tp1 / last.close - 1.0 if last.close else 0.0),
        "high_20": max(b.high for b in window),
        "low_20": min(b.low for b in window),
        "consecutive_down": ind.consecutive_down_days(closes),
    }


def _strategy_section(cfg: StrategyConfig) -> dict[str, Any]:
    """현재 적용 중인 전략 규칙. 사용자가 화면에서 바로 확인할 수 있어야 한다."""
    return {
        "symbol": cfg.symbol,
        "exchange": cfg.exchange,
        "capital_krw": cfg.capital_krw,
        "scale_in": [
            {
                "index": i + 1,
                "weight": s.weight,
                "atr_multiple": s.atr_multiple,
                "trigger_text": (
                    "진입 즉시" if s.atr_multiple == 0 else f"진입가 -{s.atr_multiple} ATR"
                ),
            }
            for i, s in enumerate(cfg.scale_in)
        ],
        "take_profit": [
            {
                "index": i + 1,
                "krw_return": s.krw_return,
                "sell_fraction": s.sell_fraction,
            }
            for i, s in enumerate(cfg.take_profit)
        ],
        "stop": {
            "atr_multiple": cfg.stop_atr_multiple,
            "min_pct": cfg.min_stop_pct,
            "max_pct": cfg.max_stop_pct,
            "krw_hard_stop": cfg.hard_stop_krw_return,
            "breakeven_after_tp": cfg.breakeven_stop_after_tp,
            "trailing_after_tp": cfg.trailing_after_tp,
            "trailing_giveback": cfg.trailing_giveback,
            "max_holding_days": cfg.max_holding_days,
        },
        "filters": {
            "max_atr_pct": cfg.max_atr_pct,
            "rsi_range": [cfg.min_rsi, cfg.max_rsi],
            "min_adx": cfg.min_adx,
            "max_gap_pct": cfg.max_gap_pct,
            "max_fx_decline": cfg.max_fx_decline,
            "min_avg_dollar_volume": cfg.min_avg_dollar_volume,
        },
        "cost": {
            "round_trip_drag": cfg.cost.round_trip_drag,
            "fx_mode": cfg.cost.fx_mode.value,
            "fx_spread_buy": cfg.cost.fx_spread_buy,
            "fx_spread_sell": cfg.cost.fx_spread_sell,
            "buy_fee_rate": cfg.cost.buy_fee_rate,
            "sell_fee_rate": cfg.cost.sell_fee_rate,
        },
        "risk": {
            "max_position_krw": cfg.risk.max_position_krw,
            "max_daily_notional_krw": cfg.risk.max_daily_notional_krw,
            "daily_loss_limit_krw": cfg.risk.daily_loss_limit_krw,
            "max_consecutive_losses": cfg.risk.max_consecutive_losses,
            "max_trades_per_day": cfg.risk.max_trades_per_day,
        },
    }


def _live_section(
    cfg: StrategyConfig,
    last: Bar,
    store: StateStore | None,
    notes: list[str],
) -> dict[str, Any]:
    """실거래 상태 DB 에서 현재 포지션과 리스크 카운터를 읽는다."""
    empty_risk = RiskState(trade_date=last.ts.date())
    if store is None:
        return {
            "available": False,
            "position": _position_payload(Position(cfg.symbol), last, cfg),
            "risk": _risk_payload(empty_risk, cfg),
            "recent_decisions": [],
        }

    try:
        position = store.load_position(cfg.symbol)
        risk = store.load_risk(dt.date.today())
        decisions = store.recent_decisions(30)
    except Exception as exc:
        notes.append(f"상태 DB 를 읽지 못했다: {type(exc).__name__}: {exc}")
        return {
            "available": False,
            "position": _position_payload(Position(cfg.symbol), last, cfg),
            "risk": _risk_payload(empty_risk, cfg),
            "recent_decisions": [],
        }

    return {
        "available": True,
        "position": _position_payload(position, last, cfg),
        "risk": _risk_payload(risk, cfg),
        "recent_decisions": [
            {
                "ts": row["ts"],
                "action": row["action"],
                "qty": row["qty"],
                "price_usd": row["price_usd"],
                "fx_rate": row["fx_rate"],
                "krw_return": row["krw_return"],
                "rationale": row["rationale"],
            }
            for row in decisions
        ],
    }


def _position_payload(position: Position, last: Bar, cfg: StrategyConfig) -> dict[str, Any]:
    """보유 포지션의 현재 평가 상태. 원화 기준이 기본이다."""
    if not position.is_open:
        return {"open": False}

    krw_r = position_krw_return(position, last.close, last.fx_rate, cfg.cost)
    liquidation = krw_proceeds(position.qty, last.close, last.fx_rate, cfg.cost)
    stop = stop_price_usd(position, cfg)
    next_tp = next(
        (
            {
                "index": i + 1,
                "krw_return": s.krw_return,
                "price_usd": position_required_price_usd(
                    position, last.fx_rate, s.krw_return, cfg.cost
                ),
                "sell_qty": max(1, round(position.ladder_base_qty * s.sell_fraction)),
            }
            for i, s in enumerate(cfg.take_profit)
            if i not in position.tp_levels_hit
        ),
        None,
    )
    opened = position.opened_at
    return {
        "open": True,
        "qty": position.qty,
        "tranches_filled": position.tranche_count,
        "tranches_planned": len(position.planned_tranche_qty),
        "avg_price_usd": position.avg_price_usd,
        "first_entry_price_usd": position.first_entry_price_usd,
        "avg_fx_rate": position.avg_fx_rate,
        "avg_krw_unit_cost": position.avg_krw_unit_cost,
        "cost_krw": position.cost_krw,
        "value_krw": liquidation,
        "unrealized_krw": liquidation - position.cost_krw,
        "krw_return": krw_r,
        "peak_krw_return": position.peak_krw_return,
        "realized_krw": position.realized_krw,
        "tp_levels_hit": sorted(position.tp_levels_hit),
        "next_tp": next_tp,
        "stop_price_usd": stop,
        "stop_distance_pct": (last.close / stop - 1.0) if stop else 0.0,
        "krw_hard_stop": cfg.hard_stop_krw_return,
        "opened_at": opened.isoformat() if opened else None,
        "breakeven_price_usd": position_required_price_usd(position, last.fx_rate, 0.0, cfg.cost),
    }


def _risk_payload(risk: RiskState, cfg: StrategyConfig) -> dict[str, Any]:
    """킬스위치와 일일 한도 소진 현황."""
    limits = cfg.risk
    return {
        "trade_date": risk.trade_date.isoformat(),
        "halted": risk.is_halted,
        "halt_reasons": list(risk.halt_reasons),
        "manual_halt": risk.manual_halt,
        "realized_krw_today": risk.realized_krw_today,
        "daily_loss_limit_krw": limits.daily_loss_limit_krw,
        "loss_used_ratio": min(
            1.0, max(0.0, -risk.realized_krw_today / limits.daily_loss_limit_krw)
        )
        if limits.daily_loss_limit_krw
        else 0.0,
        "notional_krw_today": risk.notional_krw_today,
        "max_daily_notional_krw": limits.max_daily_notional_krw,
        "notional_used_ratio": min(1.0, risk.notional_krw_today / limits.max_daily_notional_krw)
        if limits.max_daily_notional_krw
        else 0.0,
        "entries_today": risk.entries_today,
        "max_trades_per_day": limits.max_trades_per_day,
        "consecutive_losses": risk.consecutive_losses,
        "max_consecutive_losses": limits.max_consecutive_losses,
        "in_cooldown": risk.in_cooldown,
        "cooldown_until": risk.cooldown_until.isoformat() if risk.cooldown_until else None,
        "loss_cooldown_days": limits.loss_cooldown_days,
    }


def _performance_section(perf: Any, unfilled: int) -> dict[str, Any]:
    """백테스트 성과. 원화 기준."""
    return {
        "n_trades": perf.n_trades,
        "n_wins": perf.n_wins,
        "n_losses": perf.n_losses,
        "win_rate": perf.win_rate,
        "total_return": perf.total_return,
        "total_pnl_krw": perf.final_equity_krw - perf.initial_capital_krw,
        "initial_capital_krw": perf.initial_capital_krw,
        "final_equity_krw": perf.final_equity_krw,
        "cagr": perf.cagr,
        "max_drawdown": perf.max_drawdown,
        "max_drawdown_days": perf.max_drawdown_days,
        "sharpe": perf.sharpe,
        "sortino": perf.sortino,
        "profit_factor": (perf.profit_factor if math.isfinite(perf.profit_factor) else None),
        "expectancy_krw": perf.expectancy_krw,
        "avg_win_krw": perf.avg_win_krw,
        "avg_loss_krw": perf.avg_loss_krw,
        "largest_win_krw": perf.largest_win_krw,
        "largest_loss_krw": perf.largest_loss_krw,
        "avg_holding_days": perf.avg_holding_days,
        "max_consecutive_losses": perf.max_consecutive_losses,
        "avg_krw_return": perf.avg_krw_return,
        "avg_usd_return": perf.avg_usd_return,
        "avg_fx_return": perf.avg_fx_return,
        "exit_reason_counts": dict(perf.exit_reason_counts),
        "unfilled_orders": unfilled,
        "is_viable": perf.is_viable,
        "verdict": perf.verdict,
    }


def _trades(result: Any) -> list[dict[str, Any]]:
    """완결된 매매 목록. 최신이 위로 오도록 뒤집는다."""
    rows = [
        {
            "opened_at": t.opened_at.date().isoformat(),
            "closed_at": t.closed_at.date().isoformat(),
            "tranches": t.tranches,
            "qty": t.max_qty,
            "entry_price_usd": t.entry_avg_price_usd,
            "exit_price_usd": t.exit_avg_price_usd,
            "entry_fx": t.entry_avg_fx,
            "exit_fx": t.exit_avg_fx,
            "cost_krw": t.cost_krw,
            "proceeds_krw": t.proceeds_krw,
            "pnl_krw": t.pnl_krw,
            "krw_return": t.krw_return,
            "usd_return": t.usd_return,
            "fx_return": t.fx_return,
            "holding_days": t.holding_days,
            "is_win": t.is_win,
            "exit_reasons": [r.value for r in t.exit_reasons],
        }
        for t in result.trades
    ]
    rows.reverse()
    return rows


def _signals(result: Any, store: StateStore | None) -> list[dict[str, Any]]:
    """매수/매도 신호 이력.

    백테스트 체결(``source="backtest"``)과 실거래 판단 기록(``source="live"``)을
    합쳐 최신순으로 정렬한다. 어느 쪽에서 왔는지는 ``source`` 로 구분한다.
    """
    rows: list[dict[str, Any]] = []

    for fill in result.fills:
        is_buy = fill.side is OrderSide.BUY
        rows.append(
            {
                "source": "backtest",
                "ts": fill.ts.date().isoformat(),
                "side": "BUY" if is_buy else "SELL",
                "side_label": "매수" if is_buy else "매도",
                "qty": fill.qty,
                "price_usd": fill.price_usd,
                "fx_rate": fill.fx_rate,
                "amount_krw": abs(fill.cash_delta_krw),
                "tranche_index": fill.tranche_index,
                "exit_reason": fill.exit_reason.value if fill.exit_reason else None,
                "reason": fill.reason,
            }
        )

    if store is not None:
        try:
            for row in store.recent_decisions(100):
                action = str(row["action"])
                if action in ("HOLD", "BLOCKED"):
                    continue
                is_buy = action in ("ENTER", "SCALE_IN")
                rows.append(
                    {
                        "source": "live",
                        "ts": str(row["ts"])[:10],
                        "side": "BUY" if is_buy else "SELL",
                        "side_label": "매수" if is_buy else "매도",
                        "qty": row["qty"],
                        "price_usd": row["price_usd"],
                        "fx_rate": row["fx_rate"],
                        "amount_krw": abs(
                            float(row["qty"]) * float(row["price_usd"]) * float(row["fx_rate"])
                        ),
                        "tranche_index": None,
                        "exit_reason": None,
                        "reason": row["rationale"],
                    }
                )
        except Exception:  # noqa: S110 - 실거래 기록이 없어도 대시보드는 떠야 한다
            pass

    rows.sort(key=lambda r: (str(r["ts"]), str(r["source"])), reverse=True)
    return rows[:MAX_SIGNALS]


def _regime_history(
    bars: Sequence[Bar], cfg: StrategyConfig, signals: list[dict[str, Any]]
) -> dict[str, Any]:
    """월별로 각 진입 필터가 얼마나 자주 차단했는지 집계한다.

    "왜 최근에 신호가 없나" 는 자동매매를 돌리는 사람이 가장 먼저 묻는 질문이다.
    화면이 그냥 비어 있으면 봇이 고장난 것인지 시장이 나쁜 것인지 알 수 없다.
    필터별 차단률을 월 단위로 보여 주면 그 구분이 즉시 된다.

    비용은 봉당 필터 1회 평가로, 3년치 716봉이 약 1.4초다. 스냅샷 캐시 안에서 감당된다.
    """
    from koru_trade.strategy import evaluate_entry

    start = cfg.warmup_bars
    if len(bars) <= start:
        return {"months": [], "filters": [], "last_signal": None}

    buckets: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    filter_names: list[str] = []

    for i in range(start, len(bars)):
        bar = bars[i]
        key = f"{bar.ts.year}-{bar.ts.month:02d}"
        if key not in buckets:
            buckets[key] = {"month": key, "bars": 0, "passed": 0, "atr": [], "blocks": {}}
            order.append(key)
        row = buckets[key]
        row["bars"] += 1

        signal = evaluate_entry(bars[: i + 1], cfg)
        if not filter_names:
            filter_names = [c.name for c in signal.checks if c.name != "데이터충분성"]
        if signal.allowed:
            row["passed"] += 1
        for check in signal.blockers:
            if check.name == "데이터충분성":
                continue
            row["blocks"][check.name] = row["blocks"].get(check.name, 0) + 1

        atr_p = ind.atr_pct(bars[: i + 1], cfg.atr_period)
        if atr_p is not None:
            row["atr"].append(atr_p)

    months: list[dict[str, Any]] = []
    for key in order:
        row = buckets[key]
        n = max(1, int(row["bars"]))
        atrs = sorted(row["atr"])
        blocks = {name: row["blocks"].get(name, 0) / n for name in filter_names}
        top = max(blocks.items(), key=lambda kv: kv[1], default=("", 0.0))
        months.append(
            {
                "month": key,
                "bars": row["bars"],
                "passed": row["passed"],
                "pass_rate": row["passed"] / n,
                "atr_pct": atrs[len(atrs) // 2] if atrs else None,
                "blocks": blocks,
                "dominant_blocker": top[0] if top[1] > 0 else None,
            }
        )

    last_signal = signals[0]["ts"] if signals else None
    recent = months[-6:]
    worst = max(
        (
            (name, sum(m["blocks"].get(name, 0.0) for m in recent) / max(1, len(recent)))
            for name in filter_names
        ),
        key=lambda kv: kv[1],
        default=("", 0.0),
    )
    return {
        "months": months,
        "filters": filter_names,
        "last_signal": last_signal,
        "recent_dominant_blocker": worst[0] if worst[1] > 0 else None,
        "recent_dominant_rate": worst[1],
    }


def _verdict_label(code: str) -> str:
    return {
        "ENTER": "진입 가능",
        "WAIT": "대기",
        "AVOID": "진입 금지",
    }.get(code, code)


def snapshot_to_json(snapshot: DashboardSnapshot) -> str:
    """스냅샷을 JSON 문자열로. NaN/Infinity 는 null 로 바꾼다.

    표준 JSON 에는 NaN 이 없다. ``json.dumps`` 기본값은 ``NaN`` 을 그대로 뱉는데
    브라우저의 ``JSON.parse`` 는 그것을 거부한다. 대시보드가 통째로 빈 화면이 된다.
    """
    return json.dumps(
        _sanitize(snapshot.to_dict()), ensure_ascii=False, allow_nan=False, indent=None
    )


def _sanitize(value: Any) -> Any:
    """유한하지 않은 실수를 None 으로 바꾼다(재귀)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_sanitize(v) for v in value]
    return value
