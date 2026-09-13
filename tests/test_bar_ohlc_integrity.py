"""OHLC 정합성 검증.

2026-09-08 장중에 시세 제공자가 시가만 나흘 전 값으로 준 봉을 내려보냈다.
O 20.93 / H 24.54 / L 24.07 / C 24.15 — 시가가 저가보다 15% 낮은데
``high >= low`` 는 만족해서 그대로 통과했고, 시가갭이 -10.82% 로 계산돼
진입이 엉뚱한 이유로 차단됐다. 이 테스트가 그 회귀를 막는다.
"""

from __future__ import annotations

import datetime as dt

import pytest

from koru_trade.data.loader import bars_from_frame
from koru_trade.models import Bar

TS = dt.datetime(2026, 9, 8)


def _bar(**kw: float) -> Bar:
    base = {
        "open": 24.39,
        "high": 24.54,
        "low": 23.83,
        "close": 24.04,
        "volume": 1.1e7,
        "fx_rate": 1341.0,
    }
    base.update(kw)
    return Bar(ts=TS, **base)  # type: ignore[arg-type]


def test_정상_봉은_통과한다() -> None:
    b = _bar()
    assert b.low <= b.open <= b.high
    assert b.low <= b.close <= b.high


def test_실제로_들어왔던_깨진_봉을_거부한다() -> None:
    """2026-09-08 장중 실제 사례. 시가가 저가보다 낮다."""
    with pytest.raises(ValueError, match="범위 밖"):
        _bar(open=20.93, high=24.54, low=24.07, close=24.15)


@pytest.mark.parametrize(
    ("field", "value", "why"),
    [
        ("open", 23.00, "시가가 저가 아래"),
        ("open", 25.00, "시가가 고가 위"),
        ("close", 23.00, "종가가 저가 아래"),
        ("close", 25.00, "종가가 고가 위"),
    ],
)
def test_고저_범위를_벗어나면_거부한다(field: str, value: float, why: str) -> None:
    with pytest.raises(ValueError, match="범위 밖"):
        _bar(**{field: value})


def test_경계값은_허용한다() -> None:
    """시가=저가, 종가=고가 같은 경계는 실제로 흔하다."""
    _bar(open=23.83)
    _bar(open=24.54)
    _bar(close=23.83)
    _bar(close=24.54)


def test_시가_종가_고가_저가가_모두_같아도_된다() -> None:
    """거래가 한 번만 체결된 봉. 유효하다."""
    _bar(open=24.0, high=24.0, low=24.0, close=24.0)


def test_기존_high_low_검사는_그대로다() -> None:
    with pytest.raises(ValueError, match=r"high.*low"):
        _bar(high=23.0, low=24.0)


class TestLoaderSkipsBadBars:
    """로더가 깨진 봉을 조용히 버리고 나머지는 살리는지."""

    @staticmethod
    def _frames() -> tuple[object, object]:
        pd = pytest.importorskip("pandas")
        idx = pd.to_datetime(["2026-09-02", "2026-09-03", "2026-09-04", "2026-09-08"])
        px = pd.DataFrame(
            {
                # 마지막 봉만 깨져 있다(시가가 저가보다 낮다)
                "Open": [19.48, 19.92, 20.93, 20.93],
                "High": [20.24, 20.80, 23.60, 24.54],
                "Low": [19.30, 19.19, 20.83, 24.07],
                "Close": [20.15, 20.69, 23.47, 24.15],
                "Volume": [1.5e7, 2.3e7, 2.5e7, 7.5e6],
            },
            index=idx,
        )
        fx = pd.Series([1350.0, 1348.0, 1345.0, 1341.0], index=idx)
        return px, fx

    def test_깨진_봉만_버리고_나머지는_살린다(self) -> None:
        px, fx = self._frames()
        bars = bars_from_frame(px, fx)
        assert len(bars) == 3, "깨진 봉 1개만 빠져야 한다"
        assert bars[-1].ts.date() == dt.date(2026, 9, 4)

    def test_맨_뒤_봉이_버려지면_경고한다(self, caplog: pytest.LogCaptureFixture) -> None:
        """조용히 넘어가면 봇이 옛 데이터로 판단하면서 최신인 줄 안다."""
        px, fx = self._frames()
        with caplog.at_level("WARNING"):
            bars_from_frame(px, fx)
        text = caplog.text
        assert "건너뛰었다" in text
        assert "가장 최근 봉" in text

    def test_전부_깨지면_예외를_던진다(self) -> None:
        from koru_trade.data.loader import DataUnavailableError

        pd = pytest.importorskip("pandas")
        idx = pd.to_datetime(["2026-09-03", "2026-09-04"])
        px = pd.DataFrame(
            {
                "Open": [10.0, 11.0],
                "High": [12.0, 13.0],
                "Low": [11.5, 12.5],  # 저가 > 시가 : 둘 다 깨짐
                "Close": [11.8, 12.8],
                "Volume": [1e6, 1e6],
            },
            index=idx,
        )
        fx = pd.Series([1340.0, 1341.0], index=idx)
        with pytest.raises(DataUnavailableError):
            bars_from_frame(px, fx)
