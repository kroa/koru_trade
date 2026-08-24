"""기술적 지표. 전부 순수 함수이며 pandas/numpy 에 의존하지 않는다.

의존성을 두지 않는 이유는 실거래 루프에서 마지막 N개의 봉만 들고 지표를 계산할 때
DataFrame 을 만드는 오버헤드와 인덱스 정렬 버그를 피하기 위해서다.
백테스트와 실거래가 **완전히 동일한 함수**를 호출하므로 두 경로의 지표가 어긋날 수 없다.

모든 함수는 "데이터가 부족하면 ``None``" 규약을 따른다.
예외를 던지지 않으므로 워밍업 구간에서 전략이 자연스럽게 HOLD 한다.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from koru_trade.models import Bar

__all__ = [
    "adx",
    "atr",
    "atr_pct",
    "bollinger",
    "consecutive_down_days",
    "ema",
    "gap_pct",
    "leverage_decay_estimate",
    "max_drawdown",
    "realized_vol",
    "returns",
    "rsi",
    "sma",
    "true_ranges",
]


def sma(values: Sequence[float], period: int) -> float | None:
    """단순이동평균. 데이터가 ``period`` 개 미만이면 None."""
    _check_period(period)
    if len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def ema(values: Sequence[float], period: int) -> float | None:
    """지수이동평균.

    초기값은 첫 ``period`` 개의 SMA 로 잡고 이후 재귀 평활한다.
    (일반적인 차트 프로그램과 동일한 관례.)
    """
    _check_period(period)
    if len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    acc = sum(values[:period]) / period
    for v in values[period:]:
        acc = v * k + acc * (1.0 - k)
    return acc


def rsi(closes: Sequence[float], period: int = 14) -> float | None:
    """Wilder 방식 RSI.

    Returns:
        0~100 사이 값. 데이터가 ``period + 1`` 개 미만이면 None.
        하락이 전혀 없으면 100.0 을 반환한다.
    """
    _check_period(period)
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def true_ranges(bars: Sequence[Bar]) -> list[float]:
    """각 봉의 True Range 목록. 첫 봉은 전일 종가가 없으므로 고가-저가를 쓴다."""
    out: list[float] = []
    for i, bar in enumerate(bars):
        if i == 0:
            out.append(bar.high - bar.low)
            continue
        prev_close = bars[i - 1].close
        out.append(
            max(
                bar.high - bar.low,
                abs(bar.high - prev_close),
                abs(bar.low - prev_close),
            )
        )
    return out


def atr(bars: Sequence[Bar], period: int = 14) -> float | None:
    """Wilder 방식 ATR(평균 진폭).

    3배 레버리지 상품에서는 이 값이 손절폭과 분할매수 간격의 유일한 합리적 기준이다.
    고정 퍼센트 손절은 변동성 레짐이 바뀌면 즉시 무의미해진다.
    """
    _check_period(period)
    if len(bars) < period + 1:
        return None
    trs = true_ranges(bars)
    acc = sum(trs[1 : period + 1]) / period
    for tr in trs[period + 1 :]:
        acc = (acc * (period - 1) + tr) / period
    return acc


def atr_pct(bars: Sequence[Bar], period: int = 14) -> float | None:
    """ATR 을 최근 종가로 나눈 값. 변동성 레짐 필터의 입력.

    예: 0.12 이면 하루 평균 진폭이 주가의 12% 라는 뜻이다.
    """
    value = atr(bars, period)
    if value is None or not bars or bars[-1].close <= 0:
        return None
    return value / bars[-1].close


def bollinger(
    closes: Sequence[float], period: int = 20, num_std: float = 2.0
) -> tuple[float, float, float] | None:
    """볼린저 밴드.

    Returns:
        (하단, 중심선, 상단). 데이터 부족 시 None.
    """
    _check_period(period)
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    var = sum((v - mid) ** 2 for v in window) / period
    sd = math.sqrt(var)
    return mid - num_std * sd, mid, mid + num_std * sd


def adx(bars: Sequence[Bar], period: int = 14) -> float | None:
    """Wilder ADX(추세 강도).

    3배 레버리지 상품에서 횡보장 진입은 변동성 감쇠로 확실히 손실이므로
    ADX 가 낮으면 진입을 막는 데 쓴다.
    """
    _check_period(period)
    if len(bars) < 2 * period + 1:
        return None

    plus_dm: list[float] = []
    minus_dm: list[float] = []
    trs: list[float] = []
    for i in range(1, len(bars)):
        cur, prev = bars[i], bars[i - 1]
        up = cur.high - prev.high
        down = prev.low - cur.low
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        trs.append(
            max(
                cur.high - cur.low,
                abs(cur.high - prev.close),
                abs(cur.low - prev.close),
            )
        )

    def _wilder(seq: list[float]) -> list[float]:
        smoothed = [sum(seq[:period])]
        for v in seq[period:]:
            smoothed.append(smoothed[-1] - smoothed[-1] / period + v)
        return smoothed

    tr_s = _wilder(trs)
    pdm_s = _wilder(plus_dm)
    mdm_s = _wilder(minus_dm)

    dxs: list[float] = []
    for tr_v, p_v, m_v in zip(tr_s, pdm_s, mdm_s, strict=True):
        if tr_v <= 0:
            dxs.append(0.0)
            continue
        pdi = 100.0 * p_v / tr_v
        mdi = 100.0 * m_v / tr_v
        denom = pdi + mdi
        dxs.append(100.0 * abs(pdi - mdi) / denom if denom > 0 else 0.0)

    if len(dxs) < period:
        return None
    acc = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        acc = (acc * (period - 1) + dx) / period
    return acc


def returns(values: Sequence[float]) -> list[float]:
    """단순 수익률 시계열. 길이는 입력보다 1 짧다."""
    out: list[float] = []
    for i in range(1, len(values)):
        prev = values[i - 1]
        out.append(values[i] / prev - 1.0 if prev else 0.0)
    return out


def realized_vol(closes: Sequence[float], period: int = 20, annualize: int = 252) -> float | None:
    """실현 변동성(연율화).

    KORU 처럼 기초지수의 3배를 추종하는 상품은 변동성이 곧 감쇠 비용이다.
    연율 변동성 sigma 의 3배 레버리지 상품이 겪는 이론적 감쇠는 대략
    ``-0.5 * (L^2 - L) * sigma_underlying^2`` 이며, L=3, sigma=0.5 이면 연 -37.5% 다.
    이 값이 크면 진입 자체를 막아야 한다.
    """
    _check_period(period)
    if len(closes) < period + 1:
        return None
    rets = returns(closes[-(period + 1) :])
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(annualize)


def leverage_decay_estimate(
    closes: Sequence[float], leverage: float = 3.0, period: int = 20
) -> float | None:
    """레버리지 ETF 의 연율 변동성 감쇠 추정치(음수).

    상품 자체의 실현변동성에서 기초지수 변동성을 역산(``sigma_u = sigma_etf / L``)한 뒤
    ``-0.5 * (L^2 - L) * sigma_u^2`` 를 적용한다.

    Returns:
        연율 감쇠율(예: -0.375 = 연 -37.5%). 데이터 부족 시 None.
    """
    vol_etf = realized_vol(closes, period)
    if vol_etf is None or leverage <= 0:
        return None
    sigma_u = vol_etf / leverage
    return -0.5 * (leverage**2 - leverage) * sigma_u**2


def max_drawdown(values: Sequence[float]) -> float:
    """최대 낙폭(음수 비율). 빈 입력이면 0."""
    peak = -math.inf
    worst = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, v / peak - 1.0)
    return worst


def gap_pct(bars: Sequence[Bar]) -> float | None:
    """최근 봉의 시가 갭 비율(전봉 종가 대비). 봉이 2개 미만이면 None.

    3배 레버리지 상품은 한국 장중에 벌어진 사건이 미국 개장 갭으로 한꺼번에 반영된다.
    갭이 크면 지정가 주문이 슬리피지를 크게 먹으므로 진입을 미룬다.
    """
    if len(bars) < 2:
        return None
    prev_close = bars[-2].close
    return bars[-1].open / prev_close - 1.0 if prev_close else None


def consecutive_down_days(closes: Sequence[float]) -> int:
    """마지막 봉 기준 연속 하락일 수."""
    count = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] < closes[i - 1]:
            count += 1
        else:
            break
    return count


def _check_period(period: int) -> None:
    if period < 1:
        raise ValueError(f"period 는 1 이상이어야 한다: {period}")
