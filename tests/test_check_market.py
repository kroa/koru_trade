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

from check_market import BoxRange, compute_box_range, format_report  # noqa: E402

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


def test_리포트_포맷_조립() -> None:
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
    report = format_report(
        when=when,
        last_bar=bars[0],
        cur_price=22.50,
        price_source="live",
        limit_price=22.53,
        allowed=True,
        blockers=[],
        box=box,
        gemini_summary="테스트 AI 요약입니다.",
    )

    assert "KORU 미국 본장 개장 전 체크" in report
    assert "10/05(월) 22:30" in report
    assert "🟢 [매수 허용]" in report
    assert "테스트 조언 문구" in report
    assert "Google Gemini AI 마켓 인사이트:" in report
    assert "테스트 AI 요약입니다." in report
