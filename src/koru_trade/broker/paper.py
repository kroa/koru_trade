"""페이퍼 브로커 — 실제 주문 없이 실거래 코드 경로를 그대로 돌린다.

용도가 두 가지다.

1. **테스트**: 네트워크 없이 :mod:`koru_trade.live.runner` 전체를 검증한다.
2. **실전 리허설**: 실제 시세(:class:`~koru_trade.broker.kis.KisBroker`)를 주입하고
   체결만 가상으로 처리해, 실거래 직전 며칠간 돌려보며 로그를 확인한다.

체결 모델은 낙관적이지 않다. 지정가는 현재가가 그 가격을 통과해야 체결된다.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable

from koru_trade.broker.base import BalanceSnapshot, OrderResult, OrderStatus
from koru_trade.models import Fill, Order, OrderSide, OrderType, Quote

logger = logging.getLogger(__name__)

__all__ = ["PaperBroker"]


class PaperBroker:
    """가상 체결 브로커.

    Args:
        quote_source: 심볼을 받아 :class:`~koru_trade.models.Quote` 를 반환하는 함수.
            실전 리허설이면 :meth:`KisBroker.get_quote` 를 그대로 넘긴다.
        initial_cash_usd: 초기 달러 예수금.
        fill_immediately: True 면 지정가를 현재가로 즉시 체결한다(단위 테스트용).
            False 면 지정가 조건을 충족해야 체결한다.
    """

    def __init__(
        self,
        quote_source: Callable[[str], Quote],
        *,
        initial_cash_usd: float = 10_000.0,
        fill_immediately: bool = False,
    ) -> None:
        self._quote_source = quote_source
        self._cash_usd = initial_cash_usd
        self._fill_immediately = fill_immediately
        self._positions: dict[str, tuple[int, float]] = {}
        self._submitted: dict[str, OrderResult] = {}
        self.fills: list[Fill] = []
        self._seq = 0

    @property
    def dry_run(self) -> bool:
        """페이퍼 브로커는 항상 가상이다."""
        return True

    @property
    def cash_usd(self) -> float:
        return self._cash_usd

    def get_quote(self, symbol: str) -> Quote:
        return self._quote_source(symbol)

    def get_fx_rate(self) -> float:
        return self._quote_source("USDKRW").fx_rate

    def get_balance(self, symbol: str) -> BalanceSnapshot:
        qty, avg = self._positions.get(symbol, (0, 0.0))
        quote = self._quote_source(symbol)
        return BalanceSnapshot(
            symbol=symbol,
            qty=qty,
            avg_price_usd=avg,
            eval_amount_usd=qty * quote.last,
            cash_usd=self._cash_usd,
            fx_rate=quote.fx_rate,
        )

    def submit(self, order: Order) -> OrderResult:
        """가상 주문을 처리한다. 멱등키 규약은 실브로커와 동일하다."""
        prior = self._submitted.get(order.client_order_id)
        if prior is not None:
            return OrderResult(
                status=OrderStatus.DUPLICATE,
                client_order_id=order.client_order_id,
                broker_order_id=prior.broker_order_id,
                message="같은 멱등키의 주문이 이미 제출되었다",
            )

        quote = self._quote_source(order.symbol)
        price = self._fill_price(order, quote)
        if price is None:
            result = OrderResult(
                status=OrderStatus.REJECTED,
                client_order_id=order.client_order_id,
                message=(
                    f"지정가 {order.limit_price_usd} 는 현재가 {quote.last} 로 체결되지 않는다"
                ),
                submitted_at=dt.datetime.now(),
            )
            self._submitted[order.client_order_id] = result
            return result

        if order.side is OrderSide.BUY:
            cost = order.qty * price
            if cost > self._cash_usd:
                result = OrderResult(
                    status=OrderStatus.REJECTED,
                    client_order_id=order.client_order_id,
                    message=f"예수금 부족: 필요 ${cost:,.2f} / 보유 ${self._cash_usd:,.2f}",
                )
                self._submitted[order.client_order_id] = result
                return result
            self._cash_usd -= cost
            qty0, avg0 = self._positions.get(order.symbol, (0, 0.0))
            new_qty = qty0 + order.qty
            self._positions[order.symbol] = (
                new_qty,
                (qty0 * avg0 + order.qty * price) / new_qty,
            )
        else:
            qty0, avg0 = self._positions.get(order.symbol, (0, 0.0))
            if order.qty > qty0:
                result = OrderResult(
                    status=OrderStatus.REJECTED,
                    client_order_id=order.client_order_id,
                    message=f"보유({qty0}주)보다 많이 매도할 수 없다: {order.qty}주",
                )
                self._submitted[order.client_order_id] = result
                return result
            self._cash_usd += order.qty * price
            left = qty0 - order.qty
            if left:
                self._positions[order.symbol] = (left, avg0)
            else:
                self._positions.pop(order.symbol, None)

        self._seq += 1
        broker_id = f"PAPER{self._seq:06d}"
        self.fills.append(
            Fill(
                client_order_id=order.client_order_id,
                broker_order_id=broker_id,
                symbol=order.symbol,
                side=order.side,
                qty=order.qty,
                price_usd=price,
                fx_rate=quote.fx_rate,
                fee_krw=0.0,
                ts=quote.ts,
            )
        )
        result = OrderResult(
            status=OrderStatus.ACCEPTED,
            client_order_id=order.client_order_id,
            broker_order_id=broker_id,
            message=f"가상 체결 {order.side.value} {order.qty}주 @ ${price:.2f}",
            submitted_at=quote.ts,
        )
        self._submitted[order.client_order_id] = result
        logger.info("%s", result.message)
        return result

    def cancel(self, client_order_id: str) -> OrderResult:
        """가상 브로커는 즉시 체결하므로 취소할 미체결이 없다."""
        return OrderResult(
            status=OrderStatus.REJECTED,
            client_order_id=client_order_id,
            message="페이퍼 브로커는 즉시 체결하므로 취소 대상이 없다",
        )

    def _fill_price(self, order: Order, quote: Quote) -> float | None:
        """체결가를 결정한다. 체결되지 않으면 None."""
        if order.order_type is OrderType.MARKET:
            return quote.ask if order.side is OrderSide.BUY else quote.bid
        limit = order.limit_price_usd
        if limit is None:  # pragma: no cover - Order 가 생성 시점에 막는다
            return None
        if self._fill_immediately:
            return limit
        if order.side is OrderSide.BUY:
            return min(limit, quote.ask) if quote.ask <= limit else None
        return max(limit, quote.bid) if quote.bid >= limit else None
