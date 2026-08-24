"""한국투자증권(KIS) Open API 해외주식 클라이언트.

보안 규약 (GitHub 공개 저장소이므로 반드시 지킬 것)
---------------------------------------------------
* 앱키/앱시크릿/계좌번호는 :class:`~koru_trade.config.Credentials` 를 통해서만 들어온다.
* :meth:`KisBroker._request` 는 예외 메시지에 **요청 헤더와 바디를 절대 넣지 않는다.**
  요청 실패 로그에 헤더를 찍는 습관이 API 키 유출의 가장 흔한 경로다.
* 응답 로깅은 계좌번호가 담긴 필드를 제거한 뒤에 한다 (:func:`_redact`).
* 토큰은 메모리에만 둔다. 파일로 저장하지 않는다.

DRY_RUN
-------
``dry_run=True`` 이면 조회 API 는 정상 호출하되 **주문 API 는 절대 호출하지 않고**
:attr:`~koru_trade.broker.base.OrderStatus.DRY_RUN` 을 반환한다.
기본값이 True 이며, 끄려면 환경변수 ``KORU_DRY_RUN=false`` 를 명시해야 한다.

엔드포인트 표
-------------
아래 상수는 KIS API 문서 기준이다. 증권사가 규격을 바꾸면 여기만 고치면 된다.
**실전 계좌로 돌리기 전에 반드시 모의투자에서 각 TR_ID 를 검증하라.**
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
from typing import Any

import requests

from koru_trade.broker.base import (
    BalanceSnapshot,
    BrokerError,
    OrderResult,
    OrderStatus,
    TransientBrokerError,
)
from koru_trade.broker.ratelimit import RateLimiter, retry_with_backoff
from koru_trade.config import Credentials
from koru_trade.models import Order, OrderSide, OrderType, Quote

logger = logging.getLogger(__name__)

__all__ = ["EXCHANGE_ORDER_CODE", "EXCHANGE_QUOTE_CODE", "KisBroker"]

# ---------------------------------------------------------------------------
# 엔드포인트
# ---------------------------------------------------------------------------
PATH_TOKEN = "/oauth2/tokenP"  # noqa: S105 - 엔드포인트 경로이지 비밀값이 아니다
PATH_REVOKE = "/oauth2/revokeP"
PATH_HASHKEY = "/uapi/hashkey"
PATH_PRICE = "/uapi/overseas-price/v1/quotations/price"
PATH_DAILY = "/uapi/overseas-price/v1/quotations/dailyprice"
PATH_ORDER = "/uapi/overseas-stock/v1/trading/order"
PATH_ORDER_RVSECNCL = "/uapi/overseas-stock/v1/trading/order-rvsecncl"
PATH_NCCS = "/uapi/overseas-stock/v1/trading/inquire-nccs"
PATH_BALANCE = "/uapi/overseas-stock/v1/trading/inquire-balance"
PATH_PRESENT_BALANCE = "/uapi/overseas-stock/v1/trading/inquire-present-balance"
PATH_PSAMOUNT = "/uapi/overseas-stock/v1/trading/inquire-psamount"

# ---------------------------------------------------------------------------
# TR_ID 표
#
# 아래 값은 KIS 공식 저장소(koreainvestment/open-trading-api)의
# overseas_stock_functions / kis_ovs.py 원문과 대조 검증했다.
#
# 함정 두 가지:
#
# 1. 시세 조회의 거래소 코드(NAS/NYS/AMS)와 주문의 거래소 코드(NASD/NYSE/AMEX)가
#    서로 다르다. 이 연동에서 가장 흔한 버그다.
# 2. **모의투자 TR_ID 는 매수/매도가 비대칭이다.**
#    매수는 TTTT1002U -> VTTT1002U 로 첫 글자만 바뀌지만,
#    매도는 TTTT1006U -> VTTT1001U 다 (VTTT1006U 가 아니다).
#    KIS 공식 샘플 코드의 `tr_id = "V" + tr_id[1:]` 일괄 치환 규칙은
#    이 지점에서 잘못된 값을 만든다. 그래서 여기서는 표로 하드코딩한다.
# ---------------------------------------------------------------------------
TR_PRICE = "HHDFS00000300"
TR_DAILY = "HHDFS76240000"
TR_MINUTE = "HHDFS76950200"

TR_BUY_LIVE = "TTTT1002U"
TR_SELL_LIVE = "TTTT1006U"
TR_BUY_PAPER = "VTTT1002U"
TR_SELL_PAPER = "VTTT1001U"  # VTTT1006U 가 아니다. 위 주석 2번 참조

TR_RVSECNCL_LIVE = "TTTT1004U"
TR_RVSECNCL_PAPER = "VTTT1004U"

TR_NCCS_LIVE = "TTTS3018R"
TR_NCCS_PAPER = "VTTS3018R"

TR_BALANCE_LIVE = "TTTS3012R"
TR_BALANCE_PAPER = "VTTS3012R"

TR_PSAMOUNT_LIVE = "TTTS3007R"
TR_PSAMOUNT_PAPER = "VTTS3007R"

RATE_LIMIT_ERROR_CODE = "EGW00201"
"""초당 거래건수 초과 오류 코드.

KIS 는 모의투자 계좌의 초당 호출 한도를 **숫자로 고지하지 않는다**
(공식 문서에는 "모의투자 계좌는 REST API 호출 제한이 낮습니다" 라는 서술만 있다).
2차 자료들은 1건/2건/5건으로 서로 어긋난다. 따라서 특정 숫자를 신뢰하지 말고
이 코드를 받으면 자동으로 감속하는 적응형 쓰로틀링을 쓴다.
"""

EXCHANGE_QUOTE_CODE = {
    "NASDAQ": "NAS",
    "NYSE": "NYS",
    "AMEX": "AMS",
    "ARCA": "AMS",
    # KORU 는 NYSE Arca 상장이지만 KIS 종목마스터(AMSMST.COD)에 'AMS 아멕스' 로 등록되어
    # 있다. NYSMST/NASMST 에는 KORU 가 없다. (NASDAQ 의 KORU 는 티커 KRMD 인 별개 종목.)
    # NYSE Arca ETF 는 대체로 AMS 로 분류된다: SPY/SOXL/ARKK/IWM -> AMS, QQQ/TQQQ -> NAS.
    "NAS": "NAS",
    "NYS": "NYS",
    "AMS": "AMS",
}
"""시세 조회용 거래소 코드."""

EXCHANGE_ORDER_CODE = {
    "NAS": "NASD",
    "NYS": "NYSE",
    "AMS": "AMEX",
    "NASDAQ": "NASD",
    "NYSE": "NYSE",
    "AMEX": "AMEX",
    "ARCA": "AMEX",
}
"""주문용 거래소 코드. 시세 코드와 다르다."""

_SENSITIVE_KEYS = {
    "cano",
    "acnt_prdt_cd",
    "appkey",
    "appsecret",
    "authorization",
    "access_token",
    "hashkey",
    "hash",
    "personalseckey",
}


def _redact(payload: Any) -> Any:
    """로그에 남기기 전에 민감 필드를 지운다.

    >>> _redact({"CANO": "12345678", "PDNO": "KORU"})
    {'CANO': '***', 'PDNO': 'KORU'}
    """
    if isinstance(payload, dict):
        return {
            k: ("***" if k.lower() in _SENSITIVE_KEYS else _redact(v)) for k, v in payload.items()
        }
    if isinstance(payload, list):
        return [_redact(v) for v in payload]
    return payload


class KisBroker:
    """한국투자증권 해외주식 REST 클라이언트.

    Args:
        credentials: 자격증명.
        exchange: 거래소 코드(``AMS``/``NAS``/``NYS`` 또는 이름).
        dry_run: True 면 주문 API 를 호출하지 않는다. **기본 True.**
        timeout: HTTP 타임아웃(초).
        rate_limit: 초당 최대 호출 수. 실전 계좌가 모의보다 넉넉하다.
        session: 테스트용 ``requests.Session`` 주입구.
    """

    def __init__(
        self,
        credentials: Credentials,
        *,
        exchange: str = "AMS",
        dry_run: bool = True,
        timeout: float = 10.0,
        rate_limit: int | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self._cred = credentials
        self._quote_exchange = EXCHANGE_QUOTE_CODE.get(exchange.upper(), "AMS")
        self._order_exchange = EXCHANGE_ORDER_CODE.get(exchange.upper(), "AMEX")
        self._dry_run = dry_run
        self._timeout = timeout
        self._session = session or requests.Session()
        self._token: str | None = None
        self._token_expires_at: dt.datetime | None = None
        self._lock = threading.Lock()
        self._submitted: dict[str, OrderResult] = {}
        # 실전 초당 20건은 복수 출처가 일치한다. 여유를 두어 18로 잡는다.
        # 모의투자 한도는 KIS 가 숫자로 고지하지 않으므로 보수적으로 2에서 시작하고,
        # EGW00201 을 받으면 _throttle_down() 이 자동으로 더 낮춘다.
        self._rate_limit = (
            rate_limit if rate_limit is not None else (18 if credentials.is_live else 2)
        )
        self._limiter = RateLimiter(self._rate_limit, 1.0)

        if dry_run:
            logger.warning(
                "DRY_RUN 모드다. 시세는 조회하지만 주문은 나가지 않는다. "
                "실거래를 하려면 KORU_DRY_RUN=false 를 설정하라"
            )
        elif credentials.is_live:
            logger.warning("실전투자 환경에서 DRY_RUN 이 꺼져 있다. 실제 자금이 움직인다")

    # -- 속성 ---------------------------------------------------------------

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @property
    def is_live(self) -> bool:
        return self._cred.is_live

    # -- 인증 ---------------------------------------------------------------

    def _access_token(self) -> str:
        """접근 토큰을 반환한다. 만료가 임박하면 갱신한다.

        KIS 는 토큰 재발급 호출 빈도를 제한하므로 반드시 캐시해야 한다.
        만료 60초 전에 미리 갱신한다.
        """
        with self._lock:
            now = dt.datetime.now(dt.UTC)
            if (
                self._token
                and self._token_expires_at
                and now < self._token_expires_at - dt.timedelta(seconds=60)
            ):
                return self._token

            self._limiter.acquire()
            resp = self._session.post(
                f"{self._cred.base_url}{PATH_TOKEN}",
                json={
                    "grant_type": "client_credentials",
                    "appkey": self._cred.app_key,
                    "appsecret": self._cred.app_secret,
                },
                headers={"content-type": "application/json"},
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                # 본문에 키가 반사되어 실릴 수 있으므로 상태 코드만 남긴다.
                raise BrokerError(
                    f"토큰 발급 실패 (HTTP {resp.status_code}). "
                    f"앱키/앱시크릿과 KIS_ENV 설정을 확인하라"
                )
            data = resp.json()
            token = data.get("access_token")
            if not token:
                raise BrokerError("토큰 응답에 access_token 이 없다")
            expires_in = int(data.get("expires_in", 86400))
            self._token = str(token)
            self._token_expires_at = now + dt.timedelta(seconds=expires_in)
            logger.info("접근 토큰을 발급했다 (유효 %d초)", expires_in)
            return self._token

    def _headers(self, tr_id: str, *, hashkey: str | None = None) -> dict[str, str]:
        """요청 헤더. 이 사전은 절대 로그에 찍지 않는다."""
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._access_token()}",
            "appkey": self._cred.app_key,
            "appsecret": self._cred.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }
        if hashkey:
            headers["hashkey"] = hashkey
        return headers

    def _hashkey(self, body: dict[str, Any]) -> str:
        """주문 바디의 해시키를 발급받는다. KIS 가 주문 위변조 검증에 쓴다."""
        self._limiter.acquire()
        resp = self._session.post(
            f"{self._cred.base_url}{PATH_HASHKEY}",
            data=json.dumps(body),
            headers={
                "content-type": "application/json; charset=utf-8",
                "appkey": self._cred.app_key,
                "appsecret": self._cred.app_secret,
            },
            timeout=self._timeout,
        )
        if resp.status_code != 200:
            raise BrokerError(f"해시키 발급 실패 (HTTP {resp.status_code})")
        value = resp.json().get("HASH")
        if not value:
            raise BrokerError("해시키 응답에 HASH 가 없다")
        return str(value)

    # -- HTTP ---------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        tr_id: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        use_hashkey: bool = False,
    ) -> dict[str, Any]:
        """KIS API 를 호출하고 응답 본문을 반환한다.

        Raises:
            TransientBrokerError: 타임아웃 또는 5xx.
            BrokerError: 그 외 실패. **메시지에 자격증명을 담지 않는다.**
        """
        hashkey = self._hashkey(body) if (use_hashkey and body) else None
        headers = self._headers(tr_id, hashkey=hashkey)
        url = f"{self._cred.base_url}{path}"

        self._limiter.acquire()
        try:
            resp = self._session.request(
                method,
                url,
                params=params,
                data=json.dumps(body) if body is not None else None,
                headers=headers,
                timeout=self._timeout,
            )
        except requests.Timeout as exc:
            raise TransientBrokerError(f"{path} 호출 타임아웃({self._timeout}초)") from exc
        except requests.RequestException as exc:
            raise TransientBrokerError(
                f"{path} 호출 중 네트워크 오류: {type(exc).__name__}"
            ) from exc

        if resp.status_code >= 500:
            raise TransientBrokerError(f"{path} 서버 오류 (HTTP {resp.status_code})")
        if resp.status_code != 200:
            raise BrokerError(f"{path} 호출 실패 (HTTP {resp.status_code}, tr_id={tr_id})")

        try:
            data: dict[str, Any] = resp.json()
        except ValueError as exc:
            raise BrokerError(f"{path} 응답이 JSON 이 아니다") from exc

        if str(data.get("rt_cd", "0")) != "0":
            msg = data.get("msg1", "").strip()
            code = str(data.get("msg_cd", ""))
            if code == RATE_LIMIT_ERROR_CODE:
                # 유량 초과. 한도를 스스로 낮추고 일시적 오류로 올려 재시도되게 한다.
                self._throttle_down()
                raise TransientBrokerError(
                    f"{path} 초당 호출 한도 초과 [{code}]. 호출 간격을 늘렸다"
                )
            raise BrokerError(f"{path} 업무 오류 [{code}] {msg}")

        logger.debug("%s 응답: %s", path, _redact(data))
        return data

    def _throttle_down(self) -> None:
        """유량 초과를 만나면 초당 호출 한도를 절반으로 줄인다(최소 1).

        KIS 는 모의투자 한도를 숫자로 고지하지 않고 2차 자료도 서로 어긋난다.
        따라서 특정 숫자를 믿는 대신, 실제로 거부당하면 그때 줄이는 방식을 쓴다.
        한 번 줄인 한도는 프로세스가 살아 있는 동안 유지된다.
        """
        new_limit = max(1, self._rate_limit // 2)
        if new_limit == self._rate_limit:
            return
        self._rate_limit = new_limit
        self._limiter = RateLimiter(new_limit, 1.0)
        logger.warning(
            "%s 를 받아 초당 호출 한도를 %d 건으로 낮췄다", RATE_LIMIT_ERROR_CODE, new_limit
        )

    # -- 시세 ---------------------------------------------------------------

    def get_quote(self, symbol: str) -> Quote:
        """현재가를 조회한다.

        KIS 해외주식 현재가 응답에는 호가가 없으므로,
        ``bid``/``ask`` 는 현재가로 채운다. 호가 스프레드 필터를 쓰려면
        별도의 호가 API 또는 실시간 웹소켓을 붙여야 한다.
        """
        data = retry_with_backoff(
            lambda: self._request(
                "GET",
                PATH_PRICE,
                TR_PRICE,
                params={"AUTH": "", "EXCD": self._quote_exchange, "SYMB": symbol},
            ),
            retries=2,
            retry_on=(TransientBrokerError,),
        )
        out = data.get("output") or {}
        last = _to_float(out.get("last"))
        if last <= 0:
            raise BrokerError(f"{symbol} 현재가를 받지 못했다(장 시작 전이거나 코드 오류)")
        return Quote(
            symbol=symbol,
            last=last,
            bid=last,
            ask=last,
            ts=dt.datetime.now(),
            fx_rate=self.get_fx_rate(),
        )

    def get_fx_rate(self) -> float:
        """USD/KRW 매매기준율을 조회한다.

        KIS 잔고 조회 응답의 환율 필드를 쓴다. 조회에 실패하면 예외를 올린다.
        환율을 모르는 채로 원화 기준 손익을 계산하면 전부 틀리므로,
        **추정값으로 대체하지 않는다.**
        """
        data = self._request(
            "GET",
            PATH_BALANCE,
            TR_BALANCE_LIVE if self.is_live else TR_BALANCE_PAPER,
            params=self._balance_params(),
        )
        out2 = data.get("output2") or {}
        if isinstance(out2, list):
            out2 = out2[0] if out2 else {}
        rate = _to_float(out2.get("frst_bltn_exrt")) or _to_float(out2.get("exrt"))
        if rate <= 0:
            raise BrokerError(
                "잔고 응답에서 환율을 찾지 못했다. "
                "필드명(frst_bltn_exrt/exrt)이 바뀌었는지 확인하라"
            )
        return rate

    # -- 잔고 ---------------------------------------------------------------

    def _balance_params(self) -> dict[str, str]:
        return {
            "CANO": self._cred.account_no,
            "ACNT_PRDT_CD": self._cred.account_product_code,
            "OVRS_EXCG_CD": self._order_exchange,
            "TR_CRCY_CD": "USD",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }

    def get_balance(self, symbol: str) -> BalanceSnapshot:
        """보유 잔고를 조회한다."""
        data = self._request(
            "GET",
            PATH_BALANCE,
            TR_BALANCE_LIVE if self.is_live else TR_BALANCE_PAPER,
            params=self._balance_params(),
        )
        rows = data.get("output1") or []
        summary = data.get("output2") or {}
        if isinstance(summary, list):
            summary = summary[0] if summary else {}

        qty = 0
        avg = 0.0
        evaluated = 0.0
        for row in rows:
            if str(row.get("ovrs_pdno", "")).upper() != symbol.upper():
                continue
            qty = int(_to_float(row.get("ovrs_cblc_qty")))
            avg = _to_float(row.get("pchs_avg_pric"))
            evaluated = _to_float(row.get("ovrs_stck_evlu_amt"))
            break

        return BalanceSnapshot(
            symbol=symbol,
            qty=qty,
            avg_price_usd=avg,
            eval_amount_usd=evaluated,
            cash_usd=_to_float(summary.get("frcr_dncl_amt_2"))
            or _to_float(summary.get("frcr_evlu_tota")),
            fx_rate=_to_float(summary.get("frst_bltn_exrt")) or _to_float(summary.get("exrt")),
        )

    # -- 주문 ---------------------------------------------------------------

    def submit(self, order: Order) -> OrderResult:
        """주문을 제출한다.

        같은 ``client_order_id`` 로 이미 제출된 주문이 있으면
        :attr:`OrderStatus.DUPLICATE` 를 반환하고 API 를 호출하지 않는다.
        네트워크 타임아웃 뒤 재시도해도 이중 체결이 나지 않게 하기 위함이다.

        **주문 제출은 자동 재시도하지 않는다.** 타임아웃이 났을 때
        주문이 나갔는지 알 수 없으므로, 재시도 대신 미체결 조회로 확인해야 한다.
        """
        prior = self._submitted.get(order.client_order_id)
        if prior is not None:
            logger.warning(
                "멱등키 %s 로 이미 제출된 주문이 있다. 새로 내지 않는다",
                order.client_order_id,
            )
            return OrderResult(
                status=OrderStatus.DUPLICATE,
                client_order_id=order.client_order_id,
                broker_order_id=prior.broker_order_id,
                message="같은 멱등키의 주문이 이미 제출되었다",
            )

        if self._dry_run:
            result = OrderResult(
                status=OrderStatus.DRY_RUN,
                client_order_id=order.client_order_id,
                message=(
                    f"[DRY_RUN] {order.side.value} {order.qty}주 "
                    f"@ {order.limit_price_usd or 'MARKET'} — {order.reason}"
                ),
                submitted_at=dt.datetime.now(),
            )
            self._submitted[order.client_order_id] = result
            logger.info("%s", result.message)
            return result

        body = self._order_body(order)
        tr_id = self._order_tr_id(order.side)
        try:
            data = self._request("POST", PATH_ORDER, tr_id, body=body, use_hashkey=True)
        except BrokerError as exc:
            logger.error("주문 제출 실패: %s", exc)
            return OrderResult(
                status=OrderStatus.REJECTED,
                client_order_id=order.client_order_id,
                message=str(exc),
                submitted_at=dt.datetime.now(),
            )

        out = data.get("output") or {}
        result = OrderResult(
            status=OrderStatus.ACCEPTED,
            client_order_id=order.client_order_id,
            broker_order_id=str(out.get("ODNO", "")),
            message=str(data.get("msg1", "")).strip(),
            submitted_at=dt.datetime.now(),
        )
        self._submitted[order.client_order_id] = result
        logger.info(
            "주문 접수: %s %d주 @ %s (주문번호 %s)",
            order.side.value,
            order.qty,
            order.limit_price_usd,
            result.broker_order_id,
        )
        return result

    def _order_tr_id(self, side: OrderSide) -> str:
        if self.is_live:
            return TR_BUY_LIVE if side is OrderSide.BUY else TR_SELL_LIVE
        return TR_BUY_PAPER if side is OrderSide.BUY else TR_SELL_PAPER

    def _order_body(self, order: Order) -> dict[str, str]:
        """주문 바디를 만든다.

        KIS 미국주식은 ``ORD_DVSN`` 으로 지정가(00) 외에 몇 가지 시장가류 코드를
        지원하지만(매수 32/34, 매도 31/32/33/34), 각 코드의 정확한 체결 방식이
        공식 문서에 충분히 명시되어 있지 않다. 잘못된 코드로 체결되면
        3배 레버리지 상품에서는 손실이 즉시 증폭된다.

        그래서 :attr:`OrderType.MARKET` 은 **현재가 대비 3% 유리한 방향으로 민
        지정가**(매수 +3%, 매도 -3%)로 변환해 낸다. 동작이 명확하고 최악의
        체결가가 사전에 한정된다. 손절 시 이 폭 안에서 체결되지 않을 만큼
        가격이 갭으로 뛰면 다음 틱에서 다시 판단한다.
        """
        price = order.limit_price_usd
        if order.order_type is OrderType.MARKET or price is None:
            quote = self.get_quote(order.symbol)
            slip = 1.03 if order.side is OrderSide.BUY else 0.97
            price = round(quote.last * slip, 2)

        body = {
            "CANO": self._cred.account_no,
            "ACNT_PRDT_CD": self._cred.account_product_code,
            "OVRS_EXCG_CD": self._order_exchange,
            "PDNO": order.symbol,
            "ORD_QTY": str(order.qty),
            "OVRS_ORD_UNPR": f"{price:.2f}",
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00",
        }
        # KIS 공식 샘플은 매수에 SLL_TYPE="", 매도에 "00" 을 넣는다. 그대로 따른다.
        body["SLL_TYPE"] = "00" if order.side is OrderSide.SELL else ""
        return body

    def cancel(self, client_order_id: str) -> OrderResult:
        """제출한 주문을 취소한다."""
        prior = self._submitted.get(client_order_id)
        if prior is None or not prior.broker_order_id:
            return OrderResult(
                status=OrderStatus.REJECTED,
                client_order_id=client_order_id,
                message="취소할 주문번호를 찾을 수 없다",
            )
        if self._dry_run:
            return OrderResult(
                status=OrderStatus.DRY_RUN,
                client_order_id=client_order_id,
                message="[DRY_RUN] 취소 요청은 실제로 나가지 않았다",
            )

        body = {
            "CANO": self._cred.account_no,
            "ACNT_PRDT_CD": self._cred.account_product_code,
            "OVRS_EXCG_CD": self._order_exchange,
            "PDNO": "",
            "ORGN_ODNO": prior.broker_order_id,
            "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": "0",
            "OVRS_ORD_UNPR": "0",
        }
        tr_id = TR_RVSECNCL_LIVE if self.is_live else TR_RVSECNCL_PAPER
        data = self._request("POST", PATH_ORDER_RVSECNCL, tr_id, body=body, use_hashkey=True)
        return OrderResult(
            status=OrderStatus.ACCEPTED,
            client_order_id=client_order_id,
            broker_order_id=str((data.get("output") or {}).get("ODNO", "")),
            message=str(data.get("msg1", "")).strip(),
        )

    def buying_power_usd(self, symbol: str, price_usd: float) -> float:
        """주문 가능 금액(USD)을 브로커에 직접 물어본다.

        로컬에서 "현금 - 이미 낸 주문" 을 계산해 추정하면 미결제 금액, 환전 대기,
        증거금 규칙 때문에 실제와 어긋난다. 어긋난 채로 주문하면 거부되거나,
        더 나쁘게는 의도보다 많이 체결된다. **추정하지 말고 물어본다.**

        Args:
            symbol: 종목 코드.
            price_usd: 주문 예정 단가(KIS 가 이 가격 기준으로 가능 수량을 계산한다).

        Returns:
            주문 가능 외화 금액(USD).
        """
        data = self._request(
            "GET",
            PATH_PSAMOUNT,
            TR_PSAMOUNT_LIVE if self.is_live else TR_PSAMOUNT_PAPER,
            params={
                "CANO": self._cred.account_no,
                "ACNT_PRDT_CD": self._cred.account_product_code,
                "OVRS_EXCG_CD": self._order_exchange,
                "OVRS_ORD_UNPR": f"{price_usd:.2f}",
                "ITEM_CD": symbol,
            },
        )
        out = data.get("output") or {}
        if isinstance(out, list):
            out = out[0] if out else {}
        return _to_float(out.get("ord_psbl_frcr_amt")) or _to_float(out.get("frcr_ord_psbl_amt1"))

    def open_orders(self) -> list[dict[str, Any]]:
        """미체결 주문 목록. 타임아웃 뒤 "주문이 나갔나" 를 확인하는 용도다."""
        data = self._request(
            "GET",
            PATH_NCCS,
            TR_NCCS_LIVE if self.is_live else TR_NCCS_PAPER,
            params={
                "CANO": self._cred.account_no,
                "ACNT_PRDT_CD": self._cred.account_product_code,
                "OVRS_EXCG_CD": self._order_exchange,
                "SORT_SQN": "DS",
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            },
        )
        rows = data.get("output") or []
        return list(rows) if isinstance(rows, list) else []


def _to_float(value: Any) -> float:
    """KIS 응답의 문자열 숫자를 float 로. 빈 값이나 이상한 값은 0.0."""
    if value is None:
        return 0.0
    try:
        text = str(value).strip().replace(",", "")
        return float(text) if text else 0.0
    except (TypeError, ValueError):
        return 0.0
