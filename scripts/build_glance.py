"""KORU "한눈에" 페이지를 만든다.

    python scripts/build_glance.py
    python scripts/build_glance.py --public --out docs/glance.html

상황실(``build_desk.py``)이 규칙과 계산을 전부 펼쳐 보여준다면, 이 페이지는
질문 하나에만 답한다 — **지금 사야 하나, 말아야 하나.**

그래서 여기서는 지표 이름을 화면에 내보내지 않는다. EMA·ATR·RSI·ADX 는 전부
일상어로 번역해서 싣는다. 번역표는 :data:`PLAIN` 한 곳에만 둔다. 두 군데에
적어 두면 한쪽만 고치게 되고, 그러면 화면이 조용히 옛말을 하게 된다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import math
import statistics as st
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from _site_common import (  # noqa: E402
    KST,
    anonymize,
    next_open_kst,
    render,
    resolve_config,
    threshold_close,
)

from koru_trade import indicators as ind  # noqa: E402
from koru_trade.config import StrategyConfig  # noqa: E402
from koru_trade.data import load_bars  # noqa: E402
from koru_trade.models import Bar  # noqa: E402
from koru_trade.strategy import evaluate_entry  # noqa: E402

TEMPLATE = ROOT / "scripts" / "glance_template.html"
DEFAULT_OUT = ROOT / "build" / "koru_glance_out.html"

CHART_BARS = 20
"""차트에 그리는 거래일 수. 한 달치면 최근 흐름이 보이고 화면도 안 빽빽하다."""

PLAIN: dict[str, str] = {
    "데이터충분성": "판단할 기록이 충분한가",
    "변동성레짐": "출렁임이 감당할 수준인가",
    "추세방향": "값이 오르는 중인가",
    "모멘텀(RSI)": "너무 달아오르지 않았나",
    "추세강도(ADX)": "방향이 있는가",
    "시가갭": "장 열릴 때 튀지 않았나",
    "유동성": "사고팔기 쉬운가",
    "환율추세": "환율이 발목 잡지 않나",
}
"""지표 이름 -> 일상어. 화면에는 오른쪽만 나간다.

왼쪽 키는 :func:`~koru_trade.strategy.evaluate_entry` 가 돌려주는 검사 이름과
정확히 같아야 한다. 이름이 바뀌면 :func:`build_payload` 가 예외로 알려 준다 —
조용히 영문 지표명이 화면에 나가는 것보다 낫다.
"""

WHY: dict[str, str] = {
    "추세방향": "값이 최근 10일 평균보다 아래라는 뜻이고, 아직 내려가는 중이라는 신호다. "
    "3배짜리는 내려갈 때 세 배로 내려가서, 이럴 때는 사지 않는다.",
    "변동성레짐": "하루 출렁임이 너무 커서, 팔 자리와 손절 자리가 하루 안에 다 스칠 수 있다.",
    "모멘텀(RSI)": "값이 한쪽으로 지나치게 쏠려 있다. 되돌림을 맞기 쉬운 자리다.",
    "시가갭": "장이 열리자마자 너무 크게 튀었다. 미리 정해 둔 손절 자리를 건너뛰고 열릴 수 있다.",
    "유동성": "거래가 한산해서, 팔고 싶을 때 제값에 못 팔 수 있다.",
    "환율추세": "값이 올라도 환율이 빠지면 원화로는 남는 게 없다.",
    "추세강도": "방향 없이 흔들리기만 하는 장이다.",
    "데이터충분성": "기록이 모자라 판단을 미룬다.",
}
"""막힌 조건을 왜 막았는지 한 문단으로. 키는 검사 이름의 앞부분과 맞춰 찾는다."""


def _correlation(a: list[float], b: list[float]) -> float:
    """두 수익률 계열의 상관계수. 표본이 모자라면 0.

    ``statistics.correlation`` 은 길이가 다르거나 한쪽 분산이 0 이면 던진다.
    환율 한 줄 때문에 페이지 전체가 안 만들어지면 안 되므로 여기서 막는다.
    """
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    try:
        return st.correlation(a[-n:], b[-n:])
    except st.StatisticsError:
        return 0.0


def _why(name: str) -> str:
    for key, text in WHY.items():
        if name.startswith(key):
            return text
    return "오늘은 이 조건이 맞지 않는다."


def build_payload(
    bars: Sequence[Bar], cfg: StrategyConfig, *, now: dt.datetime | None = None
) -> dict[str, Any]:
    """페이지가 읽는 JSON. 네트워크를 쓰지 않는 순수 함수다."""
    closes = [b.close for b in bars]
    signal = evaluate_entry(bars, cfg)
    stamp = (now or dt.datetime.now(KST)).astimezone(KST)

    # 각 봉 시점의 기준선(빠른 이평)을 그때그때 다시 계산한다. 오늘 값 하나만
    # 쓰면 "선이 날마다 내려온다" 는 이 페이지의 핵심 설명이 그림에서 사라진다.
    series = []
    for i in range(len(bars)):
        level = ind.ema(closes[: i + 1], cfg.ema_fast)
        if level is None:
            continue
        series.append(
            {"d": bars[i].ts.strftime("%m/%d"), "c": round(closes[i], 2), "l": round(level, 2)}
        )
    window = series[-CHART_BARS:]
    if not window:
        raise ValueError("차트에 그릴 봉이 없다. 시세 조회 기간을 늘려라")

    price = closes[-1]
    line = ind.ema(closes, cfg.ema_fast) or price
    gap = max(0.0, line - price)

    checks = []
    for c in signal.checks:
        plain = PLAIN.get(c.name)
        if plain is None:
            raise ValueError(
                f"검사 이름 {c.name!r} 의 일상어 번역이 PLAIN 에 없다. "
                "번역을 추가하지 않으면 화면에 지표 이름이 그대로 나간다"
            )
        checks.append({"ok": bool(c.passed), "plain": plain})

    blocked = [c for c in signal.checks if not c.passed]
    highs = [x["c"] for x in window]

    # 환율. 원화 수익률에 곱으로 들어가는데 조건표에는 통과/실패 한 칸으로만
    # 나와서, 지금 어느 쪽으로 가고 있는지가 화면에서 사라져 있었다.
    fx = [b.fx_rate for b in bars]
    fx_win = fx[-CHART_BARS:]
    ago20 = fx[-CHART_BARS] if len(fx) >= CHART_BARS else fx[0]
    ago60 = fx[-61] if len(fx) > 61 else fx[0]
    lo_fx, hi_fx = min(fx), max(fx)
    corr = _correlation(
        [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))],
        [fx[i] / fx[i - 1] - 1.0 for i in range(1, len(fx))],
    )

    return {
        # 확정 봉 날짜. 화면에는 안 쓰지만 build_public 의 시세 후퇴 차단이
        # 이 값을 읽는다. 없으면 비교 대상이 "?" 가 되어 검사가 조용히 통과한다.
        "bar": bars[-1].ts.strftime("%Y-%m-%d"),
        "stamp": stamp.strftime("%Y.%m.%d") + " 판",
        "allowed": bool(signal.allowed),
        "price": round(price, 2),
        "line": round(line, 2),
        # 반올림이 아니라 올림이다. "2센트 모자란다" 고 적어 놓고 실제 차이가
        # 2.4센트면, 그 값에 딱 맞춰 산 사람이 조건을 못 넘는다. 올림은 살짝
        # 과하게 말할 뿐이라 그 방향으로는 사람이 손해 보지 않는다.
        "gapCents": math.ceil(gap * 100) if gap > 0 else 0,
        "gapPct": round(gap / price * 100.0, 4) if price else 0.0,
        "series": window,
        "swingPct": round((max(highs) / min(highs) - 1.0) * 100.0, 1) if min(highs) else 0.0,
        "checks": checks,
        "fx": {
            "now": round(fx[-1]),
            "from": bars[-CHART_BARS].ts.strftime("%m/%d"),
            "to": bars[-1].ts.strftime("%m/%d"),
            "series": [round(v) for v in fx_win],
            "change20": round(fx[-1] / ago20 - 1.0, 4) if ago20 else 0.0,
            "change60": round(fx[-1] / ago60 - 1.0, 4) if ago60 else 0.0,
            "ago60": round(ago60),
            "pctile": round((fx[-1] - lo_fx) / (hi_fx - lo_fx), 3) if hi_fx > lo_fx else 0.5,
            "corr": round(corr, 2),
        },
        "missingWhy": _why(blocked[0].name) if blocked else "",
        "nextOpen": next_open_kst(now),
        # 다음 장 종가가 이 값 이상이어야 그다음 개장에 사도 된다(또는 초록불이
        # 유지된다). 빠른 평균값과 다를 수 있다 — _site_common.threshold_close 참고.
        "flipAt": round(threshold_close(bars, cfg), 2),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KORU 한눈에 페이지 생성")
    parser.add_argument("--config", help="전략 설정 YAML 경로")
    parser.add_argument("--period", default="6mo", help="시세 조회 기간 (기본 6mo)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="출력 HTML 경로")
    parser.add_argument("--public", action="store_true", help="공개 배포용")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="  %(levelname)s %(message)s")

    cfg, cfg_path = resolve_config(args.config)
    print(f"전략 설정: {cfg_path}")
    if args.public:
        cfg = anonymize(cfg)

    bars = load_bars(cfg.symbol, period=args.period, cache_dir=None)
    payload = build_payload(bars, cfg)
    out = render(payload, TEMPLATE, Path(args.out))

    passed = sum(1 for c in payload["checks"] if c["ok"])
    verdict = "사도 된다" if payload["allowed"] else "사지 마라"
    print(f"생성 완료: {out}  ({out.stat().st_size:,} bytes)")
    print(f"  판정 {verdict} · 조건 {passed}/{len(payload['checks'])} 통과")
    print(
        f"  지금 ${payload['price']} · 기준선 ${payload['line']} · {payload['gapCents']}센트 차이"
    )
    print(
        f"  차트 {payload['series'][0]['d']}~{payload['series'][-1]['d']} ({len(payload['series'])}봉)"
    )
    print(f"  다음 개장 {payload['nextOpen']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
