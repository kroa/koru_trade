"""재시작 후 포지션 기록과 브로커 잔고가 어긋나는 사고 방지.

2026-09-08 22:45 감시가 모의투자로 37주를 샀다. 23:39 에 코드를 반영하려고
감시를 재시작했다. 페이퍼 브로커는 잔고를 메모리에만 들고 있어 0주가 됐고,
상태 DB 에는 37주가 남았다. 이후 매도가 "보유 0주보다 많이 팔 수 없다" 로
사흘간 349번 거절됐고, 거절은 로그에만 남아 아무도 몰랐다. DB 가 포지션이
있다고 믿어서 진입 조건 검사도 멈췄다.

이 파일은 그 사고를 그대로 재현하고, 수정이 그것을 막는지 확인한다.
"""

from __future__ import annotations

import inspect
from dataclasses import replace

import pytest
from tests.conftest import make_series, open_position

from koru_trade import cli
from koru_trade.broker.base import OrderStatus
from koru_trade.broker.paper import PaperBroker
from koru_trade.cli import _restore_paper_position
from koru_trade.config import StrategyConfig
from koru_trade.live.runner import LiveRunner
from koru_trade.live.state import StateStore
from koru_trade.models import Action, Bar, Quote
from koru_trade.notify.base import NotifyResult
from koru_trade.notify.format import format_rejected


class _Recorder:
    """보낸 알림을 기록하고 같은 키는 한 번만 보내는 알림기."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str | None]] = []
        self._seen: set[str] = set()

    @property
    def enabled(self) -> bool:
        return True

    def send(self, text: str, *, dedupe_key: str | None = None) -> NotifyResult:
        if dedupe_key is not None:
            if dedupe_key in self._seen:
                return NotifyResult.skip("중복")
            self._seen.add(dedupe_key)
        self.sent.append((text, dedupe_key))
        return NotifyResult.sent()


@pytest.fixture
def store() -> StateStore:
    s = StateStore(":memory:")
    yield s
    s.close()


def _paper(bars: tuple[Bar, ...]) -> PaperBroker:
    """프로세스가 막 뜬 상태의 페이퍼 브로커. 잔고는 비어 있다."""
    last = bars[-1]

    def quote(symbol: str) -> Quote:
        return Quote(
            symbol, last.close, last.close * 0.999, last.close * 1.001, last.ts, last.fx_rate
        )

    return PaperBroker(quote, initial_cash_usd=100_000.0, fill_immediately=True)


def _held_for_days(store: StateStore, cfg: StrategyConfig) -> tuple[Bar, ...]:
    """보유기간 1일을 훌쩍 넘긴 37주 포지션을 DB 에 심는다(사고 당시 상태)."""
    bars = make_series(150)
    entry = bars[-20]
    store.save_position(
        open_position(qty=37, price=entry.close, ts=entry.ts, cost=cfg.cost, atr=1.0)
    )
    return bars


@pytest.fixture
def time_stop_cfg(loose_cfg: StrategyConfig) -> StrategyConfig:
    return replace(loose_cfg, max_holding_days=1)


class TestIncidentReproduction:
    def test_복원하지_않으면_매도가_거절되고_DB는_그대로다(
        self, store: StateStore, time_stop_cfg: StrategyConfig
    ) -> None:
        """사고를 그대로 재현한다. 이 테스트가 깨지면 전제가 바뀐 것이다."""
        bars = _held_for_days(store, time_stop_cfg)
        runner = LiveRunner(_paper(bars), store, time_stop_cfg, notifier=_Recorder())

        result = runner.tick(bars)

        assert result.decision.action is Action.EXIT
        assert result.order_result is not None
        assert result.order_result.status is OrderStatus.REJECTED
        assert store.load_position(time_stop_cfg.symbol).qty == 37

    def test_시작할때_복원하면_매도가_체결되고_포지션이_닫힌다(
        self, store: StateStore, time_stop_cfg: StrategyConfig
    ) -> None:
        bars = _held_for_days(store, time_stop_cfg)
        broker = _paper(bars)

        _restore_paper_position(broker, store, time_stop_cfg)
        assert broker.get_balance(time_stop_cfg.symbol).qty == 37

        result = LiveRunner(broker, store, time_stop_cfg).tick(bars)

        assert result.decision.action is Action.EXIT
        assert result.order_result is not None
        assert result.order_result.status is OrderStatus.ACCEPTED
        assert not store.load_position(time_stop_cfg.symbol).is_open

    def test_복원_후_대조하면_불일치가_없다(
        self, store: StateStore, time_stop_cfg: StrategyConfig
    ) -> None:
        bars = _held_for_days(store, time_stop_cfg)
        broker = _paper(bars)
        _restore_paper_position(broker, store, time_stop_cfg)
        assert LiveRunner(broker, store, time_stop_cfg).reconcile() is None

    def test_복원하지_않으면_대조가_불일치를_잡는다(
        self, store: StateStore, time_stop_cfg: StrategyConfig
    ) -> None:
        """감시가 시작할 때 이 대조만 돌렸어도 사고는 첫날 드러났다."""
        bars = _held_for_days(store, time_stop_cfg)
        warning = LiveRunner(_paper(bars), store, time_stop_cfg).reconcile()
        assert warning is not None
        assert "로컬 37주" in warning
        assert "브로커 0주" in warning


class TestRejectionAlert:
    def test_주문이_거절되면_텔레그램으로_알린다(
        self, store: StateStore, time_stop_cfg: StrategyConfig
    ) -> None:
        bars = _held_for_days(store, time_stop_cfg)
        rec = _Recorder()
        LiveRunner(_paper(bars), store, time_stop_cfg, notifier=rec).tick(bars)

        rejected = [text for text, key in rec.sent if key and key.startswith("rejected-")]
        assert len(rejected) == 1
        assert "주문 거절" in rejected[0]
        assert "매도 37주" in rejected[0]
        assert "0주" in rejected[0]  # 거절 사유가 본문에 들어간다

    def test_감시가_같은_봉을_반복해서_봐도_거절_알림은_한_번이다(
        self, store: StateStore, time_stop_cfg: StrategyConfig
    ) -> None:
        """사고 당시 같은 봉을 15분마다 다시 봤다. 알림이 도배되면 안 된다."""
        bars = _held_for_days(store, time_stop_cfg)
        rec = _Recorder()
        runner = LiveRunner(_paper(bars), store, time_stop_cfg, notifier=rec)
        for _ in range(4):
            runner.tick(bars)

        rejected = [key for _, key in rec.sent if key and key.startswith("rejected-")]
        assert len(rejected) == 1

    def test_체결되면_거절_알림은_없다(
        self, store: StateStore, time_stop_cfg: StrategyConfig
    ) -> None:
        bars = _held_for_days(store, time_stop_cfg)
        broker = _paper(bars)
        _restore_paper_position(broker, store, time_stop_cfg)
        rec = _Recorder()
        LiveRunner(broker, store, time_stop_cfg, notifier=rec).tick(bars)
        assert not [key for _, key in rec.sent if key and key.startswith("rejected-")]

    def test_거절_사유의_꺾쇠는_이스케이프한다(
        self, store: StateStore, time_stop_cfg: StrategyConfig
    ) -> None:
        """텔레그램 HTML 모드는 짝이 안 맞는 꺾쇠가 있으면 메시지 전체를 버린다."""
        bars = _held_for_days(store, time_stop_cfg)
        result = LiveRunner(_paper(bars), store, time_stop_cfg).tick(bars)
        assert result.order is not None
        text = format_rejected(result.decision, result.order, "a<b>c", time_stop_cfg)
        assert "a&lt;b&gt;c" in text


class TestRestoreHelper:
    def test_DB에_포지션이_없으면_아무것도_하지_않는다(
        self, store: StateStore, loose_cfg: StrategyConfig
    ) -> None:
        broker = _paper(make_series(5))
        _restore_paper_position(broker, store, loose_cfg)
        assert broker.get_balance(loose_cfg.symbol).qty == 0

    def test_실브로커에는_손대지_않는다(self, store: StateStore, loose_cfg: StrategyConfig) -> None:
        """실계좌 잔고를 로컬 기록으로 덮어쓰면 안 된다."""
        store.save_position(open_position(qty=37))

        class RealBroker:
            dry_run = False

            def seed_position(self, *args: object, **kwargs: object) -> None:
                raise AssertionError("실브로커 잔고를 덮어쓰려 했다")

        _restore_paper_position(RealBroker(), store, loose_cfg)  # type: ignore[arg-type]


class TestSeedPosition:
    def test_음수_수량은_거부한다(self) -> None:
        with pytest.raises(ValueError, match="음수"):
            _paper(make_series(2)).seed_position("KORU", -1, 20.0)

    @pytest.mark.parametrize("price", [0.0, -1.0, float("nan")])
    def test_단가가_양수가_아니면_거부한다(self, price: float) -> None:
        with pytest.raises(ValueError, match="단가"):
            _paper(make_series(2)).seed_position("KORU", 10, price)

    def test_0주면_포지션을_지운다(self) -> None:
        broker = _paper(make_series(2))
        broker.seed_position("KORU", 10, 20.0)
        broker.seed_position("KORU", 0, 0.0)
        assert broker.get_balance("KORU").qty == 0

    def test_복원한_수량만큼_팔_수_있다(self) -> None:
        from koru_trade.models import Order, OrderSide, OrderType

        broker = _paper(make_series(2))
        broker.seed_position("KORU", 37, 24.25)
        result = broker.submit(Order("K1", "KORU", OrderSide.SELL, 37, OrderType.MARKET))
        assert result.status is OrderStatus.ACCEPTED
        assert broker.get_balance("KORU").qty == 0


def test_watch와_tick이_시작할때_복원한다() -> None:
    """구현에서 호출이 빠지는 회귀를 막는다."""
    watch = inspect.getsource(cli._cmd_watch)
    assert "_restore_paper_position(" in watch
    assert watch.index("_restore_paper_position(") < watch.index("LiveRunner(")
    assert "reconcile()" in watch
    assert inspect.getsource(cli._cmd_tick).count("_restore_paper_position(") == 1
