"""KORU "상황실" 페이지를 만든다.

``scripts/desk_template.html`` 의 ``__DATA__`` 자리에 지금 시장 상태를 끼워 넣어
``build/koru_desk.html`` 을 만든다. 같은 경로로 재발행하면 아티팩트 URL 이 유지된다.

    python scripts/build_desk.py
    python scripts/build_desk.py --config config/frequent.example.yaml --out build/desk.html

신호 원장(``build_site.py``)이 "과거에 어떤 신호가 났나" 를 보여준다면, 이 페이지는
"지금 어떤 상태인가" 만 보여준다. 확정된 마지막 봉 기준이며 장중 가격이 아니다.

이 파일이 저장소에 있어야 하는 이유
-----------------------------------
2026-09-19 까지 이 페이지는 임시 스크립트로만 만들어져 있었다. 저장소에 없으니
아무도 갱신할 수 없었고, 화면은 과거 어느 시점에 멈춰 있으면서 최신인 척했다.
"죽은 화면" 과 "멈춘 화면" 은 보는 사람 입장에서 구분되지 않는다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import math
import sys
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from _site_common import KST, anonymize, next_open_kst, render, resolve_config  # noqa: E402

from koru_trade import indicators as ind  # noqa: E402
from koru_trade.config import StrategyConfig  # noqa: E402
from koru_trade.data import load_bars  # noqa: E402
from koru_trade.models import Bar  # noqa: E402
from koru_trade.strategy import evaluate_entry  # noqa: E402

TEMPLATE = ROOT / "scripts" / "desk_template.html"
DEFAULT_OUT = ROOT / "build" / "koru_desk.html"

LEVERAGE = 3.0
"""KORU 의 목표 배수. 감쇠식의 L."""

VOL_WINDOW = 60
"""기초지수 변동성을 재는 거래일 수."""

RECENT_BARS = 10
"""페이지가 표에 쓰는 최근 봉 개수."""

UNDERLYING_TICKER = "EWY"
"""KORU 의 기초지수(MSCI South Korea)를 추종하는 1배 ETF."""


def decay_daily(sigma: float) -> float:
    """레버리지 ETF 의 하루 변동성 감쇠(음수).

    ``-0.5 * (L^2 - L) * sigma^2``. L=3 이면 ``-3 * sigma^2`` 다.
    3배 상품은 매일 리밸런싱하므로 기초지수가 제자리로 돌아와도 이만큼 녹는다.
    방향과 무관한 비용이라, 보유 기간을 늘리는 판단은 이 값을 근거로 반박해야 한다.
    """
    return -0.5 * (LEVERAGE**2 - LEVERAGE) * sigma**2


def fallback_daily_vol(bars: Sequence[Bar]) -> float:
    """EWY 를 못 받았을 때 KORU 자체 변동성에서 기초지수 변동성을 역산한다.

    KORU 는 EWY 일간수익률의 3배를 추종하므로 ``sigma_EWY ~ sigma_KORU / L`` 이다.

    왜 예외로 죽지 않고 근사하나: 이 페이지의 본체는 진입 판정과 익절/손절 가격이고
    감쇠 수치는 참고값이다. 네트워크 한 번 실패했다고 페이지 전체를 못 만들면,
    사람은 갱신을 포기하고 다시 멈춘 화면을 보게 된다. 대신 어느 쪽을 썼는지는
    반드시 출력해서 근사값을 실측값으로 착각하지 않게 한다.
    """
    closes = [b.close for b in bars[-(VOL_WINDOW + 1) :]]
    rets = ind.returns(closes)
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) / LEVERAGE


def underlying_daily_vol(bars: Sequence[Bar]) -> tuple[float, str]:
    """기초지수의 일간 수익률 표준편차와 그 출처.

    출처를 함께 돌려주는 이유: 근사값을 실측값으로 착각하면 감쇠가 sigma 제곱이라
    오차가 제곱으로 커진다. 페이지가 "EWY 60일" 이라고 단정해 놓고 실제로는
    근사였던 적이 있으면, 그걸로 "며칠 들고 갈까" 를 판단하게 된다.

    Returns:
        (표준편차, 출처). 출처는 ``"ewy"``(실데이터) / ``"approx"``(KORU 변동성/3) /
        ``"none"``(표본이 모자라 감쇠를 계산할 수 없음).
    """
    try:
        import yfinance as yf

        hist = yf.Ticker(UNDERLYING_TICKER).history(period="3mo")
        rets = hist["Close"].pct_change().dropna().tail(VOL_WINDOW)
        if len(rets) < 20:
            raise ValueError(f"{UNDERLYING_TICKER} 수익률 표본이 {len(rets)}개뿐이다")
        sigma = float(rets.std())
        if not math.isfinite(sigma) or sigma <= 0:
            raise ValueError(f"표준편차가 비정상이다: {sigma}")
    except Exception as exc:
        print(
            f"  경고: {UNDERLYING_TICKER} 변동성을 받지 못했다({type(exc).__name__}). "
            f"KORU 변동성/{LEVERAGE:.0f} 로 근사한다"
        )
        approx = fallback_daily_vol(bars)
        # 표본이 모자라면 0.0 이 온다. 그대로 두면 감쇠가 0 이 되어 페이지가
        # 3배 상품에 대고 "안 녹는다" 고 말한다. 0 은 값이 아니라 결측이다.
        return (approx, "approx") if approx > 0 else (0.0, "none")
    return sigma, "ewy"


def build_payload(
    bars: Sequence[Bar],
    cfg: StrategyConfig,
    ewy_vol: float,
    *,
    vol_source: str = "ewy",
    config_name: str = "",
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """페이지가 읽는 JSON 전체를 만든다.

    **네트워크를 쓰지 않는 순수 함수다.** 변동성은 인자로 받는다. 그래야
    계약 테스트가 시세 서버 없이 이 함수를 그대로 부를 수 있다.

    Args:
        bars: 시간 오름차순 봉. 마지막 봉이 "확정 봉" 이 된다.
        cfg: 전략 설정. 규칙 수치는 전부 여기서만 읽는다.
        ewy_vol: 기초지수 일간 수익률 표준편차.
        vol_source: 그 값의 출처("ewy"/"approx"/"none"). 페이지가 그대로 표시한다.
        config_name: 어떤 설정 파일로 만들었는지. 로컬과 클라우드가 갈라질 수 있다.
        now: 갱신 시각. None 이면 현재 한국시간.
    """
    closes = [b.close for b in bars]
    last = bars[-1]
    ema_fast = ind.ema(closes, cfg.ema_fast) or 0.0
    ema_slow = ind.ema(closes, cfg.ema_slow) or 0.0
    signal = evaluate_entry(bars, cfg)
    stamp = (now or dt.datetime.now(KST)).astimezone(KST)

    # 수익률을 내려면 직전 봉이 필요하므로 한 개 더 떠서 앞에서부터 짝을 짓는다.
    window = bars[-(RECENT_BARS + 1) :]
    recent = [
        {
            "d": cur.ts.strftime("%m/%d"),
            "c": round(cur.close, 2),
            "r": round(cur.close / prev.close - 1.0, 4),
            "fx": round(cur.fx_rate),
        }
        for prev, cur in pairwise(window)
    ]

    return {
        "updated": stamp.strftime("%Y-%m-%d %H:%M"),
        "bar": last.ts.strftime("%Y-%m-%d"),
        "price": round(last.close, 2),
        # 페이지가 D.fx.toLocaleString 을 부른다. 문자열로 넣으면 화면이 깨진다.
        "fx": round(last.fx_rate, 1),
        "atr": round(ind.atr(bars, cfg.atr_period) or 0.0, 2),
        "atrPct": round(ind.atr_pct(bars, cfg.atr_period) or 0.0, 4),
        "ema10": round(ema_fast, 2),
        "ema30": round(ema_slow, 2),
        "emaGap": round(ema_fast / ema_slow - 1.0, 4) if ema_slow else 0.0,
        "rsi": round(ind.rsi(closes, cfg.rsi_period) or 0.0, 1),
        "adx": round(ind.adx(bars, 14) or 0.0, 1),
        "allowed": bool(signal.allowed),
        "blockers": [c.name for c in signal.blockers],
        "checks": [
            {"name": c.name, "ok": bool(c.passed), "detail": c.detail} for c in signal.checks
        ],
        "nextOpen": next_open_kst(now),
        # 5자리로 반올림하면 sigma 가 0.0013 아래일 때 -0.0 이 된다(실측).
        # 그러면 페이지가 "하루 -0.00%, 20거래일 0.0%" 라고 쓴다.
        "decayDaily": round(decay_daily(ewy_vol), 6),
        "ewyVol": round(ewy_vol, 4),
        "ewyVolSource": vol_source,
        "configName": config_name,
        "cost": {
            # ★ 페이지의 krwReturn 은 pnl.krw_cost/krw_proceeds 를 그대로 옮긴 식이다.
            # 매수측만 보내던 시절에는 페이지가 그 한 쌍을 매도측에도 써서
            # 본전·손절·익절 6단 가격이 저장소 엔진과 갈라졌다(대칭 설정에서도 0.29%p).
            # 스프레드는 반드시 effective_* 를 보낸다 — fx_mode 가 HOLD_USD 면
            # 환전을 안 하므로 원시 스프레드를 보내면 없는 비용을 계산하게 된다
            # (왕복 0.641% vs 0.143%).
            "fee": round(cfg.cost.buy_fee_rate, 6),
            "sellFee": round(cfg.cost.sell_fee_rate, 6),
            "secFee": round(cfg.cost.sec_fee_rate, 8),
            "taf": round(cfg.cost.finra_taf_per_share, 8),
            "slip": round(cfg.cost.slippage_rate, 6),
            "fxSpread": round(cfg.cost.effective_buy_spread, 6),
            "fxSpreadSell": round(cfg.cost.effective_sell_spread, 6),
            # ★ round_trip_drag 는 수수료와 환전 스프레드만 센다. krw_cost/krw_proceeds
            # 가 슬리피지를 한 번도 참조하지 않기 때문이다(AGENTS.md 도메인 함정).
            # 슬리피지는 체결가 자체에 붙으므로 왕복분 2배를 여기서 더해야 실전과 같다.
            # 이걸 빼면 0.641% 가 나오는데 실제 왕복은 0.939% 다. 익절 1단이 +5% 인
            # 전략에서 0.3%p 는 결론을 뒤집는 크기다.
            "roundTrip": round(cfg.cost.round_trip_drag + 2.0 * cfg.cost.slippage_rate, 5),
        },
        "rules": {
            "hardStop": cfg.hard_stop_krw_return,
            "maxHold": cfg.max_holding_days,
            "stopAtr": cfg.stop_atr_multiple,
            "maxStopPct": cfg.max_stop_pct,
            "minStopPct": cfg.min_stop_pct,
            "capital": cfg.capital_krw,
            "maxPos": cfg.risk.max_position_krw,
            "ladder": [{"k": s.krw_return, "f": s.sell_fraction} for s in cfg.take_profit],
            "scaleIn": [{"m": s.atr_multiple, "w": s.weight} for s in cfg.scale_in],
            # 초록불 기준. 화면의 판정 옆에 "무엇을 넘어야 하는지" 를 같이
            # 보여주지 않으면, 막혔을 때 얼마나 모자란지 알 수가 없다.
            "gate": [
                {
                    "n": "추세방향",
                    "w": f"종가 > EMA{cfg.ema_fast} > EMA{cfg.ema_slow}",
                    "why": "내려가는 중에는 안 산다. 3배라 손실도 3배다",
                },
                {
                    "n": "변동성레짐",
                    "w": f"ATR/종가 ≤ {cfg.max_atr_pct:.0%}",
                    "why": "하루 진폭이 익절폭보다 크면 익절·손절이 구분되지 않는다",
                },
                {
                    "n": "모멘텀(RSI)",
                    "w": f"RSI{cfg.rsi_period} {cfg.min_rsi:.0f}~{cfg.max_rsi:.0f}",
                    "why": "너무 눌렸거나 너무 달아오른 구간을 뺀다",
                },
                {
                    "n": "추세강도(ADX)",
                    "w": f"ADX14 ≥ {cfg.min_adx:.0f}",
                    "why": "방향 없이 흔들리는 장을 거른다",
                },
                {
                    "n": "시가갭",
                    "w": f"시가 갭 ≤ ±{cfg.max_gap_pct:.0%}",
                    "why": "갭이 크면 손절선을 뛰어넘고 열린다",
                },
                {
                    "n": "유동성",
                    "w": f"20일 평균 거래대금 ≥ ${cfg.min_avg_dollar_volume:,.0f}",
                    "why": "못 팔고 갇히는 것을 막는다",
                },
                {
                    "n": "환율추세",
                    "w": f"USD/KRW 20일 변화 ≥ {cfg.max_fx_decline:.0%}",
                    "why": "주가가 올라도 환율이 빠지면 원화로는 손실이다",
                },
                {
                    "n": "데이터충분성",
                    "w": f"봉 {cfg.warmup_bars}개 이상",
                    "why": "지표가 덜 데워진 상태로 판단하지 않는다",
                },
            ],
            # 매도 사유. 우선순위 순서 그대로다(strategy._decide_open).
            "exits": [
                {
                    "n": "원화 하드스톱",
                    "w": f"원화 수익률 ≤ {cfg.hard_stop_krw_return:+.0%}",
                    "q": "전량",
                },
                {
                    "n": "달러 손절선",
                    "w": f"진입가 −{cfg.stop_atr_multiple:g}×ATR "
                    f"({cfg.min_stop_pct:.0%}~{cfg.max_stop_pct:.0%})",
                    "q": "전량",
                },
                {
                    "n": "본전 스톱",
                    "w": f"익절 {cfg.breakeven_stop_after_tp}단 후 본전 아래로",
                    "q": "전량",
                },
                {
                    "n": "트레일링",
                    "w": f"익절 {cfg.trailing_after_tp}단 후 고점 대비 "
                    f"{cfg.trailing_giveback:.0%} 반납",
                    "q": "전량",
                },
                {
                    "n": "추세 꺾임",
                    "w": (
                        f"이익 중({cfg.trend_break_min_krw_return:.2%} 초과) "
                        f"종가가 EMA{cfg.ema_fast} 아래"
                        if cfg.trend_break_min_krw_return is not None
                        else "꺼짐"
                    ),
                    "q": "전량",
                },
                {"n": "보유기간 만료", "w": f"{cfg.max_holding_days}영업일 경과", "q": "전량"},
                {"n": "분할 익절", "w": "아래 계단 도달", "q": "일부"},
            ],
            # 지표 용어. 조건표에 ATR·EMA·RSI·ADX 가 그냥 나오면 읽을 수가 없다.
            "glossary": [
                {
                    "t": "EMA",
                    "f": "지수이동평균",
                    "d": f"최근 가격에 더 무게를 준 평균값. EMA{cfg.ema_fast} 는 "
                    f"최근 {cfg.ema_fast}일, EMA{cfg.ema_slow} 는 {cfg.ema_slow}일 평균이다. "
                    "가격이 평균 위면 오르는 중, 아래면 내리는 중으로 본다.",
                    "now": "지금 가격이 EMA10 아래면 빨간불이다",
                },
                {
                    "t": "ATR",
                    "f": "평균 진폭",
                    "d": "하루에 위아래로 보통 얼마나 움직이는지를 잰 값. "
                    "ATR 이 $2 면 이 종목은 하루에 대략 2달러쯤 흔들린다는 뜻이다. "
                    "손절 폭과 분할매수 간격을 여기에 맞춰 잡는다.",
                    "now": "변동이 클수록 손절선을 넓게, 작을수록 좁게 잡는다",
                },
                {
                    "t": "RSI",
                    "f": "상대강도지수",
                    "d": "최근 오른 날의 힘과 내린 날의 힘을 견준 값. 0~100 이고 "
                    "50 이 중립이다. 너무 높으면 달아오른 상태, 너무 낮으면 "
                    "과하게 눌린 상태로 본다.",
                    "now": f"{cfg.min_rsi:.0f}~{cfg.max_rsi:.0f} 구간에서만 산다",
                },
                {
                    "t": "ADX",
                    "f": "추세강도지수",
                    "d": "방향이 얼마나 뚜렷한지만 재는 값. 위로 가는지 아래로 "
                    "가는지는 말해 주지 않고, 세기만 알려준다. 낮으면 방향 없이 "
                    "흔들리는 장이다.",
                    "now": f"하한 {cfg.min_adx:.0f} — 지금 설정은 사실상 이 조건을 안 쓴다",
                },
            ],
        },
        "recent": recent,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KORU 상황실 사이트 생성")
    parser.add_argument("--config", help="전략 설정 YAML 경로")
    parser.add_argument("--period", default="3y", help="시세 조회 기간 (기본 3y)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="출력 HTML 경로")
    parser.add_argument(
        "--public",
        action="store_true",
        help="공개 배포용. 실제 운용액을 기준 자본 100만원으로 환산한다",
    )
    args = parser.parse_args(argv)

    # 로더는 마지막 봉이 결측이면 경고한다. basicConfig 가 없으면 그 경고가
    # 아무 데도 안 나오고, 옛 봉으로 만든 페이지를 최신인 줄 알고 발행하게 된다.
    logging.basicConfig(level=logging.INFO, format="  %(levelname)s %(message)s")

    cfg, cfg_path = resolve_config(args.config)
    print(f"전략 설정: {cfg_path}")
    if args.public:
        cfg = anonymize(cfg)
        print(f"  공개 모드: 자본을 {cfg.capital_krw:,.0f}원 기준으로 환산했다")
    bars = load_bars(cfg.symbol, period=args.period, cache_dir=None)
    ewy_vol, vol_source = underlying_daily_vol(bars)
    payload = build_payload(bars, cfg, ewy_vol, vol_source=vol_source, config_name=cfg_path.name)
    out = render(payload, TEMPLATE, Path(args.out))

    state = "통과" if payload["allowed"] else "차단: " + ", ".join(payload["blockers"])
    source = {
        "ewy": f"{UNDERLYING_TICKER} 실데이터",
        "approx": f"KORU 변동성/{LEVERAGE:.0f} 근사",
        "none": "표본 부족 — 감쇠를 계산하지 못했다",
    }[vol_source]
    horizon = (1.0 - (1.0 + payload["decayDaily"]) ** 20) * 100.0
    print(f"생성 완료: {out}  ({out.stat().st_size:,} bytes)")
    print(f"  확정 봉 {payload['bar']} · 종가 ${payload['price']} · 환율 {payload['fx']:,.0f}원")
    print(f"  진입 게이트 {state}")
    print(f"  다음 개장 {payload['nextOpen']} (한국시간)")
    print(
        f"  하루 감쇠 {payload['decayDaily'] * 100:.3f}% (20거래일 약 {horizon:.1f}%) · "
        f"기초지수 변동성 {payload['ewyVol'] * 100:.2f}% [{source}]"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
