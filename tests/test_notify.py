"""알림 계층 검증.

핵심 불변식 두 가지.

1. **알림 실패가 매매를 막지 않는다.** 텔레그램이 죽어도, 토큰이 틀려도,
   네트워크가 끊겨도 :meth:`send` 는 예외를 던지지 않고 결과 객체를 돌려준다.
2. **봇 토큰이 새지 않는다.** 토큰은 URL 경로에 들어가므로, 예외 메시지에
   URL 이 실리면 그대로 로그에 남는다.
"""

from __future__ import annotations

import datetime as dt

import pytest
import requests
import responses
from tests.conftest import make_bar, open_position

from koru_trade.config import StrategyConfig
from koru_trade.models import Action, Decision, EntryPlan, ExitReason, Position
from koru_trade.notify.base import NotifyResult, NullNotifier, SignalNotifier
from koru_trade.notify.format import (
    format_blocked,
    format_daily_summary,
    format_decision,
    format_error,
)
from koru_trade.notify.telegram import (
    API_BASE,
    MAX_TEXT,
    TelegramConfig,
    TelegramNotifier,
    load_telegram_config,
)

TOKEN = "1234567890:FAKEfakeFAKEfakeFAKEfakeFAKEfakeFAK"
CHAT = "987654321"


@pytest.fixture
def conf() -> TelegramConfig:
    return TelegramConfig(bot_token=TOKEN, chat_id=CHAT)


@pytest.fixture
def notifier(conf: TelegramConfig) -> TelegramNotifier:
    return TelegramNotifier(conf, rate_limit=1000)


def _send_url() -> str:
    return f"{API_BASE}/bot{TOKEN}/sendMessage"


class TestConfig:
    def test_토큰이_없으면_거부된다(self) -> None:
        with pytest.raises(ValueError, match="모두 필요하다"):
            TelegramConfig(bot_token="", chat_id=CHAT)

    def test_형식이_아니면_거부된다(self) -> None:
        with pytest.raises(ValueError, match="봇 토큰 형식"):
            TelegramConfig(bot_token="not-a-token", chat_id=CHAT)

    def test_repr에_토큰이_노출되지_않는다(self, conf: TelegramConfig) -> None:
        for text in (repr(conf), str(conf), f"{conf}"):
            assert TOKEN not in text
            assert CHAT not in text

    def test_환경변수가_없으면_None이다(self) -> None:
        """알림이 없다고 매매를 막으면 안 된다."""
        assert load_telegram_config({}) is None
        assert load_telegram_config({"TELEGRAM_BOT_TOKEN": TOKEN}) is None
        assert load_telegram_config({"TELEGRAM_CHAT_ID": CHAT}) is None

    def test_잘못된_토큰이면_예외_대신_None이다(self) -> None:
        got = load_telegram_config({"TELEGRAM_BOT_TOKEN": "garbage", "TELEGRAM_CHAT_ID": CHAT})
        assert got is None

    def test_env_파일에서_읽는다(self, tmp_path, monkeypatch) -> None:
        """`.env` 를 채웠는데도 알림이 조용히 꺼져 있던 버그의 회귀 방지.

        load_credentials() 는 .env 를 로드하는데 load_telegram_config() 만
        빠뜨려서, 사용자가 값을 제대로 넣어도 os.environ 에는 없어 None 이 났다.
        환경변수를 읽는 진입점은 전부 .env 를 먼저 주입해야 한다.
        """
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        (tmp_path / ".env").write_text(
            "\n".join([f"TELEGRAM_BOT_TOKEN={TOKEN}", f"TELEGRAM_CHAT_ID={CHAT}", ""]),
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        got = load_telegram_config()
        assert got is not None, ".env 를 채웠는데 알림 설정을 못 찾았다"
        assert got.chat_id == CHAT

    def test_정상_환경변수를_읽는다(self) -> None:
        got = load_telegram_config({"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT})
        assert got is not None
        assert got.chat_id == CHAT


class TestNullNotifier:
    def test_항상_꺼져있다(self) -> None:
        n = NullNotifier()
        assert not n.enabled
        result = n.send("아무거나")
        assert result.ok
        assert result.skipped

    def test_프로토콜을_만족한다(self) -> None:
        assert isinstance(NullNotifier(), SignalNotifier)


class TestSend:
    @responses.activate
    def test_정상_전송(self, notifier: TelegramNotifier) -> None:
        responses.add(responses.POST, _send_url(), json={"ok": True}, status=200)
        result = notifier.send("테스트")
        assert result.ok
        assert not result.skipped
        body = responses.calls[0].request.body
        assert CHAT.encode() in body if isinstance(body, bytes) else CHAT in str(body)

    @responses.activate
    def test_중복키는_한_번만_보낸다(self, notifier: TelegramNotifier) -> None:
        """같은 봉에서 같은 신호가 반복될 때 도배를 막는다."""
        responses.add(responses.POST, _send_url(), json={"ok": True}, status=200)
        first = notifier.send("같은 신호", dedupe_key="k1")
        second = notifier.send("같은 신호", dedupe_key="k1")
        assert first.ok and not first.skipped
        assert second.ok and second.skipped
        assert len(responses.calls) == 1

    @responses.activate
    def test_다른_키는_각각_보낸다(self, notifier: TelegramNotifier) -> None:
        responses.add(responses.POST, _send_url(), json={"ok": True}, status=200)
        notifier.send("a", dedupe_key="k1")
        notifier.send("b", dedupe_key="k2")
        assert len(responses.calls) == 2

    @responses.activate
    def test_HTTP_오류가_예외를_던지지_않는다(self, notifier: TelegramNotifier) -> None:
        responses.add(
            responses.POST,
            _send_url(),
            json={"ok": False, "description": "chat not found"},
            status=400,
        )
        result = notifier.send("테스트")
        assert not result.ok
        assert "400" in result.detail

    @responses.activate
    def test_네트워크_오류가_예외를_던지지_않는다(self, notifier: TelegramNotifier) -> None:
        """이 테스트가 이 모듈의 존재 이유다. 알림 장애가 매매 장애가 되면 안 된다."""
        responses.add(responses.POST, _send_url(), body=requests.ConnectionError())
        result = notifier.send("테스트")
        assert not result.ok
        assert "전송 실패" in result.detail

    @responses.activate
    def test_타임아웃도_예외를_던지지_않는다(self, notifier: TelegramNotifier) -> None:
        responses.add(responses.POST, _send_url(), body=requests.Timeout())
        assert not notifier.send("테스트").ok

    @responses.activate
    def test_오류_메시지에_토큰이_없다(self, notifier: TelegramNotifier) -> None:
        """requests 예외 문자열에는 URL(=토큰)이 통째로 들어 있다."""
        responses.add(
            responses.POST,
            _send_url(),
            body=requests.ConnectionError(f"failed to reach {_send_url()}"),
        )
        result = notifier.send("테스트")
        assert TOKEN not in result.detail

    @responses.activate
    def test_긴_메시지는_잘린다(self, notifier: TelegramNotifier) -> None:
        responses.add(responses.POST, _send_url(), json={"ok": True}, status=200)
        notifier.send("가" * (MAX_TEXT + 500))
        sent = responses.calls[0].request.body
        text = sent.decode() if isinstance(sent, bytes) else str(sent)
        # JSON 인코딩 뒤라 정확한 길이 비교 대신 상한을 넘지 않는지만 본다
        assert len(text) < (MAX_TEXT + 500) * 6

    @responses.activate
    def test_check가_봇_이름을_확인한다(self, notifier: TelegramNotifier) -> None:
        responses.add(
            responses.GET,
            f"{API_BASE}/bot{TOKEN}/getMe",
            json={"ok": True, "result": {"username": "koru_bot"}},
            status=200,
        )
        result = notifier.check()
        assert result.ok
        assert "koru_bot" in result.detail

    @responses.activate
    def test_check_실패도_예외가_아니다(self, notifier: TelegramNotifier) -> None:
        responses.add(
            responses.GET, f"{API_BASE}/bot{TOKEN}/getMe", body=requests.ConnectionError()
        )
        assert not notifier.check().ok


class TestFormatDecision:
    """알림 본문은 휴대폰 한 화면에서 '무엇을 해야 하는지' 가 읽혀야 한다."""

    def test_매수_알림에_핵심_정보가_있다(self, cfg: StrategyConfig) -> None:
        bar = make_bar(close=20.0, fx=1400.0)
        plan = EntryPlan(
            tranche_qty=(40, 35, 25),
            entry_atr_usd=1.0,
            stop_price_usd=18.0,
            total_notional_krw=2_800_000,
        )
        d = Decision(
            Action.ENTER,
            qty=40,
            limit_price_usd=20.03,
            entry_plan=plan,
            rationale="진입 조건 전부 통과",
        )
        text = format_decision(d, bar, Position("KORU"), cfg, dry_run=False)
        assert "매수 신호" in text
        assert "KORU" in text
        assert "40주" in text
        assert "손절선" in text
        assert "18.00" in text
        assert "DRY_RUN" not in text

    def test_드라이런이_본문에_표시된다(self, cfg: StrategyConfig) -> None:
        """주문이 안 나갔는데 나간 줄 알면 위험하다."""
        bar = make_bar(close=20.0, fx=1400.0)
        plan = EntryPlan((40,), 1.0, 18.0, 1_000_000)
        d = Decision(Action.ENTER, qty=40, limit_price_usd=20.0, entry_plan=plan, rationale="x")
        text = format_decision(d, bar, Position("KORU"), cfg, dry_run=True)
        assert "DRY_RUN" in text

    def test_익절_알림에_실현손익과_잔여가_있다(self, cfg: StrategyConfig) -> None:
        pos = open_position(qty=100, price=20.0, fx=1400.0, atr=1.0)
        bar = make_bar(close=22.0, fx=1400.0)
        d = Decision(
            Action.TAKE_PROFIT,
            qty=30,
            limit_price_usd=22.0,
            reason=ExitReason.TAKE_PROFIT_LADDER,
            tp_levels=(0,),
            rationale="1단 익절",
        )
        text = format_decision(d, bar, pos, cfg, dry_run=False)
        assert "분할 익절" in text
        assert "실현" in text
        assert "잔여" in text
        assert "70주" in text
        # 방금 소진한 1단(+5%)이 아니라 2단(+9%)을 다음 목표로 안내해야 한다
        assert "다음 익절 원화 +9%" in text
        assert "다음 익절 원화 +5%" not in text

    def test_전량청산이_명시된다(self, cfg: StrategyConfig) -> None:
        pos = open_position(qty=100, price=20.0, fx=1400.0, atr=1.0)
        bar = make_bar(close=17.0, fx=1400.0)
        d = Decision(
            Action.EXIT,
            qty=100,
            limit_price_usd=17.0,
            reason=ExitReason.HARD_STOP_KRW,
            rationale="원화 손절",
        )
        text = format_decision(d, bar, pos, cfg, dry_run=False)
        assert "청산" in text
        assert "전량 청산" in text
        assert "원화 하드스톱" in text

    def test_모든_행동이_포매팅된다(self, cfg: StrategyConfig) -> None:
        bar = make_bar(close=20.0, fx=1400.0)
        pos = open_position(qty=100, price=20.0, fx=1400.0, atr=1.0)
        cases = [
            Decision(Action.SCALE_IN, qty=35, limit_price_usd=19.3, rationale="2차"),
            Decision(
                Action.TAKE_PROFIT,
                qty=30,
                limit_price_usd=21.5,
                reason=ExitReason.TAKE_PROFIT_LADDER,
                rationale="익절",
            ),
            Decision(
                Action.EXIT,
                qty=100,
                limit_price_usd=18.0,
                reason=ExitReason.TIME_STOP,
                rationale="타임스톱",
            ),
        ]
        for d in cases:
            text = format_decision(d, bar, pos, cfg)
            assert text
            assert cfg.symbol in text

    def test_메시지가_텔레그램_상한_안이다(self, cfg: StrategyConfig) -> None:
        bar = make_bar(close=20.0, fx=1400.0)
        pos = open_position(qty=100, price=20.0, fx=1400.0, atr=1.0)
        d = Decision(
            Action.EXIT,
            qty=100,
            limit_price_usd=18.0,
            reason=ExitReason.TIME_STOP,
            rationale="근" * 3000,
        )
        assert len(format_decision(d, bar, pos, cfg)) < MAX_TEXT


class TestOtherFormats:
    def test_차단_알림(self, cfg: StrategyConfig) -> None:
        text = format_blocked("연속 손실 3회로 한도에 도달했다", cfg)
        assert "매매 차단" in text
        assert "연속 손실" in text
        assert "청산은 계속 동작한다" in text

    def test_오류_알림(self, cfg: StrategyConfig) -> None:
        text = format_error(ValueError("뭔가 터졌다"), context="watch")
        assert "봇 오류" in text
        assert "ValueError" in text
        assert "watch" in text

    def test_마감_요약(self, cfg: StrategyConfig) -> None:
        text = format_daily_summary(
            {"realized_krw_today": -50_000, "entries_today": 2, "position_qty": 40},
            cfg,
            now=dt.datetime(2026, 8, 26),
        )
        assert "마감" in text
        assert "-50,000원" in text


class TestRunnerIntegration:
    """러너가 알림을 보내되, 알림이 죽어도 매매는 계속되는지."""

    class _Boom:
        """항상 폭발하는 알림기."""

        enabled = True

        def send(self, text: str, *, dedupe_key: str | None = None):
            raise RuntimeError("알림 서버 폭발")

    class _Spy:
        enabled = True

        def __init__(self) -> None:
            self.sent: list[str] = []

        def send(self, text: str, *, dedupe_key: str | None = None) -> NotifyResult:
            self.sent.append(text)
            return NotifyResult.sent()

    @staticmethod
    def _runner(bars, cfg, notifier):
        from koru_trade.broker.paper import PaperBroker
        from koru_trade.live import LiveRunner, StateStore
        from koru_trade.models import Quote

        last = bars[-1]
        broker = PaperBroker(
            lambda s: Quote(
                s, last.close, last.close * 0.999, last.close * 1.001, last.ts, last.fx_rate
            ),
            initial_cash_usd=1_000_000.0,
            fill_immediately=True,
        )
        return LiveRunner(broker, StateStore(":memory:"), cfg, notifier=notifier)

    def test_진입_신호에_알림이_간다(self, loose_cfg: StrategyConfig) -> None:
        from tests.conftest import make_series

        bars = make_series(160, drift=0.003, amplitude=0.01)
        spy = self._Spy()
        result = self._runner(bars, loose_cfg, spy).tick(bars)
        assert result.decision.action is Action.ENTER
        assert spy.sent
        assert "매수 신호" in spy.sent[0]

    def test_알림기가_터져도_주문은_나간다(self, loose_cfg: StrategyConfig) -> None:
        """이 테스트가 없으면 알림 장애가 조용히 매매 장애가 된다."""
        from tests.conftest import make_series

        bars = make_series(160, drift=0.003, amplitude=0.01)
        result = self._runner(bars, loose_cfg, self._Boom()).tick(bars)
        assert result.decision.action is Action.ENTER
        assert result.acted, "알림 예외 때문에 주문이 막혔다"

    def test_HOLD에는_알림이_가지_않는다(self, cfg: StrategyConfig) -> None:
        """매일 '아무것도 안 함' 알림이 오면 아무도 안 읽게 된다."""
        from tests.conftest import make_series

        bars = make_series(160, drift=-0.004)
        spy = self._Spy()
        result = self._runner(bars, cfg, spy).tick(bars)
        assert result.decision.action is Action.HOLD
        assert not spy.sent
