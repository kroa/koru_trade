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
from typing import TYPE_CHECKING

from koru_trade.config import (
    StrategyConfig,
    is_dry_run,
    load_credentials,
    load_strategy_config,
    mask_secret,
)
from koru_trade.models import Bar
from koru_trade.version import __version__

if TYPE_CHECKING:
    from koru_trade.broker.base import Broker
    from koru_trade.live.state import StateStore
    from koru_trade.notify.base import SignalNotifier

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

    cfg = _resolve_config(args.config)
    handlers = {
        "gate": _cmd_gate,
        "backtest": _cmd_backtest,
        "scan": _cmd_scan,
        "walkforward": _cmd_walkforward,
        "sweep": _cmd_sweep,
        "tick": _cmd_tick,
        "status": _cmd_status,
        "doctor": _cmd_doctor,
        "web": _cmd_web,
        "watch": _cmd_watch,
        "notify-test": _cmd_notify_test,
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


ACTIVE_CONFIG = Path("config/strategy.yaml")
"""``--config`` 를 주지 않았을 때 자동으로 쓰는 설정 파일.

프리셋 문서가 "cp config/daytrade.example.yaml config/strategy.yaml" 이라고
안내하므로, 그렇게 복사한 사용자는 별도 인자 없이 그 설정이 적용되기를 기대한다.
이 경로가 없으면 조용히 기본값을 쓴다.
"""


def _resolve_config(explicit: str | None) -> StrategyConfig:
    """적용할 전략 설정을 정한다.

    우선순위는 ``--config`` > ``config/strategy.yaml`` > 내장 기본값이다.
    어느 것을 썼는지 **항상 로그로 알린다.** 어떤 설정으로 돌고 있는지
    모른 채 실거래를 돌리는 것이 가장 위험하다.
    """
    if explicit:
        logger.info("전략 설정: %s", explicit)
        return load_strategy_config(explicit)
    if ACTIVE_CONFIG.exists():
        logger.info("전략 설정: %s (자동 적용)", ACTIVE_CONFIG)
        return load_strategy_config(ACTIVE_CONFIG)
    logger.info("전략 설정: 내장 기본값 (config/strategy.yaml 없음)")
    return StrategyConfig()


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="koru",
        description="KORU 원화 기준 분할매수/분할익절 자동매매 시스템",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--version", action="version", version=f"koru-trade {__version__}")
    p.add_argument(
        "--config",
        help="전략 설정 YAML 경로. 생략하면 config/strategy.yaml 이 있으면 그것을, "
        "없으면 내장 기본값을 쓴다",
    )
    p.add_argument("--log-level", default="INFO", help="DEBUG/INFO/WARNING/ERROR")

    sub = p.add_subparsers(dest="command")

    def add_data_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--period", default="3y", help="조회 기간 (1y/3y/5y/max/60d)")
        sp.add_argument(
            "--bar",
            default="1d",
            help="봉 간격: 1d(기본) / 1h / 15m / 5m. "
            "분봉은 yfinance 제약으로 기간이 짧다(5m·15m=60일, 1h=730일)",
        )
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

    wb = sub.add_parser("web", help="로컬 웹 대시보드 실행")
    add_data_args(wb)
    wb.add_argument("--port", type=int, default=8642, help="포트 (사용 중이면 다음 빈 포트)")
    wb.add_argument("--state", default="state/koru_state.db", help="상태 DB 경로")
    wb.add_argument("--open", action="store_true", help="브라우저를 자동으로 연다")
    wb.add_argument(
        "--refresh",
        type=float,
        default=900.0,
        help="시세를 다시 받는 주기(초). 기본 900=15분. 0 이면 켤 때 받은 시세를 계속 쓴다",
    )

    wt = sub.add_parser("watch", help="주기적으로 감시하며 신호를 알림")
    add_data_args(wt)
    wt.add_argument("--interval", type=int, default=900, help="점검 주기(초). 기본 900=15분")
    wt.add_argument("--state", default="state/koru_state.db", help="상태 DB 경로")
    wt.add_argument("--paper", action="store_true", help="페이퍼 브로커 사용")
    wt.add_argument("--once", action="store_true", help="한 번만 점검하고 종료")

    sub.add_parser("notify-test", help="텔레그램 설정 점검 및 테스트 메시지 발송")
    return p


def _build_notifier() -> SignalNotifier:
    """환경변수에서 알림 채널을 만든다. 설정이 없으면 조용히 끈다."""
    from koru_trade.notify import NullNotifier, TelegramNotifier, load_telegram_config

    conf = load_telegram_config()
    if conf is None:
        return NullNotifier()
    return TelegramNotifier(conf)


# ---------------------------------------------------------------------------
# 데이터
# ---------------------------------------------------------------------------


def _load(args: argparse.Namespace, cfg: StrategyConfig) -> tuple[Bar, ...]:
    from koru_trade.data import load_bars, load_bars_from_csv

    if getattr(args, "csv", None):
        return load_bars_from_csv(args.csv)
    interval = getattr(args, "bar", "1d")
    period = getattr(args, "period", "3y")
    # 분봉은 yfinance 가 제공하는 기간이 짧다. 기본 3y 를 그대로 보내면 빈 응답이 온다.
    if interval != "1d" and period in ("3y", "1y", "5y", "max"):
        period = "730d" if interval in ("1h", "60m", "90m") else "60d"
        logger.info("분봉(%s)은 기간을 %s 로 조정한다", interval, period)
    return load_bars(
        cfg.symbol,
        period=period,
        interval=interval,
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
    _restore_paper_position(broker, store, cfg)
    notifier = _build_notifier()
    if notifier.enabled:
        print("텔레그램 알림이 연결되어 있다")
    runner = LiveRunner(broker, store, cfg, notifier=notifier)
    result = runner.tick(bars)

    print("\n" + "=" * 74)
    print(f"  {result.summary()}")
    print("=" * 74)
    print(f"  {result.decision.rationale}")

    warning = runner.reconcile()
    if warning:
        print(f"\n  [경고] {warning}")
    return 0


def _market_status_line() -> str:
    """미국장 개폐 상태 한 줄. 휴장일과 조기폐장을 반영한다.

    이게 없으면 사람에게 "월요일이면 신호가 온다" 같은 안내를 하게 되는데,
    그 월요일이 노동절이면 틀린 안내다. 실제로 그런 일이 있었다.
    """
    import datetime as dt
    from zoneinfo import ZoneInfo

    from koru_trade.market_calendar import is_early_close, is_trading_day, next_trading_day

    ny_tz = ZoneInfo("America/New_York")
    kr_tz = ZoneInfo("Asia/Seoul")
    now_ny = dt.datetime.now(ny_tz)
    close_h = 13 if is_early_close(now_ny.date()) else 16

    if is_trading_day(now_ny.date()) and dt.time(9, 30) <= now_ny.time() < dt.time(close_h, 0):
        closes = dt.datetime.combine(now_ny.date(), dt.time(close_h, 0), ny_tz)
        left = closes - now_ny
        hh, mm = divmod(int(left.total_seconds()) // 60, 60)
        early = " (조기폐장)" if close_h == 13 else ""
        kr = closes.astimezone(kr_tz)
        return f"미국장 개장 중{early} · 마감까지 {hh}시간 {mm}분 (한국 {kr:%H:%M})"

    nxt = next_trading_day(now_ny.date(), inclusive=now_ny.time() < dt.time(9, 30))
    opens = dt.datetime.combine(nxt, dt.time(9, 30), ny_tz).astimezone(kr_tz)
    why = "" if is_trading_day(now_ny.date()) else " (휴장일)"
    return f"미국장 마감{why} · 다음 개장 한국 {opens:%m/%d(%a) %H:%M}"


def _cmd_status(args: argparse.Namespace, cfg: StrategyConfig) -> int:
    import datetime as dt

    from koru_trade.live import StateStore

    store = StateStore(args.state)
    pos = store.load_position(cfg.symbol)
    risk = store.load_risk(dt.date.today())

    print("=" * 74)
    print("  현재 상태")
    print("=" * 74)
    print(f"  {_market_status_line()}")
    print()
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
    # round_trip_drag 는 수수료+환전만이다. 슬리피지는 체결가에 붙으므로
    # 여기서 더해 주지 않으면 실제보다 싸 보인다. AGENTS.md 도메인 함정 참고.
    slip = 2.0 * cfg.cost.slippage_rate
    print(
        f"  왕복 거래비용     {cfg.cost.round_trip_drag + slip:.3%}"
        f"  (수수료·환전 {cfg.cost.round_trip_drag:.3%} + 슬리피지 {slip:.3%})"
    )
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


def _cmd_notify_test(args: argparse.Namespace, cfg: StrategyConfig) -> int:  # noqa: ARG001
    """텔레그램 자격증명을 확인하고 테스트 메시지를 보낸다."""
    from koru_trade.notify import TelegramNotifier, load_telegram_config

    conf = load_telegram_config()
    if conf is None:
        print("텔레그램이 설정되지 않았다.")
        print("  .env 에 TELEGRAM_BOT_TOKEN 과 TELEGRAM_CHAT_ID 를 채워라.")
        print("  자세한 방법은 .env.example 주석 참조.")
        return 1

    print(f"설정 확인: {conf}")
    notifier = TelegramNotifier(conf)
    check = notifier.check()
    print(f"  연결: {'OK' if check.ok else '실패'} — {check.detail}")
    if not check.ok:
        return 2

    body = "\n".join(
        [
            "✅ <b>KORU 알림 연결됨</b>",
            "",
            f"종목   {cfg.symbol}",
            f"1회 예산 {cfg.capital_krw:,.0f}원",
            "익절   원화 " + " / ".join(f"{s.krw_return:+.0%}" for s in cfg.take_profit),
            f"손절   원화 {cfg.hard_stop_krw_return:+.0%}",
            "",
            "<i>이제 매수/매도 신호가 나면 여기로 알려준다.</i>",
        ]
    )
    result = notifier.send(body)
    print(f"  발송: {'OK' if result.ok else '실패'} — {result.detail}")
    return 0 if result.ok else 3


def _cmd_watch(args: argparse.Namespace, cfg: StrategyConfig) -> int:
    """주기적으로 시세를 받아 판단하고, 신호가 나면 알림을 보낸다."""
    import time
    from pathlib import Path as _P

    from koru_trade.live import LiveRunner, StateStore
    from koru_trade.notify.format import format_error, format_mismatch

    notifier = _build_notifier()
    store = StateStore(args.state)
    broker = _make_broker(args, cfg)
    _restore_paper_position(broker, store, cfg)
    runner = LiveRunner(broker, store, cfg, notifier=notifier)
    # 시작할 때 한 번 대조한다. 어긋나 있으면 첫 주문부터 거절될 수 있다.
    # 자동으로 고치지 않는다(원인에 따라 대응이 달라서) — 사람에게 알린다.
    mismatch = runner.reconcile()
    if mismatch:
        print(f"  [경고] {mismatch}", flush=True)
        notifier.send(format_mismatch(mismatch, cfg), dedupe_key=f"mismatch-{mismatch}")

    print("=" * 74)
    print(f"  KORU 감시 시작 · {cfg.symbol}")
    print("=" * 74)
    print(f"  주기      {args.interval}초")
    print(f"  알림      {'텔레그램 연결됨' if notifier.enabled else '꺼짐 (.env 미설정)'}")
    print(f"  상태 DB   {_P(args.state).resolve()}")
    print("  중지      Ctrl+C")
    print(flush=True)

    ticks = 0
    while True:
        ticks += 1
        try:
            bars = _load(args, cfg)
            result = runner.tick(bars)
            stamp = __import__("datetime").datetime.now().strftime("%H:%M:%S")
            # flush 하지 않으면 파이프로 넘길 때 출력이 버퍼에 갇혀
            # 몇 시간 동안 아무것도 안 보인다. 멈춘 것과 구분이 안 된다.
            print(f"[{stamp}] #{ticks} {result.summary()}", flush=True)
        except KeyboardInterrupt:
            print("\n감시를 중단한다.")
            return 130
        except Exception as exc:
            logger.error("점검 실패: %s: %s", type(exc).__name__, exc)
            # dedupe 키가 없으면 네트워크 장애처럼 계속 실패하는 상황에서
            # 주기마다 알림이 나간다(15분 주기면 하루 96통). 같은 오류는
            # 하루 한 번만 알린다 — 차단 알림과 같은 규칙이다.
            day = __import__("datetime").date.today().isoformat()
            notifier.send(
                format_error(exc, context="watch"),
                dedupe_key=f"error-{type(exc).__name__}-{day}",
            )

        if args.once:
            return 0
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n감시를 중단한다.")
            return 130


def _restore_paper_position(broker: Broker, store: StateStore, cfg: StrategyConfig) -> None:
    """페이퍼 브로커의 가상 잔고를 상태 DB 포지션으로 맞춘다.

    페이퍼 브로커는 잔고를 메모리에만 들고 있어 프로세스가 뜰 때마다 0주다.
    맞추지 않으면 재시작 뒤 첫 매도가 "보유 0주" 로 거절되고, DB 는 계속
    포지션이 있다고 믿어 진입 검사까지 멈춘다(2026-09-09~11 실제 사고).

    **실브로커에는 아무것도 하지 않는다.** 실계좌 잔고를 로컬 기록으로 덮으면 안 된다.
    """
    from koru_trade.broker.paper import PaperBroker

    if not isinstance(broker, PaperBroker):
        return
    pos = store.load_position(cfg.symbol)
    if not pos.is_open:
        return
    broker.seed_position(cfg.symbol, pos.qty, pos.avg_price_usd)
    logger.info(
        "페이퍼 브로커 잔고를 상태 DB 로 복원했다: %s %d주 @ $%.2f",
        cfg.symbol,
        pos.qty,
        pos.avg_price_usd,
    )


def _make_broker(args: argparse.Namespace, cfg: StrategyConfig) -> Broker:
    """CLI 인자에 맞는 브로커를 만든다."""
    from koru_trade.broker.paper import PaperBroker
    from koru_trade.models import Quote

    if getattr(args, "paper", False):
        bars = _load(args, cfg)
        last = bars[-1]

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
        return broker

    from koru_trade.broker.kis import KisBroker

    cred = load_credentials()
    return KisBroker(cred, exchange=cfg.exchange, dry_run=is_dry_run())


def _cmd_web(args: argparse.Namespace, cfg: StrategyConfig) -> int:
    """로컬 대시보드를 띄운다. 루프백에만 바인딩된다."""
    from pathlib import Path as _P

    from koru_trade.live import StateStore
    from koru_trade.web import DashboardServer
    from koru_trade.web.server import DEFAULT_TTL

    bars = _load(args, cfg)
    store = StateStore(args.state) if _P(args.state).exists() else None
    if store is None:
        print(f"상태 DB 가 없다({args.state}). 실계좌 구역은 비어 있게 표시된다.")

    refresh = float(getattr(args, "refresh", 0.0) or 0.0)
    server = DashboardServer(
        bars,
        cfg,
        store=store,
        port=args.port,
        ttl=refresh if refresh > 0 else DEFAULT_TTL,
        bars_provider=(lambda: _load(args, cfg)) if refresh > 0 else None,
    )
    print()
    print("=" * 74)
    print(f"  KORU 대시보드가 열렸다 -> {server.url}")
    print("=" * 74)
    print(
        f"  종목      {cfg.symbol}  ({bars[0].ts:%Y-%m-%d} ~ {bars[-1].ts:%Y-%m-%d}, {len(bars)}봉)"
    )
    print("  바인딩    127.0.0.1 전용 (다른 기기에서는 접근 불가)")
    print(
        "  시세 갱신  "
        + (f"{refresh:,.0f}초마다 다시 받는다" if refresh > 0 else "없음(켤 때 받은 시세 고정)")
    )
    print("  중지      Ctrl+C")
    print()
    if args.open:
        import threading
        import webbrowser

        threading.Timer(0.8, lambda: webbrowser.open(server.url)).start()
    server.serve_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
