"""도메인 데이터 모델.

이 모듈에는 매매 로직이 없다. 값 객체만 정의한다.
모든 dataclass 는 ``frozen=True`` 로 불변이며, 잘못된 값은 생성 시점에 거부한다.
백테스트와 실거래가 완전히 동일한 타입을 주고받게 하려는 목적이다.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field, replace
from enum import Enum

__all__ = [
    "Action",
    "Bar",
    "Decision",
    "EntryPlan",
    "ExitReason",
    "Fill",
    "Lot",
    "Order",
    "OrderSide",
    "OrderType",
    "Position",
    "Quote",
    "TradeRecord",
]


class OrderSide(str, Enum):
    """주문 방향."""

    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    """주문 유형.

    3배 레버리지 상품은 호가 스프레드가 벌어지는 순간이 잦으므로
    기본은 LIMIT 이다. MARKET 은 긴급 청산 상황에만 쓴다.
    """

    LIMIT = "LIMIT"
    MARKET = "MARKET"


class Action(str, Enum):
    """전략 엔진이 내릴 수 있는 행동."""

    HOLD = "HOLD"
    """아무것도 하지 않음."""

    ENTER = "ENTER"
    """신규 진입 (1차 분할 매수)."""

    SCALE_IN = "SCALE_IN"
    """기존 포지션에 추가 분할 매수."""

    TAKE_PROFIT = "TAKE_PROFIT"
    """원화 수익률 계단 도달에 따른 분할 익절."""

    EXIT = "EXIT"
    """손절, 타임스톱, 트레일링스톱 등에 의한 청산."""

    BLOCKED = "BLOCKED"
    """리스크 한도 또는 킬스위치에 의해 매매가 차단됨."""


class ExitReason(str, Enum):
    """청산 사유. 감사추적(audit trail)과 성과 귀속에 사용한다."""

    TAKE_PROFIT_LADDER = "TAKE_PROFIT_LADDER"
    HARD_STOP_USD = "HARD_STOP_USD"
    HARD_STOP_KRW = "HARD_STOP_KRW"
    TRAILING_STOP = "TRAILING_STOP"
    BREAKEVEN_STOP = "BREAKEVEN_STOP"
    TREND_BREAK = "TREND_BREAK"
    TIME_STOP = "TIME_STOP"
    REGIME_EXIT = "REGIME_EXIT"
    KILL_SWITCH = "KILL_SWITCH"
    END_OF_BACKTEST = "END_OF_BACKTEST"


@dataclass(frozen=True, slots=True)
class Bar:
    """일봉 또는 분봉 한 개.

    ``fx_rate`` 는 해당 봉 시점의 USD/KRW 매매기준율(중간값)이다.
    환전 스프레드는 여기 포함하지 않는다. 비용 모델이 따로 적용한다.
    """

    ts: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    fx_rate: float

    def __post_init__(self) -> None:
        # NaN 검사를 먼저 한다. NaN 은 모든 비교가 False 라서
        # `value <= 0` 로는 절대 걸리지 않고 그대로 백테스트에 스며든다.
        for name in ("open", "high", "low", "close", "volume", "fx_rate"):
            value: float = getattr(self, name)
            if not math.isfinite(value):
                raise ValueError(f"{name} 이 유한한 수가 아니다: {value}")
        if self.high < self.low:
            raise ValueError(f"high({self.high}) < low({self.low}) 인 봉은 있을 수 없다")
        for name in ("open", "high", "low", "close", "fx_rate"):
            value = getattr(self, name)
            if value <= 0:
                raise ValueError(f"{name} 은 0보다 커야 한다: {value}")
        # 시가·종가가 고저 범위 밖이면 그 봉은 신뢰할 수 없다.
        #
        # high >= low 만 봐서는 부족하다. 실제로 2026-09-08 장중에 시세 제공자가
        # O 20.93 / H 24.54 / L 24.07 / C 24.15 를 준 적이 있다. 시가가 저가보다
        # 15% 낮은데 high >= low 는 만족하므로 그대로 통과했고, 그 결과 시가갭이
        # -10.82% 로 계산돼 진입이 엉뚱한 이유로 차단됐다. 시가는 나흘 전 값이
        # 그대로 복사된 것이었다.
        #
        # 어느 값이 틀렸는지 알 수 없으므로 봉 전체를 버린다. 로더가 이 예외를
        # 잡아 건너뛰고, 버려진 봉이 맨 뒤면 별도로 경고한다.
        #
        # 실측: KORU·EWY·SOXL·TQQQ 3년치 3,008봉에서 범위 이탈 0건이었다.
        # 조정주가 반올림으로 생기는 오차는 없으므로 허용치를 두지 않는다
        # (부동소수 표현 오차만 상대 1e-9 로 흡수한다).
        tol = self.high * 1e-9
        for name in ("open", "close"):
            value = getattr(self, name)
            if value < self.low - tol or value > self.high + tol:
                raise ValueError(
                    f"{name}({value}) 이 저가({self.low})~고가({self.high}) 범위 밖이다. "
                    "시세 제공자 오류로 보고 이 봉을 버린다"
                )
        if self.volume < 0:
            raise ValueError(f"volume 은 음수일 수 없다: {self.volume}")

    @property
    def typical(self) -> float:
        """(고가+저가+종가)/3. 체결 근사가로 쓴다."""
        return (self.high + self.low + self.close) / 3.0

    @property
    def krw_close(self) -> float:
        """종가의 원화 환산액(스프레드 미반영, 표시용)."""
        return self.close * self.fx_rate


@dataclass(frozen=True, slots=True)
class Quote:
    """실시간 시세 스냅샷."""

    symbol: str
    last: float
    bid: float
    ask: float
    ts: dt.datetime
    fx_rate: float

    def __post_init__(self) -> None:
        if self.ask < self.bid:
            raise ValueError(f"ask({self.ask}) < bid({self.bid}) 는 비정상 호가다")
        if self.last <= 0 or self.fx_rate <= 0:
            raise ValueError("last/fx_rate 는 0보다 커야 한다")

    @property
    def mid(self) -> float:
        """호가 중간값."""
        return (self.bid + self.ask) / 2.0

    @property
    def spread_pct(self) -> float:
        """호가 스프레드 비율. 유동성 필터에 쓴다."""
        mid = self.mid
        return (self.ask - self.bid) / mid if mid > 0 else 0.0


@dataclass(frozen=True, slots=True)
class Lot:
    """분할 매수 1회분(차수)의 체결 결과.

    ``cost_krw`` 는 이 랏을 사기 위해 실제로 빠져나간 원화 총액이다.
    거래수수료와 환전 스프레드가 이미 포함되어 있다.
    """

    lot_id: str
    qty: int
    price_usd: float
    fx_rate: float
    cost_krw: float
    opened_at: dt.datetime
    tranche_index: int

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError(f"랏 수량은 1 이상이어야 한다: {self.qty}")
        if self.price_usd <= 0 or self.fx_rate <= 0 or self.cost_krw <= 0:
            raise ValueError("price_usd/fx_rate/cost_krw 는 0보다 커야 한다")
        if self.tranche_index < 0:
            raise ValueError(f"tranche_index 는 0 이상이어야 한다: {self.tranche_index}")

    @property
    def krw_unit_cost(self) -> float:
        """주당 원화 매입원가."""
        return self.cost_krw / self.qty


@dataclass(frozen=True, slots=True)
class Position:
    """보유 포지션. 여러 :class:`Lot` 의 집합이다.

    분할 매도 시 원가는 이동평균(비례 배분)으로 차감한다.
    국내 증권사의 해외주식 평균단가 회계와 일치시키기 위함이다.
    """

    symbol: str
    lots: tuple[Lot, ...] = ()
    realized_krw: float = 0.0
    """이미 실현된 원화 손익 누계(수수료 차감 후)."""

    tp_levels_hit: frozenset[int] = field(default_factory=frozenset)
    """이미 체결된 익절 계단의 인덱스 집합. 같은 계단 중복 실행을 막는다."""

    peak_krw_return: float = 0.0
    """보유 중 기록한 원화 수익률 최고치. 트레일링 스톱 기준."""

    ladder_base_qty: int = 0
    """익절 계단의 매도 비율을 곱할 기준 수량.

    분할 매수가 진행될 때마다 갱신되며, 첫 익절이 체결된 뒤로는 고정된다
    (첫 익절 이후에는 추가 매수가 금지되므로 자연히 고정된다).
    "잔여 수량의 30%" 가 아니라 "기준 수량의 30%" 로 정의해야
    계단마다 실제 매도량이 예측 가능하고 테스트로 검증할 수 있다.
    """

    entry_atr_usd: float = 0.0
    """1차 진입 시점의 ATR(USD). 분할매수 트리거와 손절폭의 고정 기준.

    매 봉마다 ATR 을 다시 계산해서 쓰면 트리거 가격이 계속 움직여
    "얼마에 추가 매수하는지" 를 사전에 알 수 없게 된다.
    """

    planned_tranche_qty: tuple[int, ...] = ()
    """진입 시점에 계산한 차수별 목표 수량. 인덱스가 곧 차수다."""

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol 은 비어 있을 수 없다")
        if self.ladder_base_qty < 0:
            raise ValueError(f"ladder_base_qty 는 0 이상이어야 한다: {self.ladder_base_qty}")
        if self.entry_atr_usd < 0:
            raise ValueError(f"entry_atr_usd 는 0 이상이어야 한다: {self.entry_atr_usd}")

    @property
    def qty(self) -> int:
        """총 보유 수량."""
        return sum(lot.qty for lot in self.lots)

    @property
    def is_open(self) -> bool:
        return self.qty > 0

    @property
    def cost_krw(self) -> float:
        """잔여 포지션의 원화 총 매입원가(수수료·스프레드 포함)."""
        return sum(lot.cost_krw for lot in self.lots)

    @property
    def avg_krw_unit_cost(self) -> float:
        """주당 원화 평균 매입원가. 포지션이 없으면 0."""
        q = self.qty
        return self.cost_krw / q if q else 0.0

    @property
    def avg_price_usd(self) -> float:
        """USD 기준 평균 체결단가(수량 가중). 포지션이 없으면 0."""
        q = self.qty
        if not q:
            return 0.0
        return sum(lot.qty * lot.price_usd for lot in self.lots) / q

    @property
    def avg_fx_rate(self) -> float:
        """매수 시점 환율의 금액 가중 평균. 환율 손익 귀속 분석에 쓴다."""
        notional = sum(lot.qty * lot.price_usd for lot in self.lots)
        if notional <= 0:
            return 0.0
        return sum(lot.qty * lot.price_usd * lot.fx_rate for lot in self.lots) / notional

    @property
    def tranche_count(self) -> int:
        """지금까지 실행한 분할 매수 차수."""
        return len(self.lots)

    @property
    def opened_at(self) -> dt.datetime | None:
        """최초 진입 시각. 타임스톱 계산의 기준점."""
        return min((lot.opened_at for lot in self.lots), default=None)

    @property
    def first_entry_price_usd(self) -> float:
        """1차 진입 체결가. 손절선은 평단이 아니라 이 값을 기준으로 잡는다.

        평단 기준 손절은 분할매수를 할수록 손절선이 따라 내려가서
        손실 한도가 사실상 무한히 늘어나는 구조적 결함이 있다.
        """
        if not self.lots:
            return 0.0
        return min(self.lots, key=lambda lot: lot.tranche_index).price_usd

    @property
    def remaining_planned_qty(self) -> int:
        """아직 체결하지 않은 계획 수량의 합."""
        planned_total = sum(self.planned_tranche_qty)
        return max(0, planned_total - self.qty - self._sold_qty())

    def _sold_qty(self) -> int:
        """이미 익절로 팔려나간 수량(계획 대비). 계단 진행 상황 추적용."""
        return max(0, self.ladder_base_qty - self.qty)

    def with_lot(self, lot: Lot) -> Position:
        """랏을 추가하고 익절 기준 수량을 갱신한 새 포지션을 반환한다(불변).

        아직 익절이 시작되지 않았을 때에만 기준 수량이 늘어난다.
        """
        new_lots = (*self.lots, lot)
        new_base = self.ladder_base_qty
        if not self.tp_levels_hit:
            new_base = sum(x.qty for x in new_lots)
        return replace(self, lots=new_lots, ladder_base_qty=new_base)

    def reduced(self, sell_qty: int) -> tuple[Position, float]:
        """``sell_qty`` 주를 비례 배분으로 차감한 포지션과 차감된 원가를 반환한다.

        Returns:
            (감소된 포지션, 차감된 원화 매입원가)

        Raises:
            ValueError: 보유 수량보다 많이 팔려고 할 때.
        """
        if sell_qty <= 0:
            raise ValueError(f"매도 수량은 1 이상이어야 한다: {sell_qty}")
        total = self.qty
        if sell_qty > total:
            raise ValueError(f"보유({total})보다 많이 매도할 수 없다: {sell_qty}")

        if sell_qty == total:
            return replace(self, lots=()), self.cost_krw

        remaining_to_sell = sell_qty
        new_lots: list[Lot] = []
        removed_cost = 0.0
        # 각 랏에서 동일 비율로 덜어낸다. 마지막 랏이 반올림 잔차를 흡수한다.
        n = len(self.lots)
        for idx, lot in enumerate(self.lots):
            take = remaining_to_sell if idx == n - 1 else round(sell_qty * lot.qty / total)
            take = max(0, min(int(take), lot.qty, remaining_to_sell))
            if take:
                removed_cost += lot.krw_unit_cost * take
                remaining_to_sell -= take
            left = lot.qty - take
            if left > 0:
                new_lots.append(replace(lot, qty=left, cost_krw=lot.krw_unit_cost * left))

        if remaining_to_sell:  # pragma: no cover - 방어적 검사
            raise AssertionError(f"매도 수량 배분에 실패했다: {remaining_to_sell} 주 남음")
        return replace(self, lots=tuple(new_lots)), removed_cost


@dataclass(frozen=True, slots=True)
class Order:
    """브로커에 제출할 주문.

    ``client_order_id`` 는 멱등키다. 네트워크 타임아웃 후 재시도해도
    같은 키로는 두 번 체결되지 않도록 브로커 레이어가 보장한다.
    """

    client_order_id: str
    symbol: str
    side: OrderSide
    qty: int
    order_type: OrderType
    limit_price_usd: float | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        # 문자열로 들어와도 열거형으로 정규화한다. str 열거형이라 == 는 통과하지만
        # `is` 비교가 조용히 실패해 주문 방향이 무시되는 사고가 난다.
        # (타입 주석상으로는 도달 불가지만, JSON/DB 에서 복원할 때는 실제로 문자열이 온다.)
        if not isinstance(self.side, OrderSide):
            object.__setattr__(self, "side", OrderSide(self.side))  # type: ignore[unreachable]
        if not isinstance(self.order_type, OrderType):
            object.__setattr__(  # type: ignore[unreachable]
                self, "order_type", OrderType(self.order_type)
            )

        if self.qty <= 0:
            raise ValueError(f"주문 수량은 1 이상이어야 한다: {self.qty}")
        if self.order_type is OrderType.LIMIT and not self.limit_price_usd:
            raise ValueError("지정가 주문에는 limit_price_usd 가 필요하다")
        if self.limit_price_usd is not None and self.limit_price_usd <= 0:
            raise ValueError(f"limit_price_usd 는 0보다 커야 한다: {self.limit_price_usd}")
        if not self.client_order_id:
            raise ValueError("client_order_id(멱등키)는 필수다")


@dataclass(frozen=True, slots=True)
class Fill:
    """체결 결과."""

    client_order_id: str
    broker_order_id: str
    symbol: str
    side: OrderSide
    qty: int
    price_usd: float
    fx_rate: float
    fee_krw: float
    ts: dt.datetime

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError(f"체결 수량은 1 이상이어야 한다: {self.qty}")
        if self.price_usd <= 0 or self.fx_rate <= 0:
            raise ValueError("price_usd/fx_rate 는 0보다 커야 한다")


@dataclass(frozen=True, slots=True)
class EntryPlan:
    """신규 진입 시 사전에 확정되는 계획.

    "얼마를 몇 번에 나눠 사고, 어디서 자르는가" 를 진입 **전에** 전부 정한다.
    진입 후에 결정하면 손실 중에 규칙을 바꾸게 되고, 그것이 계좌가 죽는 경로다.
    """

    tranche_qty: tuple[int, ...]
    """차수별 매수 수량. 인덱스가 곧 차수."""

    entry_atr_usd: float
    """진입 시점 ATR. 분할매수 간격과 손절폭의 고정 기준."""

    stop_price_usd: float
    """1차 진입가 기준 손절 가격."""

    total_notional_krw: float
    """전 차수 체결 시 투입될 원화 총액(수수료 포함 추정)."""

    def __post_init__(self) -> None:
        if not self.tranche_qty:
            raise ValueError("tranche_qty 는 최소 1개 차수가 필요하다")
        if any(q <= 0 for q in self.tranche_qty):
            raise ValueError(f"모든 차수 수량은 1 이상이어야 한다: {self.tranche_qty}")
        if self.entry_atr_usd <= 0:
            raise ValueError(f"entry_atr_usd 는 0보다 커야 한다: {self.entry_atr_usd}")
        if self.stop_price_usd <= 0:
            raise ValueError(f"stop_price_usd 는 0보다 커야 한다: {self.stop_price_usd}")

    @property
    def total_qty(self) -> int:
        return sum(self.tranche_qty)


@dataclass(frozen=True, slots=True)
class Decision:
    """전략 엔진의 1틱 판단 결과.

    ``action`` 이 HOLD 또는 BLOCKED 이면 ``qty`` 는 0이다.
    ``rationale`` 에는 사람이 읽을 수 있는 판단 근거가 반드시 담긴다. 감사추적의 핵심이다.
    """

    action: Action
    qty: int = 0
    limit_price_usd: float | None = None
    reason: ExitReason | None = None
    tp_levels: tuple[int, ...] = ()
    """이번 결정으로 체결되는 익절 계단 인덱스들.

    갭 상승으로 여러 계단을 한 번에 넘어설 수 있으므로 튜플이다.
    """

    entry_plan: EntryPlan | None = None
    """ENTER 결정일 때만 채워진다."""

    rationale: str = ""
    metrics: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.action in (Action.HOLD, Action.BLOCKED) and self.qty:
            raise ValueError(f"{self.action} 결정에는 수량이 있을 수 없다: {self.qty}")
        if self.action not in (Action.HOLD, Action.BLOCKED) and self.qty <= 0:
            raise ValueError(f"{self.action} 결정에는 수량이 1 이상이어야 한다: {self.qty}")
        if not self.rationale:
            raise ValueError("모든 결정에는 rationale(판단 근거)이 필요하다")

    @property
    def is_actionable(self) -> bool:
        """실제 주문을 내야 하는 결정인지."""
        return self.action not in (Action.HOLD, Action.BLOCKED)


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """완결된 1회 매매(진입부터 전량청산까지)의 기록. 성과 분석 단위."""

    symbol: str
    opened_at: dt.datetime
    closed_at: dt.datetime
    tranches: int
    max_qty: int
    entry_avg_price_usd: float
    exit_avg_price_usd: float
    entry_avg_fx: float
    exit_avg_fx: float
    cost_krw: float
    proceeds_krw: float
    exit_reasons: tuple[ExitReason, ...]

    @property
    def pnl_krw(self) -> float:
        """원화 실현손익."""
        return self.proceeds_krw - self.cost_krw

    @property
    def krw_return(self) -> float:
        """원화 실현수익률. 사용자가 보는 최종 숫자."""
        return self.proceeds_krw / self.cost_krw - 1.0 if self.cost_krw else 0.0

    @property
    def usd_return(self) -> float:
        """USD 가격만의 수익률(비용 제외). 환율 기여도 분해용."""
        if not self.entry_avg_price_usd:
            return 0.0
        return self.exit_avg_price_usd / self.entry_avg_price_usd - 1.0

    @property
    def fx_return(self) -> float:
        """환율 변동만의 수익률. krw_return 은 usd_return, fx_return, 비용의 합성이다."""
        if not self.entry_avg_fx:
            return 0.0
        return self.exit_avg_fx / self.entry_avg_fx - 1.0

    @property
    def holding_days(self) -> float:
        """보유 기간(일). 단타 여부 검증용."""
        return (self.closed_at - self.opened_at).total_seconds() / 86400.0

    @property
    def is_win(self) -> bool:
        return self.pnl_krw > 0
