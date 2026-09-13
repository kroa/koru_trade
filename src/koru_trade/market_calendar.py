"""미국 주식시장(NYSE/NASDAQ) 거래일 달력.

왜 필요한가
-----------
전략의 타임 스톱은 **영업일** 기준이다(``max_holding_days``). 그런데 영업일을
"주말이 아닌 날"로 세면 공휴일이 낀 구간에서 실제보다 짧게 센다. 그러면
청산해야 할 날에 청산하지 않고 하루를 더 들고 간다. 3배 레버리지에서
하룻밤은 ATR 10% 국면 기준으로 작은 차이가 아니다.

또 하나. 봇이 "다음 개장은 언제인가"를 모르면 사람에게 잘못 안내한다.
실제로 2026-09-07(노동절)을 평일로 알고 "월요일이면 판가름난다"고
안내한 적이 있다. 달력이 코드 안에 없으면 그 실수는 반복된다.

무엇을 다루는가
---------------
- 정규 휴장일 10종 (신정·MLK·대통령의날·성금요일·현충일·6월19일·
  독립기념일·노동절·추수감사절·성탄절)
- 토/일 대체 규칙 (토요일 -> 직전 금요일, 일요일 -> 다음 월요일)
- 조기 폐장일 (13:00 ET 마감) — 세션 마감 로직이 쓸 수 있게 별도로 제공한다

다루지 않는 것
--------------
대통령 서거 등 임시 휴장은 규칙이 없어 예측할 수 없다. 필요하면
:data:`AD_HOC_CLOSURES` 에 날짜를 추가하라. 과거 사례는 이미 넣어 두었다.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache

__all__ = [
    "AD_HOC_CLOSURES",
    "early_close_days",
    "is_early_close",
    "is_trading_day",
    "market_holidays",
    "next_trading_day",
    "previous_trading_day",
    "trading_days_between",
]


# 규칙으로 유도할 수 없는 임시 휴장. 발생하면 여기에 추가한다.
AD_HOC_CLOSURES: frozenset[dt.date] = frozenset(
    {
        dt.date(2012, 10, 29),  # 허리케인 샌디
        dt.date(2012, 10, 30),
        dt.date(2018, 12, 5),  # 조지 H. W. 부시 국장일
        dt.date(2025, 1, 9),  # 지미 카터 국장일
    }
)

# 6월 19일(Juneteenth)이 연방 공휴일이 된 해. 그 전에는 정상 거래일이었다.
_JUNETEENTH_FROM = 2022

# MLK 데이가 NYSE 휴장일이 된 해.
_MLK_FROM = 1998


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """그 달의 n 번째 특정 요일. weekday 는 월=0 기준."""
    d = dt.date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + dt.timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    """그 달의 마지막 특정 요일."""
    d = dt.date(year, 12, 31) if month == 12 else dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> dt.date:
    """부활절(그레고리력). Anonymous Gregorian 알고리즘.

    성금요일 = 부활절 - 2일. 이것만 계산이 필요한 이동 축일이다.
    """
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    lam = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * lam) // 433
    month = (h + lam - 7 * m + 90) // 25
    day = (h + lam - 7 * m + 33 * month + 19) % 32
    return dt.date(year, month, day)


def _observed(d: dt.date) -> dt.date | None:
    """토/일에 걸린 공휴일의 실제 휴장일.

    토요일 -> 직전 금요일, 일요일 -> 다음 월요일.
    단 1월 1일이 토요일이면 직전 금요일은 전년도라 휴장하지 않는다(NYSE 규칙).
    """
    if d.weekday() == 5:  # 토
        if d.month == 1 and d.day == 1:
            return None
        return d - dt.timedelta(days=1)
    if d.weekday() == 6:  # 일
        return d + dt.timedelta(days=1)
    return d


@lru_cache(maxsize=64)
def market_holidays(year: int) -> frozenset[dt.date]:
    """그 해의 정규 휴장일 집합."""
    out: set[dt.date] = set()

    def add(d: dt.date | None) -> None:
        if d is not None and d.year == year:
            out.add(d)

    add(_observed(dt.date(year, 1, 1)))  # 신정
    if year >= _MLK_FROM:
        add(_nth_weekday(year, 1, 0, 3))  # MLK (1월 3번째 월)
    add(_nth_weekday(year, 2, 0, 3))  # 대통령의 날 (2월 3번째 월)
    add(_easter(year) - dt.timedelta(days=2))  # 성금요일
    add(_last_weekday(year, 5, 0))  # 현충일 (5월 마지막 월)
    if year >= _JUNETEENTH_FROM:
        add(_observed(dt.date(year, 6, 19)))  # 6월 19일
    add(_observed(dt.date(year, 7, 4)))  # 독립기념일
    add(_nth_weekday(year, 9, 0, 1))  # 노동절 (9월 1번째 월)
    add(_nth_weekday(year, 11, 3, 4))  # 추수감사절 (11월 4번째 목)
    add(_observed(dt.date(year, 12, 25)))  # 성탄절

    out |= {d for d in AD_HOC_CLOSURES if d.year == year}
    return frozenset(out)


@lru_cache(maxsize=64)
def early_close_days(year: int) -> frozenset[dt.date]:
    """조기 폐장일(13:00 ET 마감) 집합.

    휴장은 아니지만 세션이 3시간 짧다. 장 마감 전 청산 로직이
    이 날을 모르면 마감 뒤에 주문을 내려 한다.
    """
    hol = market_holidays(year)
    out: set[dt.date] = set()

    # 독립기념일 전날 (7/4 가 화~금일 때)
    july4 = dt.date(year, 7, 4)
    if july4.weekday() < 5:
        prev = july4 - dt.timedelta(days=1)
        if prev.weekday() < 5 and prev not in hol:
            out.add(prev)

    # 추수감사절 다음 금요일
    out.add(_nth_weekday(year, 11, 3, 4) + dt.timedelta(days=1))

    # 크리스마스 이브 (평일일 때)
    eve = dt.date(year, 12, 24)
    if eve.weekday() < 5 and eve not in hol:
        out.add(eve)

    return frozenset(d for d in out if d not in hol)


def is_trading_day(d: dt.date) -> bool:
    """정규장이 열리는 날인가. 주말과 휴장일을 모두 제외한다."""
    if isinstance(d, dt.datetime):
        d = d.date()
    return d.weekday() < 5 and d not in market_holidays(d.year)


def is_early_close(d: dt.date) -> bool:
    """13:00 ET 조기 폐장일인가."""
    if isinstance(d, dt.datetime):
        d = d.date()
    return d in early_close_days(d.year)


def next_trading_day(d: dt.date, *, inclusive: bool = False) -> dt.date:
    """``d`` 이후(또는 당일 포함) 첫 거래일."""
    if isinstance(d, dt.datetime):
        d = d.date()
    cursor = d if inclusive else d + dt.timedelta(days=1)
    for _ in range(30):  # 연속 휴장이 30일을 넘는 일은 없다
        if is_trading_day(cursor):
            return cursor
        cursor += dt.timedelta(days=1)
    raise RuntimeError(f"{d} 이후 30일 안에 거래일이 없다. 달력이 깨졌다")


def previous_trading_day(d: dt.date, *, inclusive: bool = False) -> dt.date:
    """``d`` 이전(또는 당일 포함) 마지막 거래일."""
    if isinstance(d, dt.datetime):
        d = d.date()
    cursor = d if inclusive else d - dt.timedelta(days=1)
    for _ in range(30):
        if is_trading_day(cursor):
            return cursor
        cursor -= dt.timedelta(days=1)
    raise RuntimeError(f"{d} 이전 30일 안에 거래일이 없다. 달력이 깨졌다")


def trading_days_between(start: dt.date, end: dt.date) -> int:
    """``start`` 다음 날부터 ``end`` 까지의 거래일 수.

    start 는 세지 않고 end 는 센다. 진입한 날을 0일차로 보고
    "며칠 들고 있었나"를 세는 용도다.
    """
    if isinstance(start, dt.datetime):
        start = start.date()
    if isinstance(end, dt.datetime):
        end = end.date()
    if end <= start:
        return 0
    count = 0
    cursor = start
    while cursor < end:
        cursor += dt.timedelta(days=1)
        if is_trading_day(cursor):
            count += 1
    return count
