"""명령줄 인터페이스.

python -m koru_trade gate          # 지금 진입해도 되는지 판정
python -m koru_trade backtest      # 과거 성과 백테스트
python -m koru_trade scan          # 전방 시뮬레이션 결과 분포
python -m koru_trade walkforward   # 워크포워드 + 몬테카를로 강건성 검증
python -m koru_trade sweep         # 파라미터 민감도
python -m koru_trade tick          # 실거래 1틱 (기본 DRY_RUN)
python -m koru_trade status        # 저장된 포지션/리스크 상태
python -m koru_trade doctor        # 설정과 자격증명 점검 (값은 마스킹)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from koru_trade.config import (
    StrategyConfig,
    is_dry_run,
    load_credentials,
    load_strategy_config,
    mask_secret,
)
from koru_trade.models import Bar
from koru_trade.version import __version__

logger = logging.getLogger("koru_trade")


def main(argv: list[str] | None = None) -> int:
    """진입점. 종료 코드를 반환한다."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.command is None:
        parser.print_help()
        return 1

    cfg = load_strategy_config(args.config) if args.config else StrategyConfig()
    handlers = {
        "gate": _cmd_gate,
        "backtest": _cmd_backtest,
        "scan": _cmd_scan,
        "walkforward": _cmd_walkforward,
        "sweep": _cmd_sweep,
        "tick": _cmd_tick,
        "status": _cmd_status,
        "doctor": _cmd_doctor,
    }
    try:
        return handlers[args.command](args, cfg)
    except KeyboardInterrupt:
        print("\n중단했다.")
        return 130
    except Exception as exc:
        logger.error("%s: %s", type(exc).__name__, exc)
        if args.log_level.upper() == "DEBUG":
            raise
        return 2


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="koru",
        description="KORU 원화 기준 분할매수/분할익절 자동매매 시스템",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--version", action="version", version=f"koru-trade {__version__}")
    p.add_argument("--config", help="전략 설정 YAML 경로")
    p.add_argument("--log-level", default="INFO", help="DEBUG/INFO/WARNING/ERROR")

    sub = p.add_subparsers(dest="command")

    def add_data_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--period", default="3y", help="조회 기간 (1y/3y/5y/max)")
        sp.add_argument("--csv", help="CSV 파일에서 읽기(네트워크 미사용)")
        sp.add_argument("--no-cache", action="store_true", help="캐시를 쓰지 않는다")

    g = sub.add_parser("gate", help="지금 진입해도 되는지 판정")
    add_data_args(g)
    g.add_argument("--save", help="리포트를 텍스트 파일로 저장")

    b = sub.add_parser("backtest", help="과거 성과 백테스트")
    add_data_args(b)
    b.add_argument("--capital", type=float, help="초기 원화 자산")
    b.add_argument("--trades", action="store_true", help="개별 매매 내역 출력")

    s = sub.add_parser("scan", help="전방 시뮬레이션 결과 분포")
    add_data_args(s)
    s.add_argument("--stride", type=int, default=1, help="표본 간격")

    w = sub.add_parser("walkforward", help="워크포워드 + 몬테카를로 강건성 검증")
    add_data_args(w)
    w.add_argument("--folds", type=int, default=4, help="분할 수")
    w.add_argument("--sims", type=int, default=2000, help="몬테카를로 반복 수")

    sw = sub.add_parser("sweep", help="파라미터 민감도 분석")
    add_data_args(sw)

    t = sub.add_parser("tick", help="실거래 1틱 실행")
    add_data_args(t)
    t.add_argument("--state", default="state/koru_state.db", help="상태 DB 경로")
    t.add_argument(
        "--paper",
        action="store_true",
        help="페이퍼 브로커 사용(자격증명 불필요)",
    )

    st = sub.add_parser("status", help="저장된 포지션과 리스크 상태")
    st.add_argument("--state", default="state/koru_state.db", help="상태 DB 경로")

    sub.add_parser("doctor", help="설정과 자격증명 점검")
    return p


# ---------------------------------------------------------------------------
# 데이터
# ---------------------------------------------------------------------------


def _load(args: argparse.Namespace, cfg: StrategyConfig) -> tuple[Bar, ...]:
    from koru_trade.data import load_bars, load_bars_from_csv

    if getattr(args, "csv", None):
        return load_bars_from_csv(args.csv)
    return load_bars(
        cfg.symbol,
        period=getattr(args, "period", "3y"),
        cache_dir=None if getattr(args, "no_cache", False) else ".cache",
    )


# ---------------------------------------------------------------------------
# 명령
# ---------------------------------------------------------------------------


def _cmd_gate(args: argparse.Namespace, cfg: StrategyConfig) -> int:
    from koru_trade.entry_gate import Verdict, evaluate_gate

    bars = _load(args, cfg)
    report = evaluate_gate(bars, cfg)
    text = report.format_report()
    print(text)
    if args.save:
        Path(args.save).parent.mkdir(parents=True, exist_ok=True)
        Path(args.save).write_text(text, encoding="utf-8")
        print(f"\n리포트를 저장했다: {args.save}")
    return 0 if report.verdict is Verdict.ENTER else 3


def _cmd_backtest(args: argparse.Namespace, cfg: StrategyConfig) -> int:
    from koru_trade.backtest import compute_metrics, run_backtest

    bars = _load(args, cfg)
    res = run_backtest(bars, cfg, initial_capital_krw=args.capital)
    rep = compute_metrics(res.trades, res.equity_curve, res.initial_capital_krw)

    print("=" * 74)
    print(f"  백테스트 결과  ({bars[0].ts:%Y-%m-%d} ~ {bars[-1].ts:%Y-%m-%d}, {len(bars)}봉)")
    print("=" * 74)
    print(rep.format_table())
    print(f"\n  지정가 미체결 취소: {res.unfilled_orders}건")
    print(f"\n  판정: {rep.verdict}")

    if args.trades and res.trades:
        print("\n" + "-" * 74)
        print("  개별 매매 내역")
        print("-" * 74)
        header = f"  {'진입':<12}{'청산':<12}{'차수':>4}{'수량':>6}{'원화수익률':>12}{'손익(원)':>14}  사유"
        print(header)
        for t in res.trades:
            reasons = ",".join(r.value for r in t.exit_reasons)
            print(
                f"  {t.opened_at:%Y-%m-%d}  {t.closed_at:%Y-%m-%d}"
                f"{t.tranches:>4}{t.max_qty:>6}{t.krw_return:>+11.2%}"
                f"{t.pnl_krw:>+14,.0f}  {reasons}"
            )
    return 0


def _cmd_scan(args: argparse.Namespace, cfg: StrategyConfig) -> int:
    from koru_trade.backtest.pathsim import scan_forward_outcomes, summarize_outcomes

    bars = _load(args, cfg)
    outcomes = scan_forward_outcomes(bars, cfg, stride=args.stride)
    print("=" * 74)
    print("  전방 시뮬레이션 — 모든 날짜에 진입했다면?")
    print("=" * 74)
    print(summarize_outcomes(outcomes, "전체").format_table())

    up = [o for o in outcomes if o.trend_up_at_entry]
    down = [o for o in outcomes if not o.trend_up_at_entry]
    print("\n  [추세 정배열 시점 진입]")
    print(summarize_outcomes(up, "정배열").format_table())
    print("\n  [추세 역배열 시점 진입]")
    print(summarize_outcomes(down, "역배열").format_table())

    calm = [o for o in outcomes if o.atr_pct_at_entry <= cfg.max_atr_pct]
    wild = [o for o in outcomes if o.atr_pct_at_entry > cfg.max_atr_pct]
    print(f"\n  [저변동성 진입: ATR% <= {cfg.max_atr_pct:.0%}]")
    print(summarize_outcomes(calm, "저변동성").format_table())
    print(f"\n  [고변동성 진입: ATR% > {cfg.max_atr_pct:.0%}]")
    print(summarize_outcomes(wild, "고변동성").format_table())
    return 0


def _cmd_walkforward(args: argparse.Namespace, cfg: StrategyConfig) -> int:
    from koru_trade.backtest.walkforward import (
        bootstrap_confidence,
        format_walkforward,
        walk_forward,
    )

    bars = _load(args, cfg)
    folds = walk_forward(bars, cfg, n_folds=args.folds)
    print(format_walkforward(folds))

    from koru_trade.backtest.pathsim import scan_forward_outcomes

    outcomes = scan_forward_outcomes(bars, cfg)
    ci = bootstrap_confidence([o.krw_return for o in outcomes], n_sims=args.sims)
    print("\n" + "=" * 74)
    print(f"  몬테카를로 부트스트랩 ({args.sims:,}회, 표본 {len(outcomes)}건)")
    print("=" * 74)
    print(ci.format_table())
    return 0


def _cmd_sweep(args: argparse.Namespace, cfg: StrategyConfig) -> int:
    from koru_trade.backtest.walkforward import format_sweep, parameter_sweep

    bars = _load(args, cfg)
    rows = parameter_sweep(bars, cfg)
    print(format_sweep(rows))
    return 0


def _cmd_tick(args: argparse.Namespace, cfg: StrategyConfig) -> int:
    from koru_trade.broker.base import Broker
    from koru_trade.broker.paper import PaperBroker
    from koru_trade.live import LiveRunner, StateStore
    from koru_trade.models import Quote

    bars = _load(args, cfg)
    last = bars[-1]

    if args.paper:

        def quote_source(symbol: str) -> Quote:
            return Quote(
                symbol=symbol,
                last=last.close,
                bid=last.close * 0.999,
                ask=last.close * 1.001,
                ts=last.ts,
                fx_rate=last.fx_rate,
            )

        broker: Broker = PaperBroker(quote_source)
        print("페이퍼 브로커로 실행한다 (실제 주문 없음)")
    else:
        from koru_trade.broker.kis import KisBroker

        cred = load_credentials()
        dry = is_dry_run()
        broker = KisBroker(cred, exchange=cfg.exchange, dry_run=dry)
        print(f"KIS 브로커 ({cred.env.value}) / DRY_RUN={dry}")

    store = StateStore(args.state)
    runner = LiveRunner(broker, store, cfg)
    result = runner.tick(bars)

    print("\n" + "=" * 74)
    print(f"  {result.summary()}")
    print("=" * 74)
    print(f"  {result.decision.rationale}")

    warning = runner.reconcile()
    if warning:
        print(f"\n  [경고] {warning}")
    return 0


def _cmd_status(args: argparse.Namespace, cfg: StrategyConfig) -> int:
    import datetime as dt

    from koru_trade.live import StateStore

    store = StateStore(args.state)
    pos = store.load_position(cfg.symbol)
    risk = store.load_risk(dt.date.today())

    print("=" * 74)
    print("  현재 상태")
    print("=" * 74)
    if pos.is_open:
        print(f"  보유 수량        {pos.qty}주 ({pos.tranche_count}차 분할)")
        print(f"  USD 평단         ${pos.avg_price_usd:.2f}")
        print(f"  원화 주당 원가   {pos.avg_krw_unit_cost:,.0f}원")
        print(f"  원화 총 원가     {pos.cost_krw:,.0f}원")
        print(f"  매수 평균 환율   {pos.avg_fx_rate:,.1f}")
        print(f"  체결된 익절 계단 {sorted(pos.tp_levels_hit) or '없음'}")
        print(f"  최고 원화수익률  {pos.peak_krw_return:+.2%}")
        print(f"  최초 진입        {pos.opened_at}")
    else:
        print("  보유 포지션 없음")
    print()
    print(f"  거래일           {risk.trade_date}")
    print(f"  당일 실현손익    {risk.realized_krw_today:+,.0f}원")
    print(f"  당일 투입금액    {risk.notional_krw_today:,.0f}원")
    print(f"  당일 진입 횟수   {risk.entries_today}회")
    print(f"  연속 손실        {risk.consecutive_losses}회")
    print(f"  매매 중단 여부   {'중단' if risk.is_halted else '정상'}")
    for r in risk.halt_reasons:
        print(f"    - {r}")

    recent = store.recent_decisions(10)
    if recent:
        print("\n  최근 판단 10건")
        for row in recent:
            print(f"    {row['ts'][:19]}  {row['action']:<12} {row['rationale'][:80]}")
    return 0


def _cmd_doctor(args: argparse.Namespace, cfg: StrategyConfig) -> int:  # noqa: ARG001
    """설정과 자격증명을 점검한다. **값은 절대 원문으로 출력하지 않는다.**"""
    print("=" * 74)
    print("  설정 점검")
    print("=" * 74)
    print(f"  버전              koru-trade {__version__}")
    print(f"  종목              {cfg.symbol} ({cfg.exchange})")
    print(f"  1회 투입 자본     {cfg.capital_krw:,.0f}원")
    print(
        f"  분할 매수         {len(cfg.scale_in)}차 "
        f"{[f'{s.weight:.0%}@{s.atr_multiple}ATR' for s in cfg.scale_in]}"
    )
    print(
        f"  분할 익절(원화)   {[f'+{s.krw_return:.0%}->{s.sell_fraction:.0%}' for s in cfg.take_profit]}"
    )
    print(f"  왕복 거래비용     {cfg.cost.round_trip_drag:.3%}")
    print(f"  환전 방식         {cfg.cost.fx_mode.value}")
    print(f"  원화 하드스톱     {cfg.hard_stop_krw_return:+.0%}")
    print(f"  최대 보유         {cfg.max_holding_days}영업일")
    print(f"  지표 워밍업       {cfg.warmup_bars}봉")
    print()
    print("  리스크 한도")
    print(f"    1회 최대 투입   {cfg.risk.max_position_krw:,.0f}원")
    print(f"    일일 최대 투입  {cfg.risk.max_daily_notional_krw:,.0f}원")
    print(f"    일일 손실 한도  {cfg.risk.daily_loss_limit_krw:,.0f}원")
    print(f"    연속 손실 한도  {cfg.risk.max_consecutive_losses}회")
    print()
    print("=" * 74)
    print("  자격증명 점검 (값은 마스킹되어 출력된다)")
    print("=" * 74)
    try:
        cred = load_credentials()
        print(f"  KIS_APP_KEY       {mask_secret(cred.app_key)}")
        print(f"  KIS_APP_SECRET    {mask_secret(cred.app_secret)}")
        print(f"  KIS_ACCOUNT_NO    {mask_secret(cred.account_no, 2)}")
        print(f"  계좌상품코드      {cred.account_product_code}")
        print(f"  환경              {cred.env.value}")
        print(f"  API 도메인        {cred.base_url}")
        ok = True
    except ValueError as exc:
        print(f"  [미설정] {exc}")
        print("  -> .env.example 을 .env 로 복사해 채워라")
        ok = False

    dry = is_dry_run()
    print(f"  KORU_DRY_RUN      {dry}  ({'주문이 나가지 않는다' if dry else '실제 주문이 나간다'})")
    if not dry:
        print("  [경고] DRY_RUN 이 꺼져 있다. 실제 자금이 움직인다")

    print()
    print("  .gitignore 점검")
    gi = Path(".gitignore")
    if gi.exists():
        content = gi.read_text(encoding="utf-8")
        for pattern in (".env", "state/", "*.sqlite", "*.log"):
            mark = "OK  " if pattern in content else "누락"
            print(f"    [{mark}] {pattern}")
    else:
        print("    [누락] .gitignore 파일이 없다")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
