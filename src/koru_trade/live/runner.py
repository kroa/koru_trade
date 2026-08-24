"""실거래 실행 루프.

한 번의 :meth:`LiveRunner.tick` 이 하는 일은 다음과 같다.

1. 상태 저장소에서 포지션과 리스크 상태를 복구한다.
2. 시세와 환율을 조회해 최신 봉을 만든다.
3. :func:`~koru_trade.strategy.decide` 로 판단한다 (백테스트와 **완전히 같은 함수**).
4. 결정을 주문으로 바꿔 브로커에 제출한다.
5. 체결 결과로 포지션을 갱신하고 저장한다.
6. 판단 근거를 감사 로그에 남긴다.

멱등성
------
주문 ID 는 ``{심볼}-{행동}-{타임스탬프}-{수량}`` 의 해시로 만든다.
같은 봉에서 같은 결정이 반복되면 같은 ID 가 나오므로,
프로세스가 죽었다 살아나 같은 봉을 다시 처리해도 주문이 두 번 나가지 않는다.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace

from koru_trade.broker.base import Broker, OrderResult, OrderStatus
from koru_trade.config import StrategyConfig
from koru_trade.live.state import StateStore
from koru_trade.models import (
    Action,
    Bar,
    Decision,
    Lot,
    Order,
    OrderSide,
    OrderType,
    Position,
)
from koru_trade.pnl import krw_cost, krw_proceeds, position_krw_return
from koru_trade.risk import RiskState
from koru_trade.strategy import decide

logger = logging.getLogger(__name__)

__all__ = ["LiveRunner", "RunnerResult"]


@dataclass(frozen=True, slots=True)
class RunnerResult:
    """1회 틱의 결과."""

    decision: Decision
    order: Order | None
    order_result: OrderResult | None
    position_after: Position
    krw_return: float

    @property
    def acted(self) -> bool:
        return self.order_result is not None and self.order_result.status in (
            OrderStatus.ACCEPTED,
            OrderStatus.DRY_RUN,
        )

    def summary(self) -> str:
        parts = [f"{self.decision.action.value}"]
        if self.decision.qty:
            parts.append(f"{self.decision.qty}주")
        if self.order_result is not None:
            parts.append(f"[{self.order_result.status.value}]")
        parts.append(f"보유 {self.position_after.qty}주")
        parts.append(f"원화수익률 {self.krw_return:+.2%}")
        return " | ".join(parts)


class LiveRunner:
    """전략 -> 주문 -> 상태 저장을 잇는 실행기.

    Args:
        broker: 브로커. 실거래는 :class:`~koru_trade.broker.kis.KisBroker`,
            리허설은 :class:`~koru_trade.broker.paper.PaperBroker`.
        store: 상태 저장소.
        cfg: 전략 설정.
    """

    def __init__(self, broker: Broker, store: StateStore, cfg: StrategyConfig) -> None:
        self._broker = broker
        self._store = store
        self._cfg = cfg

    def tick(self, bars: Sequence[Bar], *, now: dt.datetime | None = None) -> RunnerResult:
        """1회 판단하고 필요하면 주문을 낸다.

        Args:
            bars: 최신 봉을 포함한 시간 오름차순 봉 목록.
                마지막 봉의 종가와 환율이 판단 기준이 된다.
            now: 현재 시각. None 이면 마지막 봉의 시각.

        Returns:
            :class:`RunnerResult`.
        """
        if not bars:
            raise ValueError("봉 데이터가 비어 있다")

        cfg = self._cfg
        last = bars[-1]
        ts = now or last.ts

        position = self._store.load_position(cfg.symbol)
        risk = self._store.load_risk(ts.date())

        decision = decide(bars, position, risk, cfg, now=ts)
        krw_r = (
            position_krw_return(position, last.close, last.fx_rate, cfg.cost)
            if position.is_open
            else 0.0
        )

        self._store.log_decision(
            ts=ts,
            action=decision.action.value,
            qty=decision.qty,
            price_usd=last.close,
            fx_rate=last.fx_rate,
            krw_return=krw_r,
            rationale=decision.rationale,
        )
        logger.info("[%s] %s", decision.action.value, decision.rationale)

        if not decision.is_actionable:
            self._store.save_risk(risk)
            return RunnerResult(decision, None, None, position, krw_r)

        order = self._build_order(decision, last, ts)
        if self._store.has_order(order.client_order_id):
            logger.warning("멱등키 %s 는 이미 처리한 주문이다. 건너뛴다", order.client_order_id)
            return RunnerResult(decision, order, None, position, krw_r)

        result = self._broker.submit(order)
        self._store.record_order(
            order.client_order_id,
            broker_order_id=result.broker_order_id,
            side=order.side.value,
            qty=order.qty,
            limit_price=order.limit_price_usd,
            status=result.status.value,
        )

        if result.status in (OrderStatus.ACCEPTED, OrderStatus.DRY_RUN):
            position, risk = self._apply(
                decision,
                order,
                position,
                risk,
                last,
                ts,
                dry_run=result.status is OrderStatus.DRY_RUN,
            )
            self._store.save_position(position)
        else:
            logger.error("주문이 체결되지 않았다: %s", result.message)

        self._store.save_risk(risk)
        krw_r = (
            position_krw_return(position, last.close, last.fx_rate, cfg.cost)
            if position.is_open
            else 0.0
        )
        return RunnerResult(decision, order, result, position, krw_r)

    # -- 내부 ---------------------------------------------------------------

    def _build_order(self, decision: Decision, bar: Bar, ts: dt.datetime) -> Order:
        """결정을 주문으로 바꾼다."""
        cfg = self._cfg
        is_buy = decision.action in (Action.ENTER, Action.SCALE_IN)
        side = OrderSide.BUY if is_buy else OrderSide.SELL

        use_market = not is_buy and decision.action is Action.EXIT and cfg.use_market_order_on_stop
        order_type = OrderType.MARKET if use_market else OrderType.LIMIT
        limit = decision.limit_price_usd
        if order_type is OrderType.LIMIT and limit is None:
            adj = cfg.limit_slippage_bps / 10_000.0
            limit = round(bar.close * (1.0 - adj), 2)

        return Order(
            client_order_id=_idempotency_key(cfg.symbol, decision.action.value, ts, decision.qty),
            symbol=cfg.symbol,
            side=side,
            qty=decision.qty,
            order_type=order_type,
            limit_price_usd=None if order_type is OrderType.MARKET else limit,
            reason=decision.rationale[:200],
        )

    def _apply(
        self,
        decision: Decision,
        order: Order,
        position: Position,
        risk: RiskState,
        bar: Bar,
        ts: dt.datetime,
        *,
        dry_run: bool,
    ) -> tuple[Position, RiskState]:
        """체결을 가정하고 포지션과 리스크 상태를 갱신한다.

        실제 체결가는 다를 수 있으므로, 다음 틱에서 브로커 잔고와 대조해
        어긋나면 경고를 남긴다 (:meth:`reconcile`).
        """
        from koru_trade.risk import RiskState  # 순환 참조 회피

        assert isinstance(risk, RiskState)
        cfg = self._cfg
        price = order.limit_price_usd or bar.close
        qty = order.qty

        if decision.action in (Action.ENTER, Action.SCALE_IN):
            cost = krw_cost(qty, price, bar.fx_rate, cfg.cost)
            if decision.action is Action.ENTER:
                plan = decision.entry_plan
                position = Position(
                    cfg.symbol,
                    entry_atr_usd=plan.entry_atr_usd if plan else 0.0,
                    planned_tranche_qty=plan.tranche_qty if plan else (qty,),
                )
                risk = risk.with_entry(cost)
            else:
                risk = risk.with_scale_in(cost)
            position = position.with_lot(
                Lot(
                    lot_id=order.client_order_id[:12],
                    qty=qty,
                    price_usd=price,
                    fx_rate=bar.fx_rate,
                    cost_krw=cost,
                    opened_at=ts,
                    tranche_index=position.tranche_count,
                )
            )
            logger.info(
                "%s 체결 반영: %d주 @ $%.2f (원화 %s원)%s",
                decision.action.value,
                qty,
                price,
                f"{cost:,.0f}",
                " [DRY_RUN]" if dry_run else "",
            )
            return position, risk

        # 매도 -----------------------------------------------------------
        proceeds = krw_proceeds(qty, price, bar.fx_rate, cfg.cost)
        new_pos, removed_cost = position.reduced(qty)
        realized = proceeds - removed_cost
        position = replace(new_pos, realized_krw=position.realized_krw + realized)
        if decision.action is Action.TAKE_PROFIT and decision.tp_levels:
            position = replace(
                position,
                tp_levels_hit=frozenset(set(position.tp_levels_hit) | set(decision.tp_levels)),
            )
        closed = not position.is_open
        risk = risk.with_realized(realized, closed_trade=closed)
        if closed:
            if risk.realized_krw_today <= -abs(cfg.risk.daily_loss_limit_krw):
                risk = risk.with_halt(f"당일 손실 {risk.realized_krw_today:,.0f}원으로 한도 도달")
            if risk.consecutive_losses >= cfg.risk.max_consecutive_losses:
                risk = risk.start_cooldown(cfg.risk.loss_cooldown_days)
                logger.warning("연속 손실 한도 도달. %s 까지 쉰다", risk.cooldown_until)
        logger.info(
            "매도 체결 반영: %d주 @ $%.2f, 실현 %s원%s",
            qty,
            price,
            f"{realized:+,.0f}",
            " [DRY_RUN]" if dry_run else "",
        )
        return position, risk

    def reconcile(self) -> str | None:
        """저장된 포지션과 브로커 실제 잔고를 대조한다.

        어긋나면 경고 문자열을 반환한다. 자동으로 고치지 않는다 —
        불일치의 원인(부분 체결, 수동 매매, 미체결)에 따라 대응이 달라야 하므로
        사람이 판단해야 한다.

        Returns:
            불일치 설명. 일치하면 None.
        """
        cfg = self._cfg
        local = self._store.load_position(cfg.symbol)
        try:
            remote = self._broker.get_balance(cfg.symbol)
        except Exception as exc:
            return f"잔고 조회 실패로 대조하지 못했다: {type(exc).__name__}: {exc}"
        if local.qty != remote.qty:
            return (
                f"포지션 불일치: 로컬 {local.qty}주 vs 브로커 {remote.qty}주. 수동 확인이 필요하다"
            )
        return None


def _idempotency_key(symbol: str, action: str, ts: dt.datetime, qty: int) -> str:
    """멱등키를 만든다.

    같은 봉(분 단위로 절삭)에서 같은 행동·수량이면 같은 키가 나온다.
    프로세스 재시작 후 같은 봉을 다시 처리해도 주문이 중복되지 않는다.
    """
    minute = ts.replace(second=0, microsecond=0).isoformat()
    raw = f"{symbol}|{action}|{minute}|{qty}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"KORU-{digest}"
