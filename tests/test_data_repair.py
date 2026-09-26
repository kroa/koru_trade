"""최근 일봉 결측 복원 검증.

yfinance 가 가장 최근 일봉을 비워서 주는 일이 일주일에 두 번(2026-09-22, 09-25) 있었다.
로더가 빈 봉을 버리면 봇과 사이트는 하루 전 봉을 최신으로 믿고 판단한다. 9/25 는
실제로 초록불이었는데 전날 기준으로 빨간불이 났다. 아무 오류도 나지 않는다.

여기서 못 박는 계약:

1. **종가는 공식 종가가 먼저다.** 분봉 마지막 체결은 1~2센트 다를 수 있고,
   9/23 은 기준선과 2센트 차이였다. 그 크기가 판정을 뒤집는다.
2. **날짜를 확인할 수 없는 가격은 종가로 쓰지 않는다.** 장중 현재가를 종가로 쓰면
   멈춘 화면이 최신인 척하는 것과 같은 사고가 난다.
3. **값이 있는 칸은 건드리지 않는다.** 야후가 준 값이 있으면 그게 공식값이다.
4. **복원 실패가 적재를 막지 않는다.** 재료를 못 받으면 예전처럼 버리고 경고한다.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import ClassVar

import pytest

from koru_trade.data.repair import (
    OfficialQuote,
    incomplete_rows,
    official_close_for,
    quote_from_info,
    repair_recent,
    session_end,
    session_from_intraday,
)

pd = pytest.importorskip("pandas")
NAN = float("nan")
ET = "America/New_York"


def _daily(rows: list[tuple[str, float, float, float, float, float]]):
    """yfinance 일봉처럼 ET 자정 tz-aware 인덱스를 가진 프레임."""
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz=ET) for d, *_ in rows])
    return pd.DataFrame(
        {
            "Open": [r[1] for r in rows],
            "High": [r[2] for r in rows],
            "Low": [r[3] for r in rows],
            "Close": [r[4] for r in rows],
            "Volume": [r[5] for r in rows],
        },
        index=idx,
    )


def _intraday(day: str, bars: list[tuple[str, float, float, float, float, float]]):
    """(HH:MM, O, H, L, C, V) 목록으로 5분봉 프레임."""
    idx = pd.DatetimeIndex([pd.Timestamp(f"{day} {t}", tz=ET) for t, *_ in bars])
    return pd.DataFrame(
        {
            "Open": [b[1] for b in bars],
            "High": [b[2] for b in bars],
            "Low": [b[3] for b in bars],
            "Close": [b[4] for b in bars],
            "Volume": [b[5] for b in bars],
        },
        index=idx,
    )


# 2026-09-25 실제 사고를 줄여 옮긴 재료
SEP25_SESSION = [
    ("04:00", 21.00, 21.21, 20.63, 21.18, 0),  # 프리마켓 — 빼야 한다
    ("09:30", 21.27, 21.40, 20.62, 21.10, 2_000_000),
    ("12:00", 21.10, 21.77, 21.05, 21.60, 1_000_000),
    ("15:55", 21.66, 21.71, 21.555, 21.58, 292_597),
    ("16:30", 21.60, 21.90, 21.55, 21.88, 0),  # 애프터 — 빼야 한다
]
SEP25_QUOTE = OfficialQuote(price=21.59, at=dt.datetime(2026, 9, 25, 16, 0), previous_close=20.14)


def _sep25_daily():
    """9/25 줄이 통째로 빈 상태. auto_adjust=True 로 받으면 실제로 이렇게 온다."""
    return _daily(
        [
            ("2026-09-23", 22.42, 22.53, 20.98, 21.27, 18_596_600),
            ("2026-09-24", 21.10, 21.13, 19.89, 20.14, 17_426_500),
            ("2026-09-25", NAN, NAN, NAN, NAN, 15_683_798),
        ]
    )


def _sep25_complete():
    """야후가 제대로 준 날. 아무것도 고치면 안 된다."""
    return _daily(
        [
            ("2026-09-23", 22.42, 22.53, 20.98, 21.27, 18_596_600),
            ("2026-09-24", 21.10, 21.13, 19.89, 20.14, 17_426_500),
            ("2026-09-25", 21.27, 21.77, 20.62, 21.59, 15_683_798),
        ]
    )


class TestIncompleteRows:
    def test_맨_뒤_빈_줄을_찾는다(self) -> None:
        rows = incomplete_rows(_sep25_daily())
        assert [r.date() for r in rows] == [dt.date(2026, 9, 25)]

    def test_오래된_결측은_보지_않는다(self) -> None:
        """분봉은 최근 며칠치뿐이라 오래된 결측은 복원할 재료가 없다."""
        f = _daily(
            [(f"2026-09-0{i}", 20.0, 21.0, 19.0, NAN if i == 1 else 20.5, 1.0) for i in range(1, 8)]
        )
        assert incomplete_rows(f, lookback=3) == []

    def test_다_있으면_빈_목록(self) -> None:
        assert incomplete_rows(_sep25_complete()) == []

    def test_빈_프레임도_터지지_않는다(self) -> None:
        assert incomplete_rows(pd.DataFrame()) == []


class TestSessionFromIntraday:
    def test_정규장만_모은다(self) -> None:
        s = session_from_intraday(_intraday("2026-09-25", SEP25_SESSION), dt.date(2026, 9, 25))
        assert s is not None
        assert s["Open"] == pytest.approx(21.27)  # 04:00 프리마켓 21.00 이 아니다
        assert s["High"] == pytest.approx(21.77)  # 16:30 애프터 21.90 이 아니다
        assert s["Low"] == pytest.approx(20.62)  # 04:00 프리마켓 20.63 이 아니다
        assert s["Close"] == pytest.approx(21.58)  # 16:30 애프터 21.88 이 아니다
        assert s["Volume"] == pytest.approx(2_000_000 + 1_000_000 + 292_597)

    def test_다른_날은_섞지_않는다(self) -> None:
        other = _intraday("2026-09-24", [("10:00", 20.0, 20.1, 19.9, 20.05, 1.0)])
        assert session_from_intraday(other, dt.date(2026, 9, 25)) is None

    def test_조기폐장일은_13시에_끊는다(self) -> None:
        day = "2026-11-27"
        f = _intraday(
            day,
            [
                ("10:00", 20.0, 20.2, 19.9, 20.1, 1.0),
                ("12:55", 20.1, 20.3, 20.0, 20.25, 1.0),
                ("14:00", 20.25, 25.0, 20.2, 24.0, 1.0),
            ],
        )
        s = session_from_intraday(f, dt.date(2026, 11, 27))
        assert s is not None
        assert s["Close"] == pytest.approx(20.25)
        assert s["High"] == pytest.approx(20.3)
        assert session_end(dt.date(2026, 11, 27)) == dt.time(13, 0)

    def test_분봉이_비면_None(self) -> None:
        assert session_from_intraday(pd.DataFrame(), dt.date(2026, 9, 25)) is None


class TestQuoteFromInfo:
    def test_epoch_를_ET_로_바꾼다(self) -> None:
        # 1790366400 = 2026-09-25 20:00 UTC = 16:00 ET (실측값)
        q = quote_from_info(
            {
                "regularMarketPrice": 21.59,
                "regularMarketTime": 1790366400,
                "regularMarketPreviousClose": 20.14,
            }
        )
        assert q is not None
        assert q.at == dt.datetime(2026, 9, 25, 16, 0)
        assert q.price == pytest.approx(21.59)
        assert q.previous_close == pytest.approx(20.14)

    @pytest.mark.parametrize(
        "info",
        [
            None,
            {},
            {"regularMarketPrice": 21.59},
            {"regularMarketTime": 1790366400},
            {"regularMarketPrice": float("nan"), "regularMarketTime": 1790366400},
            {"regularMarketPrice": 0.0, "regularMarketTime": 1790366400},
            {"regularMarketPrice": 21.59, "regularMarketTime": "깨진값"},
        ],
    )
    def test_쓸_수_없으면_None(self, info) -> None:  # type: ignore[no-untyped-def]
        assert quote_from_info(info) is None


class TestOfficialCloseFor:
    def test_장이_끝났으면_그날_종가다(self) -> None:
        assert official_close_for(dt.date(2026, 9, 25), SEP25_QUOTE) == pytest.approx(21.59)

    def test_장중_가격은_종가가_아니다(self) -> None:
        """장중이면 그냥 현재가다. 이걸 종가로 쓰면 형성 중인 봉을 확정으로 믿게 된다."""
        live = OfficialQuote(price=21.30, at=dt.datetime(2026, 9, 25, 11, 21))
        assert official_close_for(dt.date(2026, 9, 25), live) is None

    def test_직전_거래일은_previous_close_다(self) -> None:
        assert official_close_for(dt.date(2026, 9, 24), SEP25_QUOTE) == pytest.approx(20.14)

    def test_주말을_건너_직전_거래일을_찾는다(self) -> None:
        monday = OfficialQuote(price=22.0, at=dt.datetime(2026, 9, 28, 16, 0), previous_close=21.59)
        assert official_close_for(dt.date(2026, 9, 25), monday) == pytest.approx(21.59)

    def test_날짜가_안_맞으면_쓰지_않는다(self) -> None:
        assert official_close_for(dt.date(2026, 9, 23), SEP25_QUOTE) is None

    def test_시세가_없으면_None(self) -> None:
        assert official_close_for(dt.date(2026, 9, 25), None) is None


class TestRepairRecent:
    def test_9월25일_사고를_재현하고_공식_종가로_메운다(self) -> None:
        fixed, notes = repair_recent(
            _sep25_daily(), _intraday("2026-09-25", SEP25_SESSION), SEP25_QUOTE
        )
        row = fixed.iloc[-1]
        assert row["Close"] == pytest.approx(21.59), (
            "분봉 마지막 21.58 이 아니라 공식 21.59 여야 한다"
        )
        assert row["Open"] == pytest.approx(21.27)
        assert row["High"] == pytest.approx(21.77)
        assert row["Low"] == pytest.approx(20.62)
        assert row["Volume"] == pytest.approx(15_683_798), "야후가 준 거래량은 그대로 둔다"
        assert len(notes) == 1 and "공식 종가" in notes[0]

    def test_공식_종가가_없으면_분봉으로_대신하고_그렇게_적는다(self) -> None:
        fixed, notes = repair_recent(_sep25_daily(), _intraday("2026-09-25", SEP25_SESSION), None)
        assert fixed.iloc[-1]["Close"] == pytest.approx(21.58)
        assert "공식 종가 미확인" in notes[0]

    def test_있는_값은_건드리지_않는다(self) -> None:
        """9/22 처럼 시가·고가·저가는 있고 종가만 빈 경우."""
        f = _daily([("2026-09-25", 21.30, 21.80, 20.60, NAN, 1.0)])
        fixed, _ = repair_recent(f, _intraday("2026-09-25", SEP25_SESSION), SEP25_QUOTE)
        row = fixed.iloc[-1]
        assert (row["Open"], row["High"], row["Low"]) == (21.30, 21.80, 20.60)
        assert row["Close"] == pytest.approx(21.59)

    def test_고가_저가가_종가를_감싼다(self) -> None:
        """분봉 고가보다 공식 종가가 높으면 봉 검증에서 버려진다. 그러면 복원한 의미가 없다."""
        quote = OfficialQuote(price=21.95, at=dt.datetime(2026, 9, 25, 16, 0))
        fixed, _ = repair_recent(_sep25_daily(), _intraday("2026-09-25", SEP25_SESSION), quote)
        row = fixed.iloc[-1]
        assert row["High"] >= row["Close"] >= row["Low"]
        assert row["High"] >= row["Open"] >= row["Low"]

    def test_분봉이_없으면_그대로_두고_알린다(self) -> None:
        fixed, notes = repair_recent(_sep25_daily(), pd.DataFrame(), SEP25_QUOTE)
        assert math.isnan(fixed.iloc[-1]["Close"])
        assert "복원하지 못했다" in notes[0]

    def test_원본을_바꾸지_않는다(self) -> None:
        f = _sep25_daily()
        repair_recent(f, _intraday("2026-09-25", SEP25_SESSION), SEP25_QUOTE)
        assert math.isnan(f.iloc[-1]["Close"])

    def test_고칠_게_없으면_같은_프레임을_돌려준다(self) -> None:
        f = _sep25_complete()
        fixed, notes = repair_recent(f, pd.DataFrame(), None)
        assert fixed is f and notes == []


class TestLoaderIntegration:
    """로더가 복원을 실제로 부르고, 실패해도 멈추지 않는지."""

    class _FakeTicker:
        def __init__(self, intraday, info, *, fail_hist=False, fail_info=False) -> None:  # type: ignore[no-untyped-def]
            self._intraday, self._info = intraday, info
            self._fail_hist, self._fail_info = fail_hist, fail_info

        def history(self, **_kw):  # type: ignore[no-untyped-def]
            if self._fail_hist:
                raise ConnectionError("분봉 서버 끊김")
            return self._intraday

        @property
        def info(self):  # type: ignore[no-untyped-def]
            if self._fail_info:
                raise ConnectionError("info 서버 끊김")
            return self._info

    def _yf(self, **kw):  # type: ignore[no-untyped-def]
        ticker = self._FakeTicker(**kw)

        class YF:
            @staticmethod
            def Ticker(_symbol):  # type: ignore[no-untyped-def]
                return ticker

        return YF

    INFO: ClassVar[dict[str, float]] = {
        "regularMarketPrice": 21.59,
        "regularMarketTime": 1790366400,
        "regularMarketPreviousClose": 20.14,
    }

    def test_빈_봉을_채워_돌려준다(self) -> None:
        from koru_trade.data.loader import _repair_recent_daily

        yf = self._yf(intraday=_intraday("2026-09-25", SEP25_SESSION), info=self.INFO)
        fixed = _repair_recent_daily(_sep25_daily(), "KORU", yf)
        assert fixed.iloc[-1]["Close"] == pytest.approx(21.59)

    def test_분봉을_못_받으면_원래_프레임(self) -> None:
        from koru_trade.data.loader import _repair_recent_daily

        f = _sep25_daily()
        yf = self._yf(intraday=None, info=self.INFO, fail_hist=True)
        assert _repair_recent_daily(f, "KORU", yf) is f

    def test_공식_종가를_못_받아도_분봉으로_채운다(self) -> None:
        from koru_trade.data.loader import _repair_recent_daily

        yf = self._yf(intraday=_intraday("2026-09-25", SEP25_SESSION), info=None, fail_info=True)
        fixed = _repair_recent_daily(_sep25_daily(), "KORU", yf)
        assert fixed.iloc[-1]["Close"] == pytest.approx(21.58)

    def test_채운_봉이_Bar_로_살아남는다(self) -> None:
        """복원했는데 봉 검증에서 버려지면 복원한 의미가 없다."""
        from koru_trade.data.loader import _repair_recent_daily, bars_from_frame

        yf = self._yf(intraday=_intraday("2026-09-25", SEP25_SESSION), info=self.INFO)
        fixed = _repair_recent_daily(_sep25_daily(), "KORU", yf)
        fx = pd.Series([1362.5, 1355.3, 1355.3], index=fixed.index)
        bars = bars_from_frame(fixed, fx)
        assert bars[-1].ts.date() == dt.date(2026, 9, 25)
        assert bars[-1].close == pytest.approx(21.59)

    def test_고칠_게_없으면_분봉을_받지도_않는다(self) -> None:
        from koru_trade.data.loader import _repair_recent_daily

        yf = self._yf(intraday=None, info=None, fail_hist=True, fail_info=True)
        f = _sep25_complete()
        assert _repair_recent_daily(f, "KORU", yf) is f
