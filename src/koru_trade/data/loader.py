"""시세와 환율 적재.

조정주가를 반드시 쓴다
----------------------
KORU 는 **2025-02-10 에 1:10 액면병합(역분할)**, **2026-07-15 에 20:1 액면분할**을 했다.
미조정 주가로 백테스트하면 분할일에 -90% 또는 +1900% 짜리 가짜 수익률이 생기고,
그 하루가 전체 결과를 지배해 버린다. 이 모듈은 ``auto_adjust=True`` 로만 받는다.

환율 정렬
---------
미국 증시 거래일과 USD/KRW 시세일이 정확히 일치하지 않는다.
한국 공휴일에는 환율 데이터가 비고, 미국 공휴일에는 주가가 빈다.
주가 날짜를 기준으로 **직전 값을 이월(forward fill)** 한다.
미래 환율을 끌어오는 backfill 은 look-ahead bias 이므로 절대 하지 않는다.
"""

from __future__ import annotations

import csv
import datetime as dt
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from koru_trade.models import Bar

logger = logging.getLogger(__name__)

__all__ = [
    "DataUnavailableError",
    "bars_from_frame",
    "is_intraday",
    "load_bars",
    "load_bars_from_csv",
    "save_bars_to_csv",
]

DAILY_INTERVALS = frozenset({"1d", "5d", "1wk", "1mo", "3mo"})
"""일봉 이상 간격. 이 외에는 전부 분봉/시간봉으로 취급한다."""


def is_intraday(interval: str) -> bool:
    """분봉/시간봉 간격인지.

    >>> is_intraday("5m"), is_intraday("1h"), is_intraday("1d")
    (True, True, False)
    """
    return interval.strip().lower() not in DAILY_INTERVALS


DEFAULT_FX_TICKER = "KRW=X"
"""yfinance 의 USD/KRW 티커. 값은 '1달러당 원' 이다."""

CSV_HEADER = ["ts", "open", "high", "low", "close", "volume", "fx_rate"]


class DataUnavailableError(RuntimeError):
    """시세를 받을 수 없을 때. 네트워크 없이 돌리는 테스트와 구분하기 위한 전용 예외."""


def load_bars(
    symbol: str = "KORU",
    *,
    period: str = "3y",
    interval: str = "1d",
    fx_ticker: str = DEFAULT_FX_TICKER,
    cache_dir: str | Path | None = ".cache",
    max_cache_age_hours: float = 6.0,
) -> tuple[Bar, ...]:
    """yfinance 에서 조정주가와 환율을 받아 :class:`Bar` 목록으로 만든다.

    Args:
        symbol: 종목 티커.
        period: 조회 기간 (``1y``, ``3y``, ``max`` 등).
        interval: 봉 간격. ``1d`` 권장.
            분봉(``5m``, ``15m``)은 yfinance 가 최근 60일까지만 제공하며
            환율 분봉과의 정렬 품질이 떨어진다.
        fx_ticker: 환율 티커.
        cache_dir: 캐시 디렉터리. None 이면 캐시하지 않는다.
        max_cache_age_hours: 이 시간이 지난 캐시는 무시하고 다시 받는다.

    Returns:
        시간 오름차순 :class:`Bar` 튜플.

    Raises:
        DataUnavailableError: yfinance 미설치 또는 데이터 없음.
    """
    cache_path: Path | None = None
    if cache_dir is not None:
        cache_path = Path(cache_dir) / f"{symbol}_{interval}_{period}.csv"
        # 분봉은 훨씬 빨리 낡는다. 일봉과 같은 수명을 주면 장중에 옛 봉으로 판단한다.
        if is_intraday(interval):
            max_cache_age_hours = min(max_cache_age_hours, 0.25)
        cached = _read_fresh_cache(cache_path, max_cache_age_hours)
        if cached is not None:
            logger.info("캐시에서 %s 봉 %d개를 읽었다", symbol, len(cached))
            return cached

    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover - 선택적 의존성
        raise DataUnavailableError(
            "yfinance 가 설치되어 있지 않다. `pip install -e .[data]` 로 설치하라"
        ) from exc

    logger.info("yfinance 에서 %s(%s, %s) 조회 중", symbol, period, interval)
    px = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=True)
    if px is None or px.empty:
        raise DataUnavailableError(f"{symbol} 시세를 받을 수 없다(period={period})")

    fx = yf.Ticker(fx_ticker).history(period=period, interval=interval, auto_adjust=True)
    if fx is None or fx.empty:
        raise DataUnavailableError(f"환율({fx_ticker})을 받을 수 없다")

    bars = bars_from_frame(px, fx["Close"], intraday=is_intraday(interval))
    if cache_path is not None:
        save_bars_to_csv(bars, cache_path)
    return bars


def bars_from_frame(price_frame: Any, fx_series: Any, *, intraday: bool = False) -> tuple[Bar, ...]:
    """pandas DataFrame/Series 를 :class:`Bar` 튜플로 변환한다.

    Args:
        price_frame: ``Open/High/Low/Close/Volume`` 컬럼을 가진 DataFrame.
            **조정주가여야 한다.**
        fx_series: USD/KRW 종가 Series.
        intraday: 분봉/시간봉이면 True. **이 값을 틀리면 데이터가 조용히 망가진다.**
            일봉 경로는 인덱스를 날짜로 절삭(``normalize``)해 환율과 맞추는데,
            그 절삭을 분봉에 적용하면 하루치 봉이 전부 같은 자정 시각이 되고
            중복 제거에 걸려 **하루에 한 봉만 남는다**(338봉 -> 5봉).

    Returns:
        시간 오름차순 봉. 환율이 없는 앞부분은 버린다.
        분봉이면 타임스탬프는 **미국 동부시(ET) 벽시계 시각**이다 —
        장 마감 청산 같은 시간대 규칙을 쓸 수 있게 하기 위함이다.
    """
    px = price_frame.copy()
    fx = fx_series.copy()
    px.index = _normalize_index(px.index, intraday=intraday)
    fx.index = _normalize_index(fx.index, intraday=intraday)
    px = px[~px.index.duplicated(keep="last")].sort_index()
    fx = fx[~fx.index.duplicated(keep="last")].sort_index()

    # 주가 날짜 기준으로 직전 환율을 이월한다. backfill 은 미래 정보다.
    aligned = fx.reindex(px.index.union(fx.index)).ffill().reindex(px.index)

    bars: list[Bar] = []
    skipped = 0
    for ts, row in px.iterrows():
        rate = aligned.get(ts)
        if rate is None or _is_nan(rate) or float(rate) <= 0:
            skipped += 1
            continue
        try:
            bar = Bar(
                ts=_to_datetime(ts),
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=float(row.get("Volume", 0.0) or 0.0),
                fx_rate=float(rate),
            )
        except (ValueError, TypeError):
            skipped += 1
            continue
        bars.append(bar)

    if skipped:
        logger.warning("환율 또는 시세 결측으로 %d개 봉을 건너뛰었다", skipped)
    if not bars:
        raise DataUnavailableError("환율과 정렬된 유효한 봉이 하나도 없다")
    return tuple(bars)


def save_bars_to_csv(bars: Sequence[Bar], path: str | Path) -> Path:
    """봉을 CSV 로 저장한다. 캐시와 오프라인 테스트 픽스처에 쓴다."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_HEADER)
        for b in bars:
            writer.writerow(
                [
                    b.ts.isoformat(),
                    f"{b.open:.6f}",
                    f"{b.high:.6f}",
                    f"{b.low:.6f}",
                    f"{b.close:.6f}",
                    f"{b.volume:.0f}",
                    f"{b.fx_rate:.4f}",
                ]
            )
    return p


def load_bars_from_csv(path: str | Path) -> tuple[Bar, ...]:
    """:func:`save_bars_to_csv` 로 저장한 CSV 를 읽는다.

    네트워크 없이 결정론적으로 백테스트를 재현할 때 쓴다.
    """
    p = Path(path)
    if not p.exists():
        raise DataUnavailableError(f"CSV 가 없다: {p}")
    bars: list[Bar] = []
    with p.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            bars.append(
                Bar(
                    ts=dt.datetime.fromisoformat(row["ts"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    fx_rate=float(row["fx_rate"]),
                )
            )
    if not bars:
        raise DataUnavailableError(f"CSV 에 봉이 없다: {p}")
    bars.sort(key=lambda b: b.ts)
    return tuple(bars)


def _read_fresh_cache(path: Path, max_age_hours: float) -> tuple[Bar, ...] | None:
    """캐시가 충분히 최신이면 읽어서 반환한다. 아니면 None."""
    if not path.exists():
        return None
    age_h = (dt.datetime.now().timestamp() - path.stat().st_mtime) / 3600.0
    if age_h > max_age_hours:
        logger.debug("캐시가 %.1f시간 지나 무시한다: %s", age_h, path)
        return None
    try:
        return load_bars_from_csv(path)
    except (DataUnavailableError, ValueError, KeyError) as exc:
        logger.warning("캐시를 읽지 못해 무시한다(%s): %s", exc, path)
        return None


def _normalize_index(index: Any, *, intraday: bool = False) -> Any:
    """인덱스를 tz 없는 값으로 만든다.

    타임존이 있으면 **미국 동부시로 변환한 뒤** 떼어낸다. 그래야 남은 naive 시각이
    거래소 벽시계와 같아져서 "장 마감 10분 전" 같은 규칙을 쓸 수 있다.
    단순히 tz 를 떼면(=UTC 로 읽으면) 시간대 규칙이 전부 어긋난다.

    일봉일 때만 날짜로 절삭한다. 분봉에 절삭을 적용하면 하루치가 전부
    같은 자정 시각이 되어 중복 제거에 쓸려 나간다.
    """
    idx = index
    tz = getattr(idx, "tz", None)
    if tz is not None:
        convert = getattr(idx, "tz_convert", None)
        if callable(convert):
            idx = convert("America/New_York")
        idx = idx.tz_localize(None)
    if intraday:
        return idx
    normalize = getattr(idx, "normalize", None)
    return normalize() if callable(normalize) else idx


def _to_datetime(value: Any) -> dt.datetime:
    to_pydatetime = getattr(value, "to_pydatetime", None)
    if callable(to_pydatetime):
        result = to_pydatetime()
        if isinstance(result, dt.datetime):
            return result.replace(tzinfo=None)
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time())
    raise TypeError(f"날짜로 변환할 수 없다: {value!r}")


def _is_nan(value: Any) -> bool:
    try:
        return bool(value != value)  # NaN != NaN
    except (TypeError, ValueError):  # pragma: no cover - 방어적
        return True
