"""최근 일봉의 결측을 분봉과 공식 종가로 메운다.

왜 필요한가
-----------
yfinance(야후 파이낸스)는 KORU 의 **가장 최근 일봉을 비워서** 주는 일이 잦다.
2026-09-22 봉과 2026-09-25 봉이 둘 다 그랬고, 비운 상태로 하루 넘게 남아 있었다.
조정주가(``auto_adjust=True``)로 받으면 종가뿐 아니라 시가·고가·저가까지 전부
NaN 이 된다. 조정 계산이 종가에서 출발하기 때문이다.

로더는 값이 없는 봉을 버린다. 그러면 봇과 사이트가 **하루 전 봉을 최신으로 믿고**
판단한다. 9/25 는 실제로는 초록불(종가 $21.59 > 기준선 $21.17)이었는데, 버려진
결과 전날($20.14) 기준으로 빨간불이 났다. 이런 오판은 아무 오류도 내지 않는다.

같은 날의 값은 다른 경로에는 멀쩡히 있다.

- 시가·고가·저가·거래량: 5분봉을 정규장(09:30~16:00 ET) 구간으로 모으면 된다.
- 종가: ``Ticker.info`` 의 ``regularMarketPrice`` 가 **공식 종가**다. 장이 끝난 뒤에는
  ``regularMarketTime`` 이 그날 16:00 ET 로 찍혀 있으므로 어느 날의 값인지 확인된다.

종가를 분봉 마지막 체결로 대신하지 않는 이유: 공식 종가는 16:00 종가 단일가 매매로
정해지는데, 5분봉 마지막 체결은 그 직전 값이다. 9/25 는 $21.58 대 $21.59, 9/22 는
$23.785 대 $23.77 이었다. 1~2센트지만 9/23 은 기준선과의 차이가 2센트였다 —
이 크기가 판정을 뒤집는다. 그래서 공식 종가를 먼저 쓰고, 그것이 확인되지 않을 때만
분봉 마지막 체결로 대신하며, 그 사실을 로그에 남긴다.

야후의 ``history(repair=True)`` 는 쓰지 않는다. 9/25 종가를 $21.52 로 추정했다 —
공식 $21.59 와 7센트 차이다. 추정값을 실측처럼 쓰는 것이 이 모듈이 막으려는 사고다.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from koru_trade.market_calendar import is_early_close, previous_trading_day

logger = logging.getLogger(__name__)

__all__ = [
    "LOOKBACK_ROWS",
    "OfficialQuote",
    "incomplete_rows",
    "official_close_for",
    "quote_from_info",
    "repair_recent",
    "session_end",
    "session_from_intraday",
]

ET = ZoneInfo("America/New_York")
REGULAR_OPEN = dt.time(9, 30)
REGULAR_CLOSE = dt.time(16, 0)
EARLY_CLOSE = dt.time(13, 0)
PRICE_COLS = ("Open", "High", "Low", "Close")

LOOKBACK_ROWS = 3
"""맨 뒤에서 몇 줄까지 복원을 시도하나.

분봉은 최근 며칠치만 받으므로 그보다 오래된 결측은 복원할 재료가 없다.
그리고 오래된 결측은 오늘 판단에 쓰이지 않는다. 문제는 늘 맨 끝이다.
"""


@dataclass(frozen=True, slots=True)
class OfficialQuote:
    """``Ticker.info`` 에서 뽑은 정규장 시세.

    Attributes:
        price: 정규장 마지막 가격. 장이 끝난 뒤에는 공식 종가다.
        at: 그 가격의 시각. 미국 동부시 벽시계(tz 없음).
        previous_close: ``at`` 날짜 직전 거래일의 공식 종가.
    """

    price: float
    at: dt.datetime
    previous_close: float | None = None


def session_end(day: dt.date) -> dt.time:
    """그날 정규장이 끝나는 시각(ET). 조기 폐장일은 13:00 이다."""
    return EARLY_CLOSE if is_early_close(day) else REGULAR_CLOSE


def _is_nan(value: Any) -> bool:
    try:
        return value is None or math.isnan(float(value))
    except (TypeError, ValueError):
        return True


def _et_datetime(value: Any) -> dt.datetime:
    """pandas Timestamp / datetime 을 ET 벽시계 naive datetime 으로."""
    to_py = getattr(value, "to_pydatetime", None)
    raw: Any = to_py() if callable(to_py) else value
    ts = raw if isinstance(raw, dt.datetime) else dt.datetime.combine(raw, dt.time())
    if ts.tzinfo is not None:
        ts = ts.astimezone(ET).replace(tzinfo=None)
    return ts


def incomplete_rows(frame: Any, *, lookback: int = LOOKBACK_ROWS) -> list[Any]:
    """맨 뒤 ``lookback`` 줄 중 시가·고가·저가·종가 하나라도 빈 줄의 인덱스."""
    if frame is None or len(frame) == 0:
        return []
    tail = frame.iloc[-lookback:]
    out = []
    for label, row in tail.iterrows():
        if any(_is_nan(row.get(col)) for col in PRICE_COLS):
            out.append(label)
    return out


def session_from_intraday(intraday: Any, day: dt.date) -> dict[str, float] | None:
    """분봉에서 그날 정규장의 시가·고가·저가·종가·거래량을 모은다.

    프리마켓과 애프터마켓은 뺀다. 거래량이 거의 없는 호가라 일봉의 고가·저가를
    부풀린다. 값이 비어 있는 분봉 줄도 건너뛴다.

    Returns:
        ``Open/High/Low/Close/Volume`` 사전. 그날 정규장 분봉이 없으면 None.
    """
    if intraday is None or len(intraday) == 0:
        return None
    end = session_end(day)
    rows = []
    for label, row in intraday.iterrows():
        ts = _et_datetime(label)
        if ts.date() != day or not (REGULAR_OPEN <= ts.time() < end):
            continue
        if any(_is_nan(row.get(col)) for col in PRICE_COLS):
            continue
        rows.append((ts, row))
    if not rows:
        return None
    rows.sort(key=lambda r: r[0])
    return {
        "Open": float(rows[0][1]["Open"]),
        "High": max(float(r["High"]) for _, r in rows),
        "Low": min(float(r["Low"]) for _, r in rows),
        "Close": float(rows[-1][1]["Close"]),
        "Volume": sum(0.0 if _is_nan(r.get("Volume")) else float(r["Volume"]) for _, r in rows),
    }


def _positive(value: Any) -> float | None:
    """양의 유한한 수면 float, 아니면 None. info 는 값이 빠지거나 문자열로 오기도 한다."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) and num > 0 else None


def quote_from_info(info: Mapping[str, Any] | None) -> OfficialQuote | None:
    """``Ticker.info`` 사전에서 정규장 시세를 뽑는다. 쓸 수 없으면 None."""
    if not info:
        return None
    price = _positive(info.get("regularMarketPrice"))
    stamp = info.get("regularMarketTime")
    if price is None or stamp is None:
        return None
    try:
        if isinstance(stamp, dt.datetime):
            at = _et_datetime(stamp)
        else:
            at = dt.datetime.fromtimestamp(float(stamp), tz=ET).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return OfficialQuote(
        price=price,
        at=at,
        previous_close=_positive(info.get("regularMarketPreviousClose")),
    )


def official_close_for(day: dt.date, quote: OfficialQuote | None) -> float | None:
    """``day`` 의 공식 종가를 ``quote`` 에서 확인할 수 있으면 돌려준다.

    두 경우만 인정한다.

    1. ``quote.at`` 이 그날이고 정규장 마감 시각 이후다 — 장이 끝났으므로
       ``quote.price`` 가 공식 종가다. 장중이면 그냥 현재가라 쓰지 않는다.
    2. ``day`` 가 ``quote.at`` 날짜의 **직전 거래일**이다 — ``previous_close`` 가
       그날의 공식 종가다.

    그 밖에는 None. 날짜를 확인할 수 없는 가격을 종가로 쓰면, 멈춘 화면이
    최신인 척하는 것과 같은 종류의 사고가 난다.
    """
    if quote is None:
        return None
    if quote.at.date() == day and quote.at.time() >= session_end(day):
        return quote.price
    if quote.previous_close is not None and previous_trading_day(quote.at.date()) == day:
        return quote.previous_close
    return None


def repair_recent(
    frame: Any,
    intraday: Any,
    quote: OfficialQuote | None,
    *,
    lookback: int = LOOKBACK_ROWS,
) -> tuple[Any, list[str]]:
    """일봉 프레임의 최근 결측 줄을 분봉과 공식 종가로 메운다.

    값이 있는 칸은 건드리지 않는다. 야후가 준 값이 있으면 그것이 공식값이다.
    빈 칸만 채우고, 채운 뒤 고가·저가가 시가·종가를 감싸도록 맞춘다.

    Args:
        frame: yfinance 일봉 DataFrame (``Open/High/Low/Close/Volume``).
        intraday: 같은 종목의 분봉 DataFrame. 정규장 구간만 쓴다.
        quote: :func:`quote_from_info` 결과. 종가 확인용.
        lookback: 맨 뒤 몇 줄까지 볼 것인가.

    Returns:
        (고친 프레임 사본, 무엇을 어디서 채웠는지 적은 문장 목록).
        복원하지 못한 줄은 그대로 NaN 으로 남는다 — 로더가 버리고 경고한다.
    """
    rows = incomplete_rows(frame, lookback=lookback)
    if not rows:
        return frame, []
    out = frame.copy()
    notes: list[str] = []
    for label in rows:
        day = _et_datetime(label).date()
        sess = session_from_intraday(intraday, day)
        if sess is None:
            notes.append(f"{day} 일봉이 비었고 그날 정규장 분봉도 없어 복원하지 못했다")
            continue

        filled: list[str] = []
        values: dict[str, float] = {}
        for col in ("Open", "High", "Low"):
            current = out.at[label, col]
            if _is_nan(current):
                values[col] = sess[col]
                filled.append(col)
            else:
                values[col] = float(current)

        official = official_close_for(day, quote)
        current_close = out.at[label, "Close"]
        if not _is_nan(current_close):
            values["Close"] = float(current_close)
            source = "원래 값"
        elif official is not None:
            values["Close"] = official
            source = "공식 종가"
            filled.append("Close")
        else:
            values["Close"] = sess["Close"]
            source = "분봉 마지막 체결(공식 종가 미확인)"
            filled.append("Close")

        values["High"] = max(values["High"], values["Open"], values["Close"])
        values["Low"] = min(values["Low"], values["Open"], values["Close"])
        for col, val in values.items():
            out.at[label, col] = val
        if "Volume" in out.columns and _is_nan(out.at[label, "Volume"]):
            out.at[label, "Volume"] = sess["Volume"]

        notes.append(
            f"{day} 일봉이 비어 있어 복원했다: 채운 칸 {','.join(filled) or '-'} · "
            f"종가 ${values['Close']:.2f} ({source})"
        )
    return out, notes
