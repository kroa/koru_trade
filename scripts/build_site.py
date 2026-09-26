"""KORU 신호 원장 사이트를 만든다.

``scripts/site_template.html`` 의 ``__DATA__`` 자리에 최신 백테스트·시세 데이터를
끼워 넣어 ``build/koru_signals.html`` 을 만든다. 만들어진 파일은 아티팩트로
발행한다(같은 경로로 재발행하면 URL 이 유지된다).

    python scripts/build_site.py
    python scripts/build_site.py --config config/frequent.example.yaml --out build/site.html

설정 파일 주의
--------------
``config/strategy.yaml`` 은 .gitignore 에 있다. 즉 저장소를 새로 받은 환경
(클라우드 루틴 등)에는 그 파일이 없고, 인자를 주지 않으면 내장 기본값으로
돌아가 로컬과 다른 수치가 나온다. 그래서 여기서는 ``config/strategy.yaml`` 이
있으면 그것을, 없으면 **커밋되어 있는** ``config/frequent.example.yaml`` 을 쓰고
어느 것을 썼는지 항상 출력한다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from _site_common import (  # noqa: E402
    anonymize,
    next_open_kst,
    render,
    resolve_config,
    threshold_close,
)

from koru_trade import indicators as ind  # noqa: E402
from koru_trade.backtest import compute_metrics, run_backtest  # noqa: E402
from koru_trade.backtest.walkforward import bootstrap_confidence, walk_forward  # noqa: E402
from koru_trade.config import StrategyConfig  # noqa: E402
from koru_trade.data import load_bars  # noqa: E402
from koru_trade.models import Bar  # noqa: E402
from koru_trade.pnl import required_sell_price_usd  # noqa: E402
from koru_trade.strategy import evaluate_entry, plan_entry  # noqa: E402

TEMPLATE = ROOT / "scripts" / "site_template.html"
DEFAULT_OUT = ROOT / "build" / "koru_signals.html"


def build_payload(bars: tuple[Bar, ...], cfg: StrategyConfig) -> dict[str, Any]:
    """사이트가 읽는 JSON 전체를 만든다."""
    res = run_backtest(bars, cfg)
    metrics = compute_metrics(res.trades, res.equity_curve, res.initial_capital_krw)
    folds = [f.report.total_return for f in walk_forward(bars, cfg, n_folds=4)]
    ci = bootstrap_confidence([t.krw_return for t in res.trades], n_sims=4000)

    closes = [b.close for b in bars]
    price = closes[-1]
    fx = bars[-1].fx_rate
    atr = ind.atr(bars, cfg.atr_period) or 0.0
    signal = evaluate_entry(bars, cfg)
    plan = plan_entry(bars, cfg)
    stop_pct = min(max(cfg.stop_atr_multiple * atr / price, cfg.min_stop_pct), cfg.max_stop_pct)

    return {
        "symbol": cfg.symbol,
        "generated": bars[-1].ts.strftime("%Y-%m-%d"),
        "summary": {
            "n": metrics.n_trades,
            "wins": sum(1 for t in res.trades if t.is_win),
            "winRate": round(metrics.win_rate, 4),
            "total": round(metrics.total_return, 4),
            "pf": round(metrics.profit_factor, 2),
            "mdd": round(metrics.max_drawdown, 4),
            "sharpe": round(metrics.sharpe, 2),
            "wfPos": sum(1 for x in folds if x > 0),
            "wf": [round(x, 4) for x in folds],
            "ciLow": round(ci.ci_low, 4),
            "ciHigh": round(ci.ci_high, 4),
            "capital": cfg.capital_krw,
            "avgDays": round(sum(t.holding_days for t in res.trades) / max(1, len(res.trades)), 2),
        },
        "ladder": [{"k": s.krw_return, "f": s.sell_fraction} for s in cfg.take_profit],
        "current": {
            "price": round(price, 2),
            "fx": round(fx, 1),
            "ema10": round(ind.ema(closes, cfg.ema_fast) or 0.0, 2),
            "ema30": round(ind.ema(closes, cfg.ema_slow) or 0.0, 2),
            "atrPct": round(ind.atr_pct(bars, 14) or 0.0, 4),
            "adx": round(ind.adx(bars, 14) or 0.0, 1),
            "rsi": round(ind.rsi(closes, 14) or 0.0, 1),
            "allowed": bool(signal.allowed),
            "blockers": [c.name for c in signal.blockers],
        },
        "next": {
            "openKst": next_open_kst(),
            "thresholdClose": round(threshold_close(bars, cfg), 2),
            "gapLow": round(price * (1 - cfg.max_gap_pct), 2),
            "gapHigh": round(price * (1 + cfg.max_gap_pct), 2),
            "lastClose": round(price, 2),
        },
        "plan": {
            "totalQty": sum(plan.tranche_qty) if plan else 0,
            "tranches": list(plan.tranche_qty) if plan else [],
            "notional": round(plan.total_notional_krw) if plan else 0,
            "stop": round(price * (1 - stop_pct), 2),
            "stopPct": round(stop_pct, 4),
            "scaleIn": [
                {"w": s.weight, "price": round(price - atr * s.atr_multiple, 2)}
                for s in cfg.scale_in
            ],
            "tp": [
                {
                    "k": s.krw_return,
                    "f": s.sell_fraction,
                    "price": round(
                        required_sell_price_usd(price, fx, fx, s.krw_return, cfg.cost), 2
                    ),
                }
                for s in cfg.take_profit
            ],
        },
        "trades": [
            {
                "open": t.opened_at.strftime("%Y-%m-%d"),
                "close": t.closed_at.strftime("%Y-%m-%d"),
                "entry": round(t.entry_avg_price_usd, 2),
                "exit": round(t.exit_avg_price_usd, 2),
                "krw": round(t.krw_return, 4),
                "usd": round(t.usd_return, 4),
                "fx": round(t.fx_return, 4),
                "pnl": round(t.pnl_krw),
                "days": round(t.holding_days, 1),
                "reasons": [r.value for r in t.exit_reasons],
                "win": bool(t.is_win),
            }
            for t in res.trades
        ],
        "price": [
            {"d": b.ts.strftime("%Y-%m-%d"), "c": round(b.close, 2), "fx": round(b.fx_rate, 1)}
            for b in bars
        ],
        "equity": [{"d": ts.strftime("%Y-%m-%d"), "v": round(v)} for ts, v in res.equity_curve],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KORU 신호 원장 사이트 생성")
    parser.add_argument("--config", help="전략 설정 YAML 경로")
    parser.add_argument("--period", default="3y", help="시세 조회 기간 (기본 3y)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="출력 HTML 경로")
    parser.add_argument(
        "--public",
        action="store_true",
        help="공개 배포용. 실제 운용액을 기준 자본 100만원으로 환산한다",
    )
    args = parser.parse_args(argv)

    cfg, cfg_path = resolve_config(args.config)
    print(f"전략 설정: {cfg_path}")
    if args.public:
        cfg = anonymize(cfg)
        print(f"  공개 모드: 자본을 {cfg.capital_krw:,.0f}원 기준으로 환산했다")
    bars = load_bars(cfg.symbol, period=args.period, cache_dir=None)
    payload = build_payload(bars, cfg)
    out = render(payload, TEMPLATE, Path(args.out))

    summary = payload["summary"]
    nxt = payload["next"]
    cur = payload["current"]
    print(f"생성 완료: {out}  ({out.stat().st_size:,} bytes)")
    print(f"  최신 봉 {payload['generated']} · 종가 ${cur['price']} · 환율 {cur['fx']:,.0f}")
    print(f"  매매 {summary['n']}건 · 누적 {summary['total']:+.2%} · PF {summary['pf']}")
    state = "통과" if cur["allowed"] else "차단: " + ", ".join(cur["blockers"])
    print(f"  진입 게이트 {state}")
    print(
        f"  다음 개장 {nxt['openKst']} · 종가 임계 ${nxt['thresholdClose']} · "
        f"갭 ${nxt['gapLow']}~${nxt['gapHigh']}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
