"""장전 체크 스크립트(scripts/check_market.py) 테스트.

네트워크는 쓰지 않는다. 박스권 계산 및 포맷팅 순수 함수를 검증한다.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from check_market import (  # noqa: E402
    BoxRange,
    compute_box_range,
    format_report,
    generate_trade_action_guide,
)

from koru_trade.models import Bar  # noqa: E402

KST = ZoneInfo("Asia/Seoul")


def _make_mock_bars(closes: list[float]) -> list[Bar]:
    base = dt.datetime(2026, 10, 1, 9, 30, tzinfo=ZoneInfo("America/New_York"))
    bars: list[Bar] = []
    for i, c in enumerate(closes):
        bars.append(
            Bar(
                ts=base + dt.timedelta(days=i),
                open=c,
                high=c + 1.0,
                low=c - 1.0,
                close=c,
                volume=1_000_000,
                fx_rate=1350.0,
            )
        )
    return bars


def test_박스권_상단_판정() -> None:
    # 20개 봉: 19.0 ~ 24.0 범위 (high=25.0, low=18.0)
    closes = [20.0] * 19 + [24.5]
    bars = _make_mock_bars(closes)
    box = compute_box_range(bars, current_price=24.5)

    assert box.zone == "상단"
    assert box.pct_20d >= 0.70
    assert "박스권 상단" in box.advice


def test_박스권_하단_판정() -> None:
    # low=17.0, high=25.0, 현재가=18.0
    closes = [22.0] * 19 + [18.0]
    bars = _make_mock_bars(closes)
    box = compute_box_range(bars, current_price=18.0)

    assert box.zone == "하단"
    assert box.pct_20d <= 0.30
    assert "박스권 하단" in box.advice


def test_매매_행동_가이드_생성() -> None:
    box_top = BoxRange(
        low_20d=18.0,
        high_20d=24.0,
        pct_20d=0.85,
        low_60d=15.0,
        high_60d=26.0,
        pct_60d=0.70,
        zone="상단",
        advice="상단 조언",
    )
    buy_g, sell_g = generate_trade_action_guide(
        allowed=True,
        blockers=[],
        box=box_top,
        cur_price=23.5,
        limit_price=23.0,
    )
    assert "추격 매수 금지" in buy_g
    assert "적극 분할 익절 구간" in sell_g

    # 차단 상태일 때
    buy_blocked, _ = generate_trade_action_guide(
        allowed=False,
        blockers=["추세방향"],
        box=box_top,
        cur_price=21.0,
        limit_price=21.0,
    )
    assert "매수 보류 / 관망" in buy_blocked
    assert "추세방향" in buy_blocked


def test_리포트_포맷_조립_오전_오후() -> None:
    bars = _make_mock_bars([22.0])
    box = BoxRange(
        low_20d=18.0,
        high_20d=24.0,
        pct_20d=0.75,
        low_60d=15.0,
        high_60d=26.0,
        pct_60d=0.65,
        zone="상단",
        advice="테스트 조언 문구",
    )
    when = {"open": "10/05(월) 22:30", "close": "10/06(화) 05:00"}

    # 1. 야간 개장 전 모드
    report_eve = format_report(
        when=when,
        last_bar=bars[0],
        cur_price=22.50,
        price_source="live",
        limit_price=22.53,
        allowed=True,
        blockers=[],
        box=box,
        gemini_summary="테스트 AI 요약입니다.",
        mode="premarket",
    )
    assert "KORU 본장 개장 전 체크" in report_eve
    assert "10/05(월) 22:30" in report_eve
    assert "🟢 [매수 허용]" in report_eve
    assert "지금 매수해야 할까?" in report_eve
    assert "지금 매도해야 할까?" in report_eve
    assert "Google Gemini AI 마켓 인사이트:" in report_eve

    # 2. 오전 모드
    report_morn = format_report(
        when=when,
        last_bar=bars[0],
        cur_price=22.50,
        price_source="live",
        limit_price=22.53,
        allowed=True,
        blockers=[],
        box=box,
        gemini_summary=None,
        mode="morning",
    )
    assert "KORU 오전 정기 브리핑" in report_morn
