"""브로커 인터페이스.

전략과 실행을 분리한다. 실거래(:class:`~koru_trade.broker.kis.KisBroker`)와
모의 실행(:class:`~koru_trade.broker.paper.PaperBroker`)이 같은 프로토콜을 구현하므로,
실거래로 넘어갈 때 바꾸는 것은 객체 하나뿐이다.

멱등성 규약
-----------
:meth:`Broker.submit` 은 같은 ``client_order_id`` 로 두 번 호출되어도
주문을 두 번 내지 않는다. 네트워크 타임아웃 뒤 재시도할 때
"이미 나갔는지 모르겠는" 상황이 자동매매에서 가장 비싼 사고이기 때문이다.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from koru_trade.models import Order, Quote

__all__ = ["Broker", "BrokerError", "OrderResult", "OrderStatus", "TransientBrokerError"]


class BrokerError(RuntimeError):
    """브로커 호출 실패. 메시지에 자격증명이 실리지 않도록 주의해서 만든다."""


class TransientBrokerError(BrokerError):
    """재시도하면 성공할 수 있는 일시적 오류(타임아웃, 5xx, 유량 초과)."""


class OrderStatus(str, Enum):
    """주문 상태."""

    ACCEPTED = "ACCEPTED"
    """접수됨(체결 여부는 별도 조회)."""

    REJECTED = "REJECTED"
    """거부됨."""

    DUPLICATE = "DUPLICATE"
    """같은 멱등키로 이미 제출된 주문이라 새로 내지 않았다."""

    DRY_RUN = "DRY_RUN"
    """드라이런 모드라 실제로 내지 않았다."""


@dataclass(frozen=True, slots=True)
class OrderResult:
    """주문 제출 결과."""

    status: OrderStatus
    client_order_id: str
    broker_order_id: str = ""
    message: str = ""
    submitted_at: dt.datetime | None = None

    @property
    def is_live_order(self) -> bool:
        """실제로 거래소에 나간 주문인지."""
        return self.status is OrderStatus.ACCEPTED


@dataclass(frozen=True, slots=True)
class BalanceSnapshot:
    """계좌 잔고 스냅샷.

    민감정보(계좌번호)는 담지 않는다. 수량과 금액만 담는다.
    """

    symbol: str
    qty: int
    avg_price_usd: float
    eval_amount_usd: float
    cash_usd: float
    fx_rate: float

    @property
    def eval_amount_krw(self) -> float:
        return self.eval_amount_usd * self.fx_rate


@runtime_checkable
class Broker(Protocol):
    """브로커가 제공해야 하는 최소 인터페이스."""

    def get_quote(self, symbol: str) -> Quote:
        """현재 시세를 조회한다."""
        ...

    def get_fx_rate(self) -> float:
        """USD/KRW 매매기준율을 조회한다."""
        ...

    def submit(self, order: Order) -> OrderResult:
        """주문을 제출한다. 같은 ``client_order_id`` 는 한 번만 나간다."""
        ...

    def cancel(self, client_order_id: str) -> OrderResult:
        """미체결 주문을 취소한다."""
        ...

    def get_balance(self, symbol: str) -> BalanceSnapshot:
        """보유 잔고를 조회한다."""
        ...
