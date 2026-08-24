"""도메인 모델 검증.

값 객체의 불변식이 여기서 깨지면 상위 계층이 전부 무너진다.
특히 :meth:`Position.reduced` 의 **원가 보존**은 분할 익절의 정확성을 좌우한다.
"""

from __future__ import annotations

import datetime as dt

import pytest
from tests.conftest import BASE_TS

from koru_trade.models import (
    Action,
    Bar,
    Decision,
    EntryPlan,
    ExitReason,
    Fill,
    Lot,
    Order,
    OrderSide,
    OrderType,
    Position,
    Quote,
    TradeRecord,
)


def _lot(qty: int, price: float, fx: float, idx: int, unit_cost: float | None = None) -> Lot:
    cost = (unit_cost if unit_cost is not None else price * fx) * qty
    return Lot(f"L{idx}", qty, price, fx, cost, BASE_TS + dt.timedelta(days=idx), idx)


class TestBar:
    def test_고가가_저가보다_낮으면_거부된다(self) -> None:
        with pytest.raises(ValueError, match="high"):
            Bar(BASE_TS, 20.0, 19.0, 21.0, 20.0, 1000, 1400.0)

    @pytest.mark.parametrize("field", ["open", "close", "fx_rate"])
    def test_0이하_가격은_거부된다(self, field: str) -> None:
        kwargs = {
            "ts": BASE_TS,
            "open": 20.0,
            "high": 21.0,
            "low": 19.0,
            "close": 20.0,
            "volume": 1000.0,
            "fx_rate": 1400.0,
        }
        kwargs[field] = 0.0
        with pytest.raises(ValueError, match="0보다 커야"):
            Bar(**kwargs)

    def test_0이하_고저가는_고저_역전_검사에_먼저_걸린다(self) -> None:
        """high=0 은 low 보다 작아지므로 순서상 고저 역전 오류가 먼저 난다."""
        with pytest.raises(ValueError, match="있을 수 없다"):
            Bar(BASE_TS, 20.0, 0.0, 19.0, 20.0, 1000.0, 1400.0)
        with pytest.raises(ValueError, match="0보다 커야"):
            Bar(BASE_TS, 20.0, 21.0, 0.0, 20.0, 1000.0, 1400.0)

    def test_NaN과_무한대는_거부된다(self) -> None:
        """NaN 은 모든 비교가 False 라 `<= 0` 검사를 그냥 통과한다.

        시세 API 나 pandas 에서 결측치가 그대로 넘어오면
        백테스트 전체가 조용히 오염되므로 생성 시점에 막는다.
        """
        nan = float("nan")
        with pytest.raises(ValueError, match="유한한 수가 아니다"):
            Bar(BASE_TS, 20.0, 21.0, 19.0, nan, 1000.0, 1400.0)
        with pytest.raises(ValueError, match="유한한 수가 아니다"):
            Bar(BASE_TS, 20.0, 21.0, 19.0, 20.0, 1000.0, float("inf"))
        with pytest.raises(ValueError, match="유한한 수가 아니다"):
            Bar(BASE_TS, 20.0, 21.0, 19.0, 20.0, nan, 1400.0)

    def test_음수_거래량은_거부된다(self) -> None:
        with pytest.raises(ValueError, match="음수"):
            Bar(BASE_TS, 20.0, 21.0, 19.0, 20.0, -1.0, 1400.0)

    def test_원화종가와_대표가가_계산된다(self) -> None:
        b = Bar(BASE_TS, 20.0, 22.0, 18.0, 21.0, 1000, 1400.0)
        assert b.krw_close == pytest.approx(21.0 * 1400.0)
        assert b.typical == pytest.approx((22.0 + 18.0 + 21.0) / 3)


class TestQuote:
    def test_역전된_호가는_거부된다(self) -> None:
        with pytest.raises(ValueError, match="비정상 호가"):
            Quote("KORU", 20.0, bid=21.0, ask=20.0, ts=BASE_TS, fx_rate=1400.0)

    def test_스프레드비율이_계산된다(self) -> None:
        q = Quote("KORU", 20.0, bid=19.9, ask=20.1, ts=BASE_TS, fx_rate=1400.0)
        assert q.mid == pytest.approx(20.0)
        assert q.spread_pct == pytest.approx(0.2 / 20.0)


class TestLot:
    @pytest.mark.parametrize(
        ("qty", "price", "fx", "cost", "idx"),
        [(0, 20.0, 1400.0, 1000.0, 0), (10, 0.0, 1400.0, 1000.0, 0), (10, 20.0, 1400.0, 0.0, 0)],
    )
    def test_잘못된_값은_거부된다(
        self, qty: int, price: float, fx: float, cost: float, idx: int
    ) -> None:
        with pytest.raises(ValueError):
            Lot("X", qty, price, fx, cost, BASE_TS, idx)

    def test_음수_차수는_거부된다(self) -> None:
        with pytest.raises(ValueError, match="tranche_index"):
            Lot("X", 10, 20.0, 1400.0, 1000.0, BASE_TS, -1)

    def test_주당_원가가_계산된다(self) -> None:
        assert _lot(10, 20.0, 1400.0, 0).krw_unit_cost == pytest.approx(28_000.0)


class TestPosition:
    def test_빈_포지션의_집계값은_모두_0이다(self) -> None:
        p = Position("KORU")
        assert not p.is_open
        assert p.qty == 0
        assert p.cost_krw == 0
        assert p.avg_krw_unit_cost == 0
        assert p.avg_price_usd == 0
        assert p.avg_fx_rate == 0
        assert p.first_entry_price_usd == 0
        assert p.opened_at is None

    def test_빈_심볼은_거부된다(self) -> None:
        with pytest.raises(ValueError, match="symbol"):
            Position("")

    def test_랏을_더하면_수량과_원가가_누적된다(self) -> None:
        p = Position("KORU").with_lot(_lot(40, 20.0, 1400.0, 0)).with_lot(_lot(35, 18.0, 1390.0, 1))
        assert p.qty == 75
        assert p.tranche_count == 2
        assert p.cost_krw == pytest.approx(40 * 20 * 1400 + 35 * 18 * 1390)
        assert p.avg_price_usd == pytest.approx((40 * 20 + 35 * 18) / 75)

    def test_손절_기준은_평단이_아니라_1차_진입가다(self) -> None:
        """분할 매수로 평단이 내려가도 손절선은 그대로여야 한다."""
        p = Position("KORU").with_lot(_lot(40, 20.0, 1400.0, 0)).with_lot(_lot(35, 15.0, 1400.0, 1))
        assert p.avg_price_usd < 20.0
        assert p.first_entry_price_usd == 20.0

    def test_익절_시작_전에는_기준수량이_갱신된다(self) -> None:
        p = Position("KORU").with_lot(_lot(40, 20.0, 1400.0, 0))
        assert p.ladder_base_qty == 40
        p = p.with_lot(_lot(35, 18.0, 1400.0, 1))
        assert p.ladder_base_qty == 75

    def test_익절_시작_후에는_기준수량이_고정된다(self) -> None:
        from dataclasses import replace

        p = Position("KORU").with_lot(_lot(40, 20.0, 1400.0, 0))
        p = replace(p, tp_levels_hit=frozenset({0}))
        p = p.with_lot(_lot(10, 18.0, 1400.0, 1))
        assert p.ladder_base_qty == 40  # 75가 아니다

    def test_음수_기준수량과_ATR은_거부된다(self) -> None:
        with pytest.raises(ValueError, match="ladder_base_qty"):
            Position("KORU", ladder_base_qty=-1)
        with pytest.raises(ValueError, match="entry_atr_usd"):
            Position("KORU", entry_atr_usd=-1.0)


class TestPositionReduced:
    """분할 매도의 원가 배분. 여기가 틀리면 익절 수익률이 전부 틀린다."""

    def test_전량_매도하면_원가_전부가_차감되고_포지션이_빈다(self) -> None:
        p = Position("KORU").with_lot(_lot(40, 20.0, 1400.0, 0))
        left, removed = p.reduced(40)
        assert not left.is_open
        assert removed == pytest.approx(p.cost_krw)

    @pytest.mark.parametrize("sell", [1, 7, 25, 49, 74])
    def test_어떤_수량을_팔아도_원가_합이_보존된다(self, sell: int) -> None:
        """차감된 원가 + 남은 원가 = 원래 원가. 이 항등식이 회계의 전부다."""
        p = Position("KORU").with_lot(_lot(40, 20.0, 1400.0, 0)).with_lot(_lot(35, 18.0, 1390.0, 1))
        left, removed = p.reduced(sell)
        assert left.qty == 75 - sell
        assert removed + left.cost_krw == pytest.approx(p.cost_krw, rel=1e-9)

    def test_세_차수에서도_원가가_보존된다(self) -> None:
        p = Position("KORU")
        for i, (q, pr) in enumerate([(40, 20.0), (35, 18.0), (25, 16.0)]):
            p = p.with_lot(_lot(q, pr, 1400.0 - i * 5, i))
        for sell in range(1, 100):
            left, removed = p.reduced(sell)
            assert removed + left.cost_krw == pytest.approx(p.cost_krw, rel=1e-9)
            assert left.qty == 100 - sell

    def test_보유보다_많이_팔면_거부된다(self) -> None:
        p = Position("KORU").with_lot(_lot(40, 20.0, 1400.0, 0))
        with pytest.raises(ValueError, match="많이 매도할 수 없다"):
            p.reduced(41)

    def test_0주_이하_매도는_거부된다(self) -> None:
        p = Position("KORU").with_lot(_lot(40, 20.0, 1400.0, 0))
        with pytest.raises(ValueError, match="1 이상"):
            p.reduced(0)

    def test_비례배분은_각_랏의_주당원가를_유지한다(self) -> None:
        p = Position("KORU").with_lot(_lot(40, 20.0, 1400.0, 0)).with_lot(_lot(40, 10.0, 1400.0, 1))
        left, _ = p.reduced(40)
        # 각 랏에서 절반씩 빠졌으므로 남은 평단은 원래 평단과 같아야 한다.
        assert left.avg_krw_unit_cost == pytest.approx(p.avg_krw_unit_cost, rel=1e-9)


class TestOrder:
    def test_지정가에_가격이_없으면_거부된다(self) -> None:
        with pytest.raises(ValueError, match="limit_price_usd"):
            Order("id", "KORU", OrderSide.BUY, 10, OrderType.LIMIT)

    def test_멱등키가_비면_거부된다(self) -> None:
        with pytest.raises(ValueError, match="client_order_id"):
            Order("", "KORU", OrderSide.BUY, 10, OrderType.MARKET)

    def test_0주_주문은_거부된다(self) -> None:
        with pytest.raises(ValueError, match="1 이상"):
            Order("id", "KORU", OrderSide.BUY, 0, OrderType.MARKET)

    def test_시장가는_가격이_없어도_된다(self) -> None:
        o = Order("id", "KORU", OrderSide.SELL, 10, OrderType.MARKET)
        assert o.limit_price_usd is None


class TestDecision:
    def test_HOLD에_수량이_있으면_거부된다(self) -> None:
        with pytest.raises(ValueError, match="수량이 있을 수 없다"):
            Decision(Action.HOLD, qty=10, rationale="x")

    def test_행동에_수량이_없으면_거부된다(self) -> None:
        with pytest.raises(ValueError, match="1 이상"):
            Decision(Action.ENTER, qty=0, rationale="x")

    def test_근거가_없으면_거부된다(self) -> None:
        with pytest.raises(ValueError, match="rationale"):
            Decision(Action.HOLD)

    def test_실행가능_여부가_정확하다(self) -> None:
        assert Decision(Action.ENTER, qty=1, rationale="x").is_actionable
        assert not Decision(Action.HOLD, rationale="x").is_actionable
        assert not Decision(Action.BLOCKED, rationale="x").is_actionable


class TestEntryPlan:
    def test_빈_차수는_거부된다(self) -> None:
        with pytest.raises(ValueError, match="1개 차수"):
            EntryPlan((), 1.0, 18.0, 1000.0)

    def test_0주_차수는_거부된다(self) -> None:
        with pytest.raises(ValueError, match="1 이상"):
            EntryPlan((10, 0), 1.0, 18.0, 1000.0)

    def test_총수량이_합산된다(self) -> None:
        assert EntryPlan((40, 35, 25), 1.0, 18.0, 1000.0).total_qty == 100


class TestTradeRecord:
    def _record(self, cost: float, proceeds: float) -> TradeRecord:
        return TradeRecord(
            symbol="KORU",
            opened_at=BASE_TS,
            closed_at=BASE_TS + dt.timedelta(days=3),
            tranches=2,
            max_qty=100,
            entry_avg_price_usd=20.0,
            exit_avg_price_usd=22.0,
            entry_avg_fx=1400.0,
            exit_avg_fx=1350.0,
            cost_krw=cost,
            proceeds_krw=proceeds,
            exit_reasons=(ExitReason.TAKE_PROFIT_LADDER,),
        )

    def test_원화수익률과_손익이_일치한다(self) -> None:
        r = self._record(1_000_000, 1_050_000)
        assert r.pnl_krw == pytest.approx(50_000)
        assert r.krw_return == pytest.approx(0.05)
        assert r.is_win

    def test_주가와_환율_기여도가_분리된다(self) -> None:
        r = self._record(1_000_000, 1_050_000)
        assert r.usd_return == pytest.approx(0.10)
        assert r.fx_return == pytest.approx(1350 / 1400 - 1)

    def test_보유기간이_일_단위로_계산된다(self) -> None:
        assert self._record(1, 1).holding_days == pytest.approx(3.0)

    def test_원가가_0이면_수익률이_0이다(self) -> None:
        assert self._record(0.0, 100.0).krw_return == 0.0


class TestFill:
    def test_0주_체결은_거부된다(self) -> None:
        with pytest.raises(ValueError, match="1 이상"):
            Fill("c", "b", "KORU", OrderSide.BUY, 0, 20.0, 1400.0, 0.0, BASE_TS)

    def test_0이하_가격은_거부된다(self) -> None:
        with pytest.raises(ValueError, match="0보다 커야"):
            Fill("c", "b", "KORU", OrderSide.BUY, 1, 0.0, 1400.0, 0.0, BASE_TS)
