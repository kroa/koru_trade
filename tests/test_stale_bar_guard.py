"""시세가 과거로 후퇴했을 때 판단을 멈추는지 검증.

2026-09-24 에 실제로 난 사고를 막는 장치다. 제공자가 09-22 봉의 종가를
뒤늦게 채워 넣는 동안 09-23 일봉이 아직 집계 전이어서, 받아 온 시계열의
마지막 봉이 09-23($21.27)에서 09-22($23.77)로 하루 물러났다.

봇은 $21.34 에 산 41주를 $23.77 과 비교해 원화 +9.41% 로 착각했고,
익절 2단을 발동시켜 **존재하지 않는 가격** $23.73 에 18주 매도 주문을 냈다.
그 주문은 중복 실행 중이던 다른 프로세스의 페이퍼 브로커가 잔고를 몰라서
거절했다. 버그가 버그를 막은 것이지 안전장치가 작동한 게 아니다.

여기서 못 박는 계약은 하나다. **봉 시각이 뒤로 가면 주문을 내지 않는다.**
값이 비슷한지는 보지 않는다. 낡은 봉으로 내린 판단은 전부 틀리기 때문이다.
"""

from __future__ import annotations

import datetime as dt

import pytest
from tests.conftest import make_series

from koru_trade.broker.paper import PaperBroker
from koru_trade.config import StrategyConfig
from koru_trade.live.runner import LiveRunner
from koru_trade.live.state import StateStore
from koru_trade.models import Action, Bar, Quote
from koru_trade.notify.base import NotifyResult


@pytest.fixture
def store(tmp_path) -> StateStore:  # type: ignore[no-untyped-def]
    return StateStore(tmp_path / "state.db")


def _quote(symbol: str) -> Quote:
    """페이퍼 브로커가 체결가를 물어볼 때 쓰는 고정 시세."""
    return Quote(
        symbol=symbol,
        last=21.27,
        bid=21.26,
        ask=21.28,
        ts=dt.datetime(2026, 9, 23),
        fx_rate=1360.0,
    )


def _broker() -> PaperBroker:
    return PaperBroker(_quote)


def _runner(store: StateStore, cfg: StrategyConfig) -> LiveRunner:
    return LiveRunner(_broker(), store, cfg)


def _at(bars: tuple[Bar, ...], ts: dt.datetime, close: float) -> tuple[Bar, ...]:
    """마지막 봉을 지정한 시각·종가로 바꾼 시계열."""
    last = bars[-1]
    return (
        *bars[:-1],
        Bar(
            ts=ts,
            open=last.open,
            high=max(last.high, close),
            low=min(last.low, close),
            close=close,
            volume=last.volume,
            fx_rate=last.fx_rate,
        ),
    )


class TestRegressionIsRefused:
    def test_봉이_하루_뒤로_가면_주문을_안_낸다(
        self, store: StateStore, cfg: StrategyConfig
    ) -> None:
        bars = make_series(80, drift=0.004)
        runner = _runner(store, cfg)
        newer = _at(bars, dt.datetime(2026, 9, 23), 21.27)
        runner.tick(newer)

        older = _at(bars, dt.datetime(2026, 9, 22), 23.77)
        result = runner.tick(older)

        assert result.decision.action is Action.HOLD
        assert result.order is None, "낡은 봉으로 주문이 나갔다"
        assert result.order_result is None
        assert "후퇴" in result.decision.rationale

    def test_같은_봉은_막지_않는다(self, store: StateStore, cfg: StrategyConfig) -> None:
        """같은 봉을 다시 보는 것은 정상이다. 멱등키가 중복 주문을 막는다."""
        bars = make_series(80, drift=0.004)
        runner = _runner(store, cfg)
        same = _at(bars, dt.datetime(2026, 9, 23), 21.27)
        runner.tick(same)
        assert "후퇴" not in runner.tick(same).decision.rationale

    def test_더_새_봉은_통과한다(self, store: StateStore, cfg: StrategyConfig) -> None:
        bars = make_series(80, drift=0.004)
        runner = _runner(store, cfg)
        runner.tick(_at(bars, dt.datetime(2026, 9, 23), 21.27))
        nxt = runner.tick(_at(bars, dt.datetime(2026, 9, 24), 21.30))
        assert "후퇴" not in nxt.decision.rationale

    def test_첫_틱은_비교할_대상이_없어_통과한다(
        self, store: StateStore, cfg: StrategyConfig
    ) -> None:
        bars = make_series(80, drift=0.004)
        r = _runner(store, cfg).tick(_at(bars, dt.datetime(2026, 9, 22), 23.77))
        assert "후퇴" not in r.decision.rationale


class TestSurvivesRestart:
    """메모리에만 들고 있으면 프로세스를 올릴 때마다 구멍이 다시 열린다."""

    def test_재시작해도_후퇴를_잡는다(self, store: StateStore, cfg: StrategyConfig) -> None:
        bars = make_series(80, drift=0.004)
        _runner(store, cfg).tick(_at(bars, dt.datetime(2026, 9, 23), 21.27))

        revived = _runner(store, cfg)  # 같은 DB 로 새 러너
        result = revived.tick(_at(bars, dt.datetime(2026, 9, 22), 23.77))

        assert result.order is None
        assert "후퇴" in result.decision.rationale

    def test_기록이_없으면_None_이다(self, store: StateStore) -> None:
        assert store.last_decision_ts() is None

    def test_id_가_아니라_ts_의_최대값을_쓴다(self, store: StateStore, cfg: StrategyConfig) -> None:
        """후퇴 사고 때는 나중에 쓴 행이 더 오래된 ts 를 갖는다.

        MAX(id) 로 읽으면 낡은 시각을 기준으로 삼아 구멍이 그대로 열린다.
        """
        for ts, px in (
            (dt.datetime(2026, 9, 23), 21.27),
            (dt.datetime(2026, 9, 22), 23.77),  # 나중에 쓰였지만 더 과거
        ):
            store.log_decision(
                ts=ts,
                action="HOLD",
                qty=0,
                price_usd=px,
                fx_rate=1360.0,
                krw_return=0.0,
                rationale="테스트",
            )
        assert store.last_decision_ts() == dt.datetime(2026, 9, 23)

    def test_깨진_ts_는_None_으로_떨어진다(self, store: StateStore) -> None:
        """해석 못 하는 값에 터지면 봇 전체가 멈춘다."""
        with store._connect() as conn:
            conn.execute(
                "INSERT INTO audit(ts, action, qty, price_usd, fx_rate, krw_return, rationale)"
                " VALUES(?,?,?,?,?,?,?)",
                ("깨진값", "HOLD", 0, 1.0, 1360.0, 0.0, "테스트"),
            )
        assert store.last_decision_ts() is None


class TestNotifies:
    def test_후퇴하면_알림을_보낸다(self, store: StateStore, cfg: StrategyConfig) -> None:
        """조용히 건너뛰면 며칠째 멈춰 있어도 아무도 모른다."""
        sent: list[str] = []

        class Spy:
            def send(self, text: str, *, dedupe_key: str | None = None) -> NotifyResult:
                sent.append(text)
                return NotifyResult.sent()

        bars = make_series(80, drift=0.004)
        runner = LiveRunner(_broker(), store, cfg, notifier=Spy())
        runner.tick(_at(bars, dt.datetime(2026, 9, 23), 21.27))
        runner.tick(_at(bars, dt.datetime(2026, 9, 22), 23.77))

        assert any("후퇴" in t for t in sent), f"알림이 안 갔다: {sent}"
