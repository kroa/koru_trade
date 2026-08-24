"""상태 영속화와 실거래 실행기 검증.

봇이 죽었다 살아났을 때 포지션과 리스크 카운터를 정확히 복구하지 못하면,
이미 들고 있는 포지션을 모른 채 새로 진입하거나
손실 한도를 넘긴 상태에서 매매를 재개하게 된다.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest
from tests.conftest import BASE_TS, make_series, open_position

from koru_trade.broker.base import OrderStatus
from koru_trade.broker.paper import PaperBroker
from koru_trade.config import StrategyConfig
from koru_trade.live.runner import LiveRunner, _idempotency_key
from koru_trade.live.state import StateStore
from koru_trade.models import Action, Bar, Position, Quote
from koru_trade.risk import RiskState


@pytest.fixture
def store() -> StateStore:
    s = StateStore(":memory:")
    yield s
    s.close()


class TestPositionPersistence:
    def test_없으면_빈_포지션을_돌려준다(self, store: StateStore) -> None:
        assert not store.load_position("KORU").is_open

    def test_저장하고_읽으면_동일하다(self, store: StateStore) -> None:
        pos = open_position(qty=40, price=20.0, fx=1400.0, atr=1.23, plan=(40, 35, 25))
        store.save_position(pos)
        loaded = store.load_position("KORU")
        assert loaded.qty == pos.qty
        assert loaded.cost_krw == pytest.approx(pos.cost_krw)
        assert loaded.entry_atr_usd == pytest.approx(1.23)
        assert loaded.planned_tranche_qty == (40, 35, 25)
        assert loaded.first_entry_price_usd == pytest.approx(20.0)

    def test_익절_진행상태가_보존된다(self, store: StateStore) -> None:
        """재시작 후 같은 계단을 다시 팔면 안 된다."""
        pos = replace(
            open_position(qty=70),
            tp_levels_hit=frozenset({0, 1}),
            peak_krw_return=0.13,
            ladder_base_qty=100,
        )
        store.save_position(pos)
        loaded = store.load_position("KORU")
        assert loaded.tp_levels_hit == frozenset({0, 1})
        assert loaded.peak_krw_return == pytest.approx(0.13)
        assert loaded.ladder_base_qty == 100

    def test_여러_랏이_보존된다(self, store: StateStore) -> None:
        from koru_trade.models import Lot

        pos = open_position(qty=40, price=20.0)
        pos = pos.with_lot(Lot("B", 35, 18.0, 1390.0, 35 * 18.0 * 1390.0, BASE_TS, 1))
        store.save_position(pos)
        loaded = store.load_position("KORU")
        assert loaded.tranche_count == 2
        assert loaded.avg_price_usd == pytest.approx(pos.avg_price_usd)

    def test_수량이_0이면_삭제된다(self, store: StateStore) -> None:
        store.save_position(open_position(qty=10))
        store.save_position(Position("KORU"))
        assert not store.load_position("KORU").is_open

    def test_덮어쓰기가_동작한다(self, store: StateStore) -> None:
        store.save_position(open_position(qty=10))
        store.save_position(open_position(qty=20))
        assert store.load_position("KORU").qty == 20


class TestRiskPersistence:
    def test_같은_날짜는_그대로_복구된다(self, store: StateStore) -> None:
        risk = (
            RiskState(dt.date(2026, 8, 24))
            .with_entry(1_000_000)
            .with_realized(-50_000, closed_trade=True)
        )
        store.save_risk(risk)
        loaded = store.load_risk(dt.date(2026, 8, 24))
        assert loaded.notional_krw_today == pytest.approx(1_000_000)
        assert loaded.realized_krw_today == pytest.approx(-50_000)
        assert loaded.consecutive_losses == 1

    def test_다음_날은_일일집계가_초기화되고_연속손실은_유지된다(self, store: StateStore) -> None:
        risk = RiskState(dt.date(2026, 8, 24))
        for _ in range(2):
            risk = risk.with_realized(-10_000, closed_trade=True)
        risk = risk.with_entry(500_000)
        store.save_risk(risk)

        nextday = store.load_risk(dt.date(2026, 8, 25))
        assert nextday.trade_date == dt.date(2026, 8, 25)
        assert nextday.notional_krw_today == 0.0
        assert nextday.realized_krw_today == 0.0
        assert nextday.consecutive_losses == 2

    def test_기록이_없으면_빈_상태다(self, store: StateStore) -> None:
        risk = store.load_risk(dt.date(2026, 8, 24))
        assert risk.entries_today == 0
        assert risk.consecutive_losses == 0

    def test_킬스위치_사유가_보존된다(self, store: StateStore) -> None:
        risk = RiskState(dt.date(2026, 8, 24)).with_halt("당일 손실 한도")
        store.save_risk(risk)
        assert store.load_risk(dt.date(2026, 8, 24)).is_halted


class TestOrderIdempotency:
    def test_기록되지_않은_주문은_없다고_나온다(self, store: StateStore) -> None:
        assert not store.has_order("nope")

    def test_기록한_주문은_다시_찾을_수_있다(self, store: StateStore) -> None:
        store.record_order(
            "cid-1",
            broker_order_id="B1",
            side="BUY",
            qty=10,
            limit_price=20.5,
            status="ACCEPTED",
        )
        assert store.has_order("cid-1")

    def test_같은_키를_두_번_기록해도_예외가_없다(self, store: StateStore) -> None:
        for _ in range(2):
            store.record_order(
                "cid-1", broker_order_id="B1", side="BUY", qty=10, limit_price=1.0, status="A"
            )
        assert store.has_order("cid-1")


class TestAuditLog:
    def test_판단_기록이_남고_최신순으로_읽힌다(self, store: StateStore) -> None:
        for i in range(3):
            store.log_decision(
                ts=BASE_TS + dt.timedelta(minutes=i),
                action="HOLD",
                qty=0,
                price_usd=20.0 + i,
                fx_rate=1400.0,
                krw_return=0.0,
                rationale=f"근거 {i}",
            )
        rows = store.recent_decisions(10)
        assert len(rows) == 3
        assert rows[0]["rationale"] == "근거 2"

    def test_제한_개수만큼만_반환한다(self, store: StateStore) -> None:
        for i in range(20):
            store.log_decision(
                ts=BASE_TS + dt.timedelta(minutes=i),
                action="HOLD",
                qty=0,
                price_usd=20.0,
                fx_rate=1400.0,
                krw_return=0.0,
                rationale=str(i),
            )
        assert len(store.recent_decisions(5)) == 5


class TestFileStore:
    def test_파일에_저장하고_새_인스턴스로_읽는다(self, tmp_path) -> None:
        """프로세스가 죽었다 살아나는 상황의 재현."""
        db = tmp_path / "state" / "koru.db"
        first = StateStore(db)
        first.save_position(open_position(qty=42))
        first.save_risk(RiskState(dt.date(2026, 8, 24)).with_entry(1_000_000))

        second = StateStore(db)
        assert second.load_position("KORU").qty == 42
        assert second.load_risk(dt.date(2026, 8, 24)).notional_krw_today == pytest.approx(1_000_000)


class TestIdempotencyKey:
    def test_같은_봉_같은_결정은_같은_키다(self) -> None:
        ts = dt.datetime(2026, 8, 24, 23, 30, 15)
        a = _idempotency_key("KORU", "ENTER", ts, 40)
        b = _idempotency_key("KORU", "ENTER", ts.replace(second=59), 40)
        assert a == b  # 초 단위는 절삭된다

    def test_수량이_다르면_키가_다르다(self) -> None:
        ts = dt.datetime(2026, 8, 24, 23, 30)
        assert _idempotency_key("KORU", "ENTER", ts, 40) != _idempotency_key(
            "KORU", "ENTER", ts, 41
        )

    def test_행동이_다르면_키가_다르다(self) -> None:
        ts = dt.datetime(2026, 8, 24, 23, 30)
        assert _idempotency_key("KORU", "ENTER", ts, 40) != _idempotency_key(
            "KORU", "SCALE_IN", ts, 40
        )

    def test_다른_분이면_키가_다르다(self) -> None:
        base = dt.datetime(2026, 8, 24, 23, 30)
        assert _idempotency_key("KORU", "ENTER", base, 40) != _idempotency_key(
            "KORU", "ENTER", base + dt.timedelta(minutes=1), 40
        )


class TestLiveRunner:
    @staticmethod
    def _broker(bars: tuple[Bar, ...], cash: float = 100_000.0) -> PaperBroker:
        last = bars[-1]

        def quote(symbol: str) -> Quote:
            return Quote(
                symbol, last.close, last.close * 0.999, last.close * 1.001, last.ts, last.fx_rate
            )

        return PaperBroker(quote, initial_cash_usd=cash, fill_immediately=True)

    def test_봉이_없으면_거부된다(self, store: StateStore, loose_cfg: StrategyConfig) -> None:
        runner = LiveRunner(self._broker(make_series(2)), store, loose_cfg)
        with pytest.raises(ValueError, match="비어 있다"):
            runner.tick([])

    def test_진입_신호에_주문이_나가고_포지션이_저장된다(
        self, store: StateStore, loose_cfg: StrategyConfig
    ) -> None:
        bars = make_series(150, drift=0.003, amplitude=0.01)
        runner = LiveRunner(self._broker(bars), store, loose_cfg)
        result = runner.tick(bars)
        assert result.decision.action is Action.ENTER
        assert result.order_result is not None
        assert result.order_result.status is OrderStatus.ACCEPTED
        assert store.load_position(loose_cfg.symbol).is_open

    def test_같은_봉을_두_번_처리해도_주문이_중복되지_않는다(
        self, store: StateStore, loose_cfg: StrategyConfig
    ) -> None:
        """프로세스 재시작 후 같은 봉을 다시 처리하는 상황의 핵심 방어."""
        bars = make_series(150, drift=0.003, amplitude=0.01)
        broker = self._broker(bars)
        runner = LiveRunner(broker, store, loose_cfg)
        runner.tick(bars)
        fills_after_first = len(broker.fills)
        second = runner.tick(bars)
        assert len(broker.fills) == fills_after_first
        assert second.order_result is None  # 멱등키로 걸러졌다

    def test_HOLD면_주문이_나가지_않는다(self, store: StateStore, cfg: StrategyConfig) -> None:
        bars = make_series(150, drift=-0.005)
        broker = self._broker(bars)
        runner = LiveRunner(broker, store, cfg)
        result = runner.tick(bars)
        assert result.decision.action is Action.HOLD
        assert result.order is None
        assert not broker.fills

    def test_모든_판단이_감사로그에_남는다(self, store: StateStore, cfg: StrategyConfig) -> None:
        bars = make_series(150, drift=-0.005)
        LiveRunner(self._broker(bars), store, cfg).tick(bars)
        rows = store.recent_decisions(5)
        assert rows
        assert rows[0]["rationale"]

    def test_잔고_불일치를_보고한다(self, store: StateStore, loose_cfg: StrategyConfig) -> None:
        bars = make_series(150, drift=0.003, amplitude=0.01)
        broker = self._broker(bars)
        runner = LiveRunner(broker, store, loose_cfg)
        store.save_position(open_position(qty=999))
        warning = runner.reconcile()
        assert warning is not None
        assert "포지션 불일치" in warning

    def test_잔고_조회_실패를_경고로_처리한다(self, store: StateStore, cfg: StrategyConfig) -> None:
        class Broken:
            dry_run = True

            def get_balance(self, symbol: str):
                raise RuntimeError("연결 끊김")

        runner = LiveRunner(Broken(), store, cfg)  # type: ignore[arg-type]
        warning = runner.reconcile()
        assert warning is not None
        assert "잔고 조회 실패" in warning

    def test_결과_요약_문자열이_생성된다(
        self, store: StateStore, loose_cfg: StrategyConfig
    ) -> None:
        bars = make_series(150, drift=0.003, amplitude=0.01)
        result = LiveRunner(self._broker(bars), store, loose_cfg).tick(bars)
        summary = result.summary()
        assert "ENTER" in summary
        assert "원화수익률" in summary
