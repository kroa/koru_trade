"""브로커 계층 검증.

두 가지를 집중적으로 본다.

1. **멱등성** — 같은 주문이 두 번 나가면 안 된다. 자동매매의 가장 비싼 사고다.
2. **비밀정보 비유출** — 예외 메시지나 로그에 앱키/계좌번호가 실리면 안 된다.
   GitHub 공개 저장소이므로 이건 협상 대상이 아니다.

네트워크는 ``responses`` 로 모킹한다. 실제 KIS 서버를 부르는 테스트는 없다.
"""

from __future__ import annotations

import datetime as dt
import json
import logging

import pytest
import requests
import responses

from koru_trade.broker.base import BrokerError, OrderStatus, TransientBrokerError
from koru_trade.broker.kis import (
    EXCHANGE_ORDER_CODE,
    EXCHANGE_QUOTE_CODE,
    PATH_BALANCE,
    PATH_HASHKEY,
    PATH_ORDER,
    PATH_PRICE,
    PATH_PSAMOUNT,
    PATH_TOKEN,
    RATE_LIMIT_ERROR_CODE,
    TR_BUY_LIVE,
    TR_BUY_PAPER,
    TR_SELL_LIVE,
    TR_SELL_PAPER,
    KisBroker,
    _redact,
    _to_float,
)
from koru_trade.broker.paper import PaperBroker
from koru_trade.broker.ratelimit import RateLimiter, retry_with_backoff
from koru_trade.config import Credentials, KisEnv
from koru_trade.models import Order, OrderSide, OrderType, Quote

APP_KEY = "PSFAKEKEYFAKEKEYFAKEKEYFAKEKEY000000"
APP_SECRET = "SECRETFAKEFAKEFAKE" * 8
ACCOUNT = "12345678"


@pytest.fixture
def cred() -> Credentials:
    return Credentials(
        app_key=APP_KEY,
        app_secret=APP_SECRET,
        account_no=ACCOUNT,
        account_product_code="01",
        env=KisEnv.PAPER,
    )


@pytest.fixture
def broker(cred: Credentials) -> KisBroker:
    return KisBroker(cred, exchange="AMS", dry_run=False, rate_limit=1000)


def _mock_token(base: str) -> None:
    responses.add(
        responses.POST,
        f"{base}{PATH_TOKEN}",
        json={"access_token": "TOKEN123", "expires_in": 86400, "token_type": "Bearer"},
        status=200,
    )


def _mock_balance(base: str, *, qty: int = 0, fx: float = 1400.0) -> None:
    responses.add(
        responses.GET,
        f"{base}{PATH_BALANCE}",
        json={
            "rt_cd": "0",
            "msg1": "정상처리",
            "output1": [
                {
                    "ovrs_pdno": "KORU",
                    "ovrs_cblc_qty": str(qty),
                    "pchs_avg_pric": "20.1234",
                    "ovrs_stck_evlu_amt": str(qty * 21.0),
                }
            ],
            "output2": {"frst_bltn_exrt": str(fx), "frcr_dncl_amt_2": "5000.00"},
        },
        status=200,
    )


class TestCredentials:
    def test_repr에_원문이_노출되지_않는다(self, cred: Credentials) -> None:
        """디버깅 중 print(cred) 한 줄이 키를 유출하는 것을 막는다."""
        text = repr(cred)
        assert APP_KEY not in text
        assert APP_SECRET not in text
        assert ACCOUNT not in text
        assert "..." in text

    def test_str도_마스킹된다(self, cred: Credentials) -> None:
        assert APP_SECRET not in str(cred)

    def test_f문자열_보간에도_노출되지_않는다(self, cred: Credentials) -> None:
        assert APP_SECRET not in f"{cred}"

    def test_누락된_항목은_거부된다(self) -> None:
        with pytest.raises(ValueError, match="자격증명 누락"):
            Credentials("", APP_SECRET, ACCOUNT, "01")

    def test_숫자가_아닌_계좌번호는_거부된다(self) -> None:
        with pytest.raises(ValueError, match="숫자"):
            Credentials(APP_KEY, APP_SECRET, "ABC12345", "01")

    def test_2자리가_아닌_상품코드는_거부된다(self) -> None:
        with pytest.raises(ValueError, match="2자리"):
            Credentials(APP_KEY, APP_SECRET, ACCOUNT, "001")

    def test_환경에_따라_도메인이_달라진다(self) -> None:
        paper = Credentials(APP_KEY, APP_SECRET, ACCOUNT, "01", KisEnv.PAPER)
        live = Credentials(APP_KEY, APP_SECRET, ACCOUNT, "01", KisEnv.LIVE)
        assert "openapivts" in paper.base_url
        assert "openapivts" not in live.base_url
        assert live.is_live and not paper.is_live


class TestRedact:
    def test_민감_필드가_지워진다(self) -> None:
        out = _redact({"CANO": ACCOUNT, "appkey": APP_KEY, "PDNO": "KORU"})
        assert out["CANO"] == "***"
        assert out["appkey"] == "***"
        assert out["PDNO"] == "KORU"

    def test_중첩_구조도_처리된다(self) -> None:
        out = _redact({"body": [{"ACNT_PRDT_CD": "01", "qty": 10}]})
        assert out["body"][0]["ACNT_PRDT_CD"] == "***"
        assert out["body"][0]["qty"] == 10

    def test_대소문자를_가리지_않는다(self) -> None:
        assert _redact({"AppSecret": APP_SECRET})["AppSecret"] == "***"


class TestExchangeCodes:
    def test_시세코드와_주문코드가_다르다(self) -> None:
        """KIS 연동에서 가장 흔한 버그. 명시적으로 못 박는다."""
        assert EXCHANGE_QUOTE_CODE["ARCA"] == "AMS"
        assert EXCHANGE_ORDER_CODE["ARCA"] == "AMEX"
        assert EXCHANGE_QUOTE_CODE["NASDAQ"] == "NAS"
        assert EXCHANGE_ORDER_CODE["NASDAQ"] == "NASD"


class TestToFloat:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("1,234.56", 1234.56), ("", 0.0), (None, 0.0), ("abc", 0.0), (12, 12.0)],
    )
    def test_문자열_숫자를_안전하게_변환한다(self, raw: object, expected: float) -> None:
        assert _to_float(raw) == pytest.approx(expected)


class TestKisAuth:
    @responses.activate
    def test_토큰이_캐시되어_재사용된다(self, broker: KisBroker, cred: Credentials) -> None:
        """KIS 는 토큰 재발급 빈도를 제한한다. 매번 새로 받으면 차단된다."""
        _mock_token(cred.base_url)
        _mock_balance(cred.base_url)
        broker.get_balance("KORU")
        broker.get_balance("KORU")
        token_calls = [c for c in responses.calls if PATH_TOKEN in c.request.url]
        assert len(token_calls) == 1

    @responses.activate
    def test_토큰_발급_실패_메시지에_키가_없다(self, broker: KisBroker, cred: Credentials) -> None:
        responses.add(
            responses.POST,
            f"{cred.base_url}{PATH_TOKEN}",
            json={"error_description": f"invalid appkey {APP_KEY}"},
            status=403,
        )
        with pytest.raises(BrokerError) as exc:
            broker.get_balance("KORU")
        assert APP_KEY not in str(exc.value)
        assert APP_SECRET not in str(exc.value)
        assert "HTTP 403" in str(exc.value)

    @responses.activate
    def test_토큰_응답에_access_token이_없으면_오류다(
        self, broker: KisBroker, cred: Credentials
    ) -> None:
        responses.add(responses.POST, f"{cred.base_url}{PATH_TOKEN}", json={"x": 1}, status=200)
        with pytest.raises(BrokerError, match="access_token"):
            broker.get_balance("KORU")


class TestKisQuote:
    @responses.activate
    def test_현재가를_조회한다(self, broker: KisBroker, cred: Credentials) -> None:
        _mock_token(cred.base_url)
        _mock_balance(cred.base_url, fx=1385.5)
        responses.add(
            responses.GET,
            f"{cred.base_url}{PATH_PRICE}",
            json={"rt_cd": "0", "output": {"last": "20.36", "tvol": "1000000"}},
            status=200,
        )
        q = broker.get_quote("KORU")
        assert q.last == pytest.approx(20.36)
        assert q.fx_rate == pytest.approx(1385.5)

    @responses.activate
    def test_현재가가_0이면_오류다(self, broker: KisBroker, cred: Credentials) -> None:
        _mock_token(cred.base_url)
        responses.add(
            responses.GET,
            f"{cred.base_url}{PATH_PRICE}",
            json={"rt_cd": "0", "output": {"last": "0"}},
            status=200,
        )
        with pytest.raises(BrokerError, match="현재가"):
            broker.get_quote("KORU")

    @responses.activate
    def test_업무오류_코드를_예외로_변환한다(self, broker: KisBroker, cred: Credentials) -> None:
        _mock_token(cred.base_url)
        responses.add(
            responses.GET,
            f"{cred.base_url}{PATH_PRICE}",
            json={"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "조회 권한이 없습니다"},
            status=200,
        )
        with pytest.raises(BrokerError, match="EGW00123"):
            broker.get_quote("KORU")

    @responses.activate
    def test_5xx는_일시적_오류로_분류된다(self, broker: KisBroker, cred: Credentials) -> None:
        _mock_token(cred.base_url)
        responses.add(responses.GET, f"{cred.base_url}{PATH_BALANCE}", json={}, status=503)
        with pytest.raises(TransientBrokerError, match="503"):
            broker.get_fx_rate()

    @responses.activate
    def test_환율을_못_찾으면_추정하지_않고_실패한다(
        self, broker: KisBroker, cred: Credentials
    ) -> None:
        """환율을 모른 채 원화 손익을 계산하면 전부 틀린다. 조용히 넘어가면 안 된다."""
        _mock_token(cred.base_url)
        responses.add(
            responses.GET,
            f"{cred.base_url}{PATH_BALANCE}",
            json={"rt_cd": "0", "output1": [], "output2": {}},
            status=200,
        )
        with pytest.raises(BrokerError, match="환율"):
            broker.get_fx_rate()


class TestKisOrders:
    @responses.activate
    def test_드라이런에서는_주문API가_호출되지_않는다(self, cred: Credentials) -> None:
        dry = KisBroker(cred, dry_run=True, rate_limit=1000)
        order = Order("cid-1", "KORU", OrderSide.BUY, 10, OrderType.LIMIT, 20.5)
        result = dry.submit(order)
        assert result.status is OrderStatus.DRY_RUN
        assert not responses.calls  # 네트워크를 전혀 안 썼다

    @responses.activate
    def test_같은_멱등키로는_두_번_나가지_않는다(
        self, broker: KisBroker, cred: Credentials
    ) -> None:
        _mock_token(cred.base_url)
        responses.add(
            responses.POST, f"{cred.base_url}{PATH_HASHKEY}", json={"HASH": "H"}, status=200
        )
        responses.add(
            responses.POST,
            f"{cred.base_url}{PATH_ORDER}",
            json={"rt_cd": "0", "msg1": "정상", "output": {"ODNO": "0000123"}},
            status=200,
        )
        order = Order("cid-dup", "KORU", OrderSide.BUY, 10, OrderType.LIMIT, 20.5)
        first = broker.submit(order)
        second = broker.submit(order)
        assert first.status is OrderStatus.ACCEPTED
        assert second.status is OrderStatus.DUPLICATE
        order_calls = [c for c in responses.calls if c.request.url.endswith(PATH_ORDER)]
        assert len(order_calls) == 1

    @responses.activate
    def test_주문_바디에_올바른_거래소코드가_들어간다(
        self, broker: KisBroker, cred: Credentials
    ) -> None:
        _mock_token(cred.base_url)
        responses.add(
            responses.POST, f"{cred.base_url}{PATH_HASHKEY}", json={"HASH": "H"}, status=200
        )
        responses.add(
            responses.POST,
            f"{cred.base_url}{PATH_ORDER}",
            json={"rt_cd": "0", "msg1": "정상", "output": {"ODNO": "1"}},
            status=200,
        )
        broker.submit(Order("cid-x", "KORU", OrderSide.BUY, 7, OrderType.LIMIT, 20.5))
        call = next(c for c in responses.calls if c.request.url.endswith(PATH_ORDER))
        body = json.loads(call.request.body)
        assert body["OVRS_EXCG_CD"] == "AMEX"  # 시세코드 AMS 가 아니다
        assert body["ORD_QTY"] == "7"
        assert body["OVRS_ORD_UNPR"] == "20.50"

    @responses.activate
    def test_매도주문에_판매유형이_붙는다(self, broker: KisBroker, cred: Credentials) -> None:
        _mock_token(cred.base_url)
        responses.add(
            responses.POST, f"{cred.base_url}{PATH_HASHKEY}", json={"HASH": "H"}, status=200
        )
        responses.add(
            responses.POST,
            f"{cred.base_url}{PATH_ORDER}",
            json={"rt_cd": "0", "msg1": "정상", "output": {"ODNO": "1"}},
            status=200,
        )
        broker.submit(Order("cid-s", "KORU", OrderSide.SELL, 5, OrderType.LIMIT, 21.0))
        call = next(c for c in responses.calls if c.request.url.endswith(PATH_ORDER))
        assert json.loads(call.request.body)["SLL_TYPE"] == "00"

    @responses.activate
    def test_주문_거부는_예외가_아니라_결과로_돌아온다(
        self, broker: KisBroker, cred: Credentials
    ) -> None:
        """주문 실패로 봇 전체가 죽으면 안 된다. 다음 틱에 다시 판단해야 한다."""
        _mock_token(cred.base_url)
        responses.add(
            responses.POST, f"{cred.base_url}{PATH_HASHKEY}", json={"HASH": "H"}, status=200
        )
        responses.add(
            responses.POST,
            f"{cred.base_url}{PATH_ORDER}",
            json={"rt_cd": "1", "msg_cd": "40570000", "msg1": "주문가능금액 부족"},
            status=200,
        )
        result = broker.submit(Order("cid-r", "KORU", OrderSide.BUY, 10, OrderType.LIMIT, 20.5))
        assert result.status is OrderStatus.REJECTED
        assert "주문가능금액" in result.message

    @responses.activate
    def test_로그에_계좌번호가_남지_않는다(
        self, broker: KisBroker, cred: Credentials, caplog: pytest.LogCaptureFixture
    ) -> None:
        _mock_token(cred.base_url)
        responses.add(
            responses.POST, f"{cred.base_url}{PATH_HASHKEY}", json={"HASH": "H"}, status=200
        )
        responses.add(
            responses.POST,
            f"{cred.base_url}{PATH_ORDER}",
            json={"rt_cd": "0", "msg1": "정상", "output": {"ODNO": "1"}},
            status=200,
        )
        with caplog.at_level(logging.DEBUG):
            broker.submit(Order("cid-l", "KORU", OrderSide.BUY, 10, OrderType.LIMIT, 20.5))
        text = caplog.text
        assert ACCOUNT not in text
        assert APP_KEY not in text
        assert APP_SECRET not in text


class TestRateLimiter:
    def test_한도_내에서는_즉시_통과한다(self) -> None:
        limiter = RateLimiter(5, 1.0)
        for _ in range(5):
            limiter.acquire(timeout=0.1)
        assert limiter.current_usage == 5

    def test_한도를_넘으면_타임아웃한다(self) -> None:
        limiter = RateLimiter(1, 10.0)
        limiter.acquire()
        with pytest.raises(TimeoutError, match="슬롯"):
            limiter.acquire(timeout=0.05)

    @pytest.mark.parametrize(("calls", "period"), [(0, 1.0), (5, 0.0), (5, -1.0)])
    def test_잘못된_설정은_거부된다(self, calls: int, period: float) -> None:
        with pytest.raises(ValueError):
            RateLimiter(calls, period)


class TestRetry:
    def test_성공하면_바로_반환한다(self) -> None:
        calls = []

        def fn() -> str:
            calls.append(1)
            return "ok"

        assert retry_with_backoff(fn, retries=3, sleep=lambda _: None) == "ok"
        assert len(calls) == 1

    def test_실패하면_지정_횟수만큼_재시도한다(self) -> None:
        calls = []
        delays: list[float] = []

        def fn() -> str:
            calls.append(1)
            raise ValueError("실패")

        with pytest.raises(ValueError):
            retry_with_backoff(fn, retries=3, retry_on=(ValueError,), sleep=delays.append)
        assert len(calls) == 4  # 최초 1회 + 재시도 3회
        assert len(delays) == 3
        assert delays[0] < delays[1] < delays[2]  # 지수 백오프

    def test_대상이_아닌_예외는_재시도하지_않는다(self) -> None:
        calls = []

        def fn() -> str:
            calls.append(1)
            raise TypeError("다른 예외")

        with pytest.raises(TypeError):
            retry_with_backoff(fn, retries=3, retry_on=(ValueError,), sleep=lambda _: None)
        assert len(calls) == 1

    def test_음수_재시도는_거부된다(self) -> None:
        with pytest.raises(ValueError, match="retries"):
            retry_with_backoff(lambda: 1, retries=-1)


class TestPaperBroker:
    @staticmethod
    def _quote(symbol: str) -> Quote:
        return Quote(symbol, 20.0, 19.98, 20.02, dt.datetime(2026, 8, 24), 1400.0)

    def test_매수하면_현금이_줄고_포지션이_생긴다(self) -> None:
        pb = PaperBroker(self._quote, initial_cash_usd=1000.0)
        result = pb.submit(Order("c1", "KORU", "BUY", 10, OrderType.MARKET))
        assert result.status is OrderStatus.ACCEPTED
        assert pb.cash_usd == pytest.approx(1000.0 - 10 * 20.02)
        assert pb.get_balance("KORU").qty == 10

    def test_예수금이_모자라면_거부된다(self) -> None:
        pb = PaperBroker(self._quote, initial_cash_usd=10.0)
        result = pb.submit(Order("c1", "KORU", "BUY", 10, OrderType.MARKET))
        assert result.status is OrderStatus.REJECTED
        assert "예수금 부족" in result.message

    def test_보유보다_많이_팔면_거부된다(self) -> None:
        pb = PaperBroker(self._quote, initial_cash_usd=1000.0)
        pb.submit(Order("c1", "KORU", "BUY", 5, OrderType.MARKET))
        result = pb.submit(Order("c2", "KORU", "SELL", 10, OrderType.MARKET))
        assert result.status is OrderStatus.REJECTED

    def test_멱등키가_같으면_중복으로_처리된다(self) -> None:
        pb = PaperBroker(self._quote, initial_cash_usd=1000.0)
        pb.submit(Order("same", "KORU", "BUY", 5, OrderType.MARKET))
        again = pb.submit(Order("same", "KORU", "BUY", 5, OrderType.MARKET))
        assert again.status is OrderStatus.DUPLICATE
        assert pb.get_balance("KORU").qty == 5

    def test_지정가가_안_닿으면_체결되지_않는다(self) -> None:
        pb = PaperBroker(self._quote, initial_cash_usd=1000.0)
        result = pb.submit(Order("c1", "KORU", "BUY", 5, OrderType.LIMIT, 19.0))
        assert result.status is OrderStatus.REJECTED
        assert "체결되지 않는다" in result.message

    def test_지정가가_닿으면_체결된다(self) -> None:
        pb = PaperBroker(self._quote, initial_cash_usd=1000.0)
        result = pb.submit(Order("c1", "KORU", "BUY", 5, OrderType.LIMIT, 21.0))
        assert result.status is OrderStatus.ACCEPTED

    def test_평단이_수량가중으로_갱신된다(self) -> None:
        pb = PaperBroker(self._quote, initial_cash_usd=10_000.0, fill_immediately=True)
        pb.submit(Order("c1", "KORU", "BUY", 10, OrderType.LIMIT, 20.0))
        pb.submit(Order("c2", "KORU", "BUY", 10, OrderType.LIMIT, 22.0))
        assert pb.get_balance("KORU").avg_price_usd == pytest.approx(21.0)

    def test_전량_매도하면_포지션이_사라진다(self) -> None:
        pb = PaperBroker(self._quote, initial_cash_usd=1000.0)
        pb.submit(Order("c1", "KORU", "BUY", 5, OrderType.MARKET))
        pb.submit(Order("c2", "KORU", "SELL", 5, OrderType.MARKET))
        assert pb.get_balance("KORU").qty == 0

    def test_체결_이력이_기록된다(self) -> None:
        pb = PaperBroker(self._quote, initial_cash_usd=1000.0)
        pb.submit(Order("c1", "KORU", "BUY", 5, OrderType.MARKET))
        assert len(pb.fills) == 1
        assert pb.fills[0].side is OrderSide.BUY

    def test_항상_드라이런이다(self) -> None:
        assert PaperBroker(self._quote).dry_run

    def test_문자열_side가_열거형으로_정규화된다(self) -> None:
        """JSON 등에서 문자열로 들어와도 `is` 비교가 성립해야 한다."""
        order = Order("c1", "KORU", "BUY", 5, "MARKET")
        assert order.side is OrderSide.BUY
        assert order.order_type is OrderType.MARKET

    def test_잘못된_side_문자열은_거부된다(self) -> None:
        with pytest.raises(ValueError, match="BUYY"):
            Order("c1", "KORU", "BUYY", 5, OrderType.MARKET)

    def test_취소는_지원하지_않는다(self) -> None:
        pb = PaperBroker(self._quote)
        assert pb.cancel("x").status is OrderStatus.REJECTED


class TestTimeoutHandling:
    @responses.activate
    def test_타임아웃은_일시적_오류다(self, broker: KisBroker, cred: Credentials) -> None:
        _mock_token(cred.base_url)
        responses.add(responses.GET, f"{cred.base_url}{PATH_BALANCE}", body=requests.Timeout())
        with pytest.raises(TransientBrokerError, match="타임아웃"):
            broker.get_balance("KORU")


class TestVerifiedTrIds:
    """KIS 공식 소스와 대조 검증한 값들. 바뀌면 즉시 알아야 한다."""

    def test_모의투자_매도_TR_ID가_비대칭이다(self) -> None:
        """이 프로젝트에서 가장 잡기 어려운 함정.

        매수는 TTTT1002U -> VTTT1002U 로 첫 글자만 바뀌지만,
        매도는 TTTT1006U -> **VTTT1001U** 다. VTTT1006U 가 아니다.
        KIS 공식 샘플의 `tr_id = "V" + tr_id[1:]` 일괄 치환 규칙을 그대로 쓰면
        존재하지 않는 TR_ID 를 보내게 된다.
        """
        assert TR_BUY_LIVE == "TTTT1002U"
        assert TR_BUY_PAPER == "VTTT1002U"
        assert TR_SELL_LIVE == "TTTT1006U"
        assert TR_SELL_PAPER == "VTTT1001U"
        assert "V" + TR_SELL_LIVE[1:] != TR_SELL_PAPER  # 일괄 치환 규칙이 틀린다

    def test_KORU는_AMS_아멕스로_분류된다(self) -> None:
        """KIS 종목마스터 AMSMST.COD 에 KORU 가 있고 NYSMST/NASMST 에는 없다."""
        assert EXCHANGE_QUOTE_CODE["ARCA"] == "AMS"
        assert EXCHANGE_ORDER_CODE["AMS"] == "AMEX"

    def test_브로커_기본_거래소가_KORU에_맞다(self, cred: Credentials) -> None:
        broker = KisBroker(cred, rate_limit=1000)
        assert broker._quote_exchange == "AMS"
        assert broker._order_exchange == "AMEX"


class TestAdaptiveThrottle:
    @responses.activate
    def test_유량초과를_받으면_한도를_스스로_낮춘다(
        self, broker: KisBroker, cred: Credentials
    ) -> None:
        """KIS 는 모의투자 초당 한도를 숫자로 고지하지 않는다.

        따라서 상수를 믿지 말고, 실제로 거부당하면 그때 감속해야 한다.
        """
        _mock_token(cred.base_url)
        responses.add(
            responses.GET,
            f"{cred.base_url}{PATH_BALANCE}",
            json={"rt_cd": "1", "msg_cd": RATE_LIMIT_ERROR_CODE, "msg1": "초당 거래건수 초과"},
            status=200,
        )
        before = broker._rate_limit
        with pytest.raises(TransientBrokerError, match="한도 초과"):
            broker.get_balance("KORU")
        assert broker._rate_limit < before

    @responses.activate
    def test_한도는_1_미만으로_내려가지_않는다(self, cred: Credentials) -> None:
        broker = KisBroker(cred, dry_run=False, rate_limit=1)
        _mock_token(cred.base_url)
        responses.add(
            responses.GET,
            f"{cred.base_url}{PATH_BALANCE}",
            json={"rt_cd": "1", "msg_cd": RATE_LIMIT_ERROR_CODE, "msg1": "초과"},
            status=200,
        )
        with pytest.raises(TransientBrokerError):
            broker.get_balance("KORU")
        assert broker._rate_limit == 1


class TestBuyingPower:
    @responses.activate
    def test_주문가능금액을_브로커에_직접_물어본다(
        self, broker: KisBroker, cred: Credentials
    ) -> None:
        """로컬 추정 금지. 미결제·환전대기 때문에 반드시 어긋난다."""
        _mock_token(cred.base_url)
        responses.add(
            responses.GET,
            f"{cred.base_url}{PATH_PSAMOUNT}",
            json={"rt_cd": "0", "output": {"ord_psbl_frcr_amt": "1,234.56"}},
            status=200,
        )
        assert broker.buying_power_usd("KORU", 20.5) == pytest.approx(1234.56)


class TestOrderBody:
    @responses.activate
    def test_매수에는_SLL_TYPE이_빈_문자열이다(self, broker: KisBroker, cred: Credentials) -> None:
        _mock_token(cred.base_url)
        responses.add(
            responses.POST, f"{cred.base_url}{PATH_HASHKEY}", json={"HASH": "H"}, status=200
        )
        responses.add(
            responses.POST,
            f"{cred.base_url}{PATH_ORDER}",
            json={"rt_cd": "0", "msg1": "정상", "output": {"ODNO": "1"}},
            status=200,
        )
        broker.submit(Order("cid-b", "KORU", OrderSide.BUY, 3, OrderType.LIMIT, 20.5))
        call = next(c for c in responses.calls if c.request.url.endswith(PATH_ORDER))
        assert json.loads(call.request.body)["SLL_TYPE"] == ""

    @responses.activate
    def test_시장가는_한정된_폭의_지정가로_변환된다(
        self, broker: KisBroker, cred: Credentials
    ) -> None:
        """ORD_DVSN 시장가 코드의 체결 방식이 문서에 불충분해 지정가로 변환한다.

        3배 레버리지에서는 예측 불가능한 체결가가 곧 손실 증폭이므로,
        최악의 체결가가 사전에 한정되는 편이 안전하다.
        """
        _mock_token(cred.base_url)
        responses.add(
            responses.GET,
            f"{cred.base_url}{PATH_PRICE}",
            json={"rt_cd": "0", "output": {"last": "20.00"}},
            status=200,
        )
        _mock_balance(cred.base_url)
        responses.add(
            responses.POST, f"{cred.base_url}{PATH_HASHKEY}", json={"HASH": "H"}, status=200
        )
        responses.add(
            responses.POST,
            f"{cred.base_url}{PATH_ORDER}",
            json={"rt_cd": "0", "msg1": "정상", "output": {"ODNO": "1"}},
            status=200,
        )
        broker.submit(Order("cid-m", "KORU", OrderSide.SELL, 3, OrderType.MARKET))
        call = next(c for c in responses.calls if c.request.url.endswith(PATH_ORDER))
        body = json.loads(call.request.body)
        assert body["ORD_DVSN"] == "00"  # 지정가
        assert float(body["OVRS_ORD_UNPR"]) == pytest.approx(19.40)  # 20.00 x 0.97
