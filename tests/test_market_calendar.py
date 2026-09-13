"""미국 거래일 달력 검증.

여기 있는 날짜들은 NYSE 가 실제로 쉰 날이다. 규칙을 바꾸면 이 테스트가 깨진다.
"""

from __future__ import annotations

import datetime as dt

import pytest

from koru_trade.market_calendar import (
    early_close_days,
    is_early_close,
    is_trading_day,
    market_holidays,
    next_trading_day,
    previous_trading_day,
    trading_days_between,
)

# NYSE 실제 휴장일. 출처: NYSE 연간 휴장 공고.
_KNOWN = {
    2025: [
        (1, 1, "신정"),
        (1, 9, "카터 국장일"),
        (1, 20, "MLK"),
        (2, 17, "대통령의 날"),
        (4, 18, "성금요일"),
        (5, 26, "현충일"),
        (6, 19, "6월19일"),
        (7, 4, "독립기념일"),
        (9, 1, "노동절"),
        (11, 27, "추수감사절"),
        (12, 25, "성탄절"),
    ],
    2026: [
        (1, 1, "신정"),
        (1, 19, "MLK"),
        (2, 16, "대통령의 날"),
        (4, 3, "성금요일"),
        (5, 25, "현충일"),
        (6, 19, "6월19일"),
        (7, 3, "독립기념일(7/4 토 -> 금 대체)"),
        (9, 7, "노동절"),
        (11, 26, "추수감사절"),
        (12, 25, "성탄절"),
    ],
}


@pytest.mark.parametrize("year", sorted(_KNOWN))
def test_실제_휴장일과_정확히_일치한다(year: int) -> None:
    expected = {dt.date(year, m, d) for m, d, _ in _KNOWN[year]}
    assert market_holidays(year) == expected


@pytest.mark.parametrize(
    ("year", "month", "day", "name"),
    [(y, m, d, n) for y, items in _KNOWN.items() for m, d, n in items],
)
def test_휴장일은_거래일이_아니다(year: int, month: int, day: int, name: str) -> None:
    assert not is_trading_day(dt.date(year, month, day)), name


def test_노동절이_거래일로_새지_않는다() -> None:
    """이 버그 때문에 2026-09-07 을 평일로 안내한 적이 있다."""
    assert not is_trading_day(dt.date(2026, 9, 7))
    assert is_trading_day(dt.date(2026, 9, 8))
    assert next_trading_day(dt.date(2026, 9, 7)) == dt.date(2026, 9, 8)
    assert previous_trading_day(dt.date(2026, 9, 7)) == dt.date(2026, 9, 4)


def test_주말은_거래일이_아니다() -> None:
    assert not is_trading_day(dt.date(2026, 9, 5))  # 토
    assert not is_trading_day(dt.date(2026, 9, 6))  # 일
    assert is_trading_day(dt.date(2026, 9, 4))  # 금


def test_토요일_공휴일은_직전_금요일로_당겨진다() -> None:
    """2026-07-04 는 토요일이라 7/3(금)에 쉰다."""
    assert dt.date(2026, 7, 3) in market_holidays(2026)
    assert dt.date(2026, 7, 4) not in market_holidays(2026)


def test_일요일_공휴일은_다음_월요일로_밀린다() -> None:
    """2027-12-25 는 토요일 -> 12/24(금). 2022-12-25 는 일요일 -> 12/26(월)."""
    assert dt.date(2022, 12, 26) in market_holidays(2022)


def test_신정이_토요일이면_전년도_금요일에_쉬지_않는다() -> None:
    """NYSE 규칙: 1월 1일이 토요일이면 직전 금요일(전년 12/31)은 정상 개장."""
    assert dt.date(2022, 1, 1).weekday() == 5
    assert dt.date(2021, 12, 31) not in market_holidays(2021)
    assert not any(d.month == 1 and d.day <= 2 for d in market_holidays(2022) if d.day == 1)


def test_juneteenth_는_2022년부터다() -> None:
    assert dt.date(2021, 6, 18) not in market_holidays(2021)
    assert dt.date(2022, 6, 20) in market_holidays(2022)  # 6/19 일요일 -> 월


def test_성금요일이_부활절_이틀_전이다() -> None:
    # 2024 부활절 3/31 -> 성금요일 3/29
    assert dt.date(2024, 3, 29) in market_holidays(2024)
    # 2025 부활절 4/20 -> 4/18
    assert dt.date(2025, 4, 18) in market_holidays(2025)


def test_조기폐장일() -> None:
    assert is_early_close(dt.date(2026, 11, 27))  # 추수감사절 다음 금
    assert is_early_close(dt.date(2026, 12, 24))  # 크리스마스 이브
    assert not is_early_close(dt.date(2026, 9, 8))
    # 조기폐장일은 휴장일과 겹치지 않는다
    for year in (2024, 2025, 2026, 2027):
        assert not (early_close_days(year) & market_holidays(year))


class TestTradingDaysBetween:
    def test_시작일은_세지_않고_종료일은_센다(self) -> None:
        # 화 -> 수 = 1 거래일
        assert trading_days_between(dt.date(2026, 9, 8), dt.date(2026, 9, 9)) == 1

    def test_주말을_건너뛴다(self) -> None:
        # 금(9/4) -> 화(9/8): 토·일 제외, 월(9/7)은 노동절 -> 화 하나뿐
        assert trading_days_between(dt.date(2026, 9, 4), dt.date(2026, 9, 8)) == 1

    def test_휴장일을_건너뛴다(self) -> None:
        """이게 핵심이다. 노동절을 세면 2가 나온다."""
        assert trading_days_between(dt.date(2026, 9, 4), dt.date(2026, 9, 9)) == 2

    def test_역순이거나_같으면_0(self) -> None:
        assert trading_days_between(dt.date(2026, 9, 9), dt.date(2026, 9, 4)) == 0
        assert trading_days_between(dt.date(2026, 9, 4), dt.date(2026, 9, 4)) == 0

    def test_추수감사절_주간(self) -> None:
        # 2026-11-25(수) -> 11-30(월): 26일 휴장, 27일 거래(조기폐장), 28·29 주말
        assert trading_days_between(dt.date(2026, 11, 25), dt.date(2026, 11, 30)) == 2


def test_business_days_between_이_달력을_쓴다() -> None:
    """strategy 의 타임스톱이 휴장일을 세면 안 된다."""
    from koru_trade.strategy import business_days_between

    fri = dt.datetime(2026, 9, 4, 16, 0)
    wed = dt.datetime(2026, 9, 9, 16, 0)
    # 노동절을 세면 3, 안 세면 2
    assert business_days_between(fri, wed) == 2


def test_add_business_days_가_달력을_쓴다() -> None:
    """쿨다운이 휴장일에 풀리면 안 된다."""
    from koru_trade.risk import add_business_days

    # 금(9/4)에서 2거래일 -> 월(9/7)은 노동절이라 화(9/8), 수(9/9)
    assert add_business_days(dt.date(2026, 9, 4), 2) == dt.date(2026, 9, 9)


def test_연속_휴장에도_다음_거래일을_찾는다() -> None:
    for year in (2024, 2025, 2026, 2027):
        for d in sorted(market_holidays(year)):
            nxt = next_trading_day(d)
            assert is_trading_day(nxt)
            assert nxt > d
