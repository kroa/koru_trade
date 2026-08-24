"""원화(KRW) 환산 손익 계산 엔진.

이 모듈이 이 프로젝트의 핵심이다.
사용자의 요구는 "환율로 손해보지 않도록 **원화 환산** 5~20% 수익에서 분할 익절"이므로,
모든 익절/손절 판정은 USD 가격 수익률이 아니라 아래 식으로 계산한 원화 실현수익률로 한다.

기본 식
-------
매수 시 실제로 빠져나가는 원화::

    cost_krw = qty * buy_price_usd * (1 + fee_buy) * fx_mid_buy * (1 + spread_buy)

매도 시 실제로 들어오는 원화::

    proceeds_krw = qty * sell_price_usd * (1 - fee_sell - sec_fee) * fx_mid_sell * (1 - spread_sell)

원화 수익률::

    krw_return = proceeds_krw / cost_krw - 1

이를 인수분해하면 다음과 같다::

    krw_return = R_price * R_fx * C - 1

    R_price = sell_price_usd / buy_price_usd      (주가 기여)
    R_fx    = fx_mid_sell   / fx_mid_buy          (환율 기여)
    C       = (1 - fee_sell - sec_fee)(1 - spread_sell)
              / [(1 + fee_buy)(1 + spread_buy)]   (비용 계수, 항상 1보다 작다)

따라서 **환율이 내리면 주가가 그만큼 더 올라야 목표 수익률에 도달**한다.
:func:`required_sell_price_usd` 가 그 필요가격을 정확히 역산해 준다.

환전 방식
---------
:class:`FxMode` 로 두 가지를 지원한다.

``CONVERT``
    매수할 때 원화를 달러로 바꾸고, 매도 후 달러를 원화로 되돌린다.
    환전 스프레드를 왕복 2회 부담한다. 국내 증권사 기본 동작이며 보수적이다.
``HOLD_USD``
    달러 예수금을 그대로 굴린다. 스프레드는 없지만 환율 변동은 평가액에 그대로 반영된다.
    (원화로 최종 회수할 시점에는 결국 스프레드가 발생하므로,
    이 모드는 "당장 원화로 안 바꿀 때"의 평가 기준이다.)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from koru_trade.models import Position

__all__ = [
    "CostModel",
    "FxMode",
    "PnlBreakdown",
    "breakeven_sell_price_usd",
    "buy_fx_rate",
    "cost_factor",
    "decompose",
    "krw_cost",
    "krw_proceeds",
    "krw_return",
    "position_krw_return",
    "position_required_price_usd",
    "required_sell_price_usd",
    "sell_fx_rate",
]


class FxMode(str, Enum):
    """환전 방식."""

    CONVERT = "CONVERT"
    """원화 <-> 달러 환전을 왕복으로 한다(스프레드 2회)."""

    HOLD_USD = "HOLD_USD"
    """달러 예수금을 유지한다(스프레드 0회, 환율 변동은 그대로 반영)."""


@dataclass(frozen=True, slots=True)
class CostModel:
    """거래 비용 모델.

    기본값은 국내 대형 증권사의 **온라인 미국주식 우대 조건**을 가정한 보수적 수치다.
    실제 계약 조건에 맞게 반드시 조정하라. 값이 낙관적일수록 백테스트가 부풀려진다.

    Attributes:
        buy_fee_rate: 매수 거래수수료율. 0.0007 = 0.07%.
        sell_fee_rate: 매도 거래수수료율.
        sec_fee_rate: 미국 SEC 수수료. 매도 시에만 부과되며 요율은 매년 바뀐다.
        finra_taf_per_share: FINRA TAF. 매도 주당 정액(USD), 건당 상한 있음.
        fx_spread_buy: 매수 환전 스프레드(매매기준율 대비). 0.0025 = 우대 75% 적용 수준.
        fx_spread_sell: 매도 환전 스프레드.
        slippage_rate: 체결 슬리피지. 지정가라도 호가 이동으로 발생한다.
        fx_mode: 환전 방식.
    """

    buy_fee_rate: float = 0.0007
    sell_fee_rate: float = 0.0007
    sec_fee_rate: float = 0.0000278
    finra_taf_per_share: float = 0.000166
    fx_spread_buy: float = 0.0025
    fx_spread_sell: float = 0.0025
    slippage_rate: float = 0.0015
    fx_mode: FxMode = FxMode.CONVERT

    def __post_init__(self) -> None:
        rates = {
            "buy_fee_rate": self.buy_fee_rate,
            "sell_fee_rate": self.sell_fee_rate,
            "sec_fee_rate": self.sec_fee_rate,
            "fx_spread_buy": self.fx_spread_buy,
            "fx_spread_sell": self.fx_spread_sell,
            "slippage_rate": self.slippage_rate,
        }
        for name, value in rates.items():
            if not 0.0 <= value < 1.0:
                raise ValueError(f"{name} 은 0 이상 1 미만이어야 한다: {value}")
        if self.finra_taf_per_share < 0:
            raise ValueError("finra_taf_per_share 는 음수일 수 없다")
        total_sell = self.sell_fee_rate + self.sec_fee_rate
        if total_sell >= 1.0:
            raise ValueError(f"매도 비용 합계가 100% 이상이다: {total_sell}")

    @property
    def effective_buy_spread(self) -> float:
        """매수 시 적용할 환전 스프레드. HOLD_USD 모드에서는 0."""
        return self.fx_spread_buy if self.fx_mode is FxMode.CONVERT else 0.0

    @property
    def effective_sell_spread(self) -> float:
        """매도 시 적용할 환전 스프레드. HOLD_USD 모드에서는 0."""
        return self.fx_spread_sell if self.fx_mode is FxMode.CONVERT else 0.0

    @property
    def round_trip_drag(self) -> float:
        """가격과 환율이 전혀 안 움직여도 발생하는 왕복 손실률(양수).

        주당 정액 수수료(FINRA TAF)는 가격에 의존하므로 여기서는 제외된 **근사값**이다.
        정확한 값이 필요하면 :meth:`round_trip_drag_at` 를 쓴다.
        설정 검증(익절 문턱값이 비용보다 큰지)에는 이 근사로 충분하다.
        """
        return 1.0 - cost_factor(self)

    def round_trip_drag_at(self, price_usd: float) -> float:
        """특정 가격에서의 **정확한** 왕복 손실률(양수). TAF 포함."""
        if price_usd <= 0:
            raise ValueError(f"가격은 0보다 커야 한다: {price_usd}")
        gross = (
            price_usd * (1.0 - self.sell_fee_rate - self.sec_fee_rate) - self.finra_taf_per_share
        )
        num = gross * (1.0 - self.effective_sell_spread)
        den = price_usd * (1.0 + self.buy_fee_rate) * (1.0 + self.effective_buy_spread)
        return 1.0 - num / den


def buy_fx_rate(fx_mid: float, cost: CostModel) -> float:
    """매수 시 실제로 적용되는 환율(원화를 달러로 바꿀 때는 비싸게 산다)."""
    return fx_mid * (1.0 + cost.effective_buy_spread)


def sell_fx_rate(fx_mid: float, cost: CostModel) -> float:
    """매도 시 실제로 적용되는 환율(달러를 원화로 바꿀 때는 싸게 판다)."""
    return fx_mid * (1.0 - cost.effective_sell_spread)


def cost_factor(cost: CostModel) -> float:
    """비용 계수 ``C``. 가격·환율 변동이 0일 때 남는 비율(항상 1 미만).

    주당 정액 수수료(FINRA TAF)는 가격에 의존하므로 여기서는 제외된다.
    금액이 매우 작아 계수 근사에 영향이 미미하며,
    :func:`krw_proceeds` 에서는 정확히 반영된다.
    """
    numerator = (1.0 - cost.sell_fee_rate - cost.sec_fee_rate) * (1.0 - cost.effective_sell_spread)
    denominator = (1.0 + cost.buy_fee_rate) * (1.0 + cost.effective_buy_spread)
    return numerator / denominator


def krw_cost(qty: int, price_usd: float, fx_mid: float, cost: CostModel) -> float:
    """매수에 실제로 들어가는 원화 총액(수수료·환전스프레드 포함).

    Args:
        qty: 매수 수량(주).
        price_usd: 체결 단가(USD).
        fx_mid: 체결 시점 USD/KRW 매매기준율.
        cost: 비용 모델.

    Returns:
        원화 총 매입원가.
    """
    _validate(qty, price_usd, fx_mid)
    notional_usd = qty * price_usd
    gross_usd = notional_usd * (1.0 + cost.buy_fee_rate)
    return gross_usd * buy_fx_rate(fx_mid, cost)


def krw_proceeds(qty: int, price_usd: float, fx_mid: float, cost: CostModel) -> float:
    """매도로 실제로 들어오는 원화 총액(수수료·SEC·TAF·환전스프레드 차감 후).

    Args:
        qty: 매도 수량(주).
        price_usd: 체결 단가(USD).
        fx_mid: 체결 시점 USD/KRW 매매기준율.
        cost: 비용 모델.

    Returns:
        원화 순수취액. 비용이 매도대금을 초과하면 0.
    """
    _validate(qty, price_usd, fx_mid)
    notional_usd = qty * price_usd
    fees_usd = notional_usd * (cost.sell_fee_rate + cost.sec_fee_rate)
    fees_usd += qty * cost.finra_taf_per_share
    net_usd = max(0.0, notional_usd - fees_usd)
    return net_usd * sell_fx_rate(fx_mid, cost)


def krw_return(
    qty: int,
    buy_price_usd: float,
    buy_fx_mid: float,
    sell_price_usd: float,
    sell_fx_mid: float,
    cost: CostModel,
) -> float:
    """1회 왕복 매매의 원화 실현수익률.

    Returns:
        수익률(예: 0.05 = 원화 기준 +5%).
    """
    c = krw_cost(qty, buy_price_usd, buy_fx_mid, cost)
    p = krw_proceeds(qty, sell_price_usd, sell_fx_mid, cost)
    return p / c - 1.0 if c > 0 else 0.0


def position_krw_return(
    position: Position, price_usd: float, fx_mid: float, cost: CostModel
) -> float:
    """현재 포지션 전량을 지금 청산했을 때의 원화 평가수익률.

    분할 매수로 여러 랏이 쌓여 있어도, 각 랏이 서로 다른 환율에 매입되었어도
    ``cost_krw`` 에 그 사실이 모두 누적되어 있으므로 이 한 줄이 정확한 답이다.

    Returns:
        원화 평가수익률. 포지션이 없으면 0.
    """
    if not position.is_open:
        return 0.0
    proceeds = krw_proceeds(position.qty, price_usd, fx_mid, cost)
    return proceeds / position.cost_krw - 1.0


def required_sell_price_usd(
    buy_price_usd: float,
    buy_fx_mid: float,
    sell_fx_mid: float,
    target_krw_return: float,
    cost: CostModel,
) -> float:
    """목표 원화 수익률에 도달하기 위해 필요한 USD 매도가.

    ``krw_return = R_price * R_fx * C - 1`` 을 ``sell_price_usd`` 에 대해 푼 것이다::

        sell_price_usd = buy_price_usd * (1 + target) / (R_fx * C)

    환율이 매수 시점보다 내려가면(``R_fx < 1``) 필요가격이 그만큼 위로 밀린다.
    이것이 사용자가 말한 "환율로 손해보지 않도록"의 정량적 표현이다.

    Args:
        buy_price_usd: 매수 단가.
        buy_fx_mid: 매수 시점 매매기준율.
        sell_fx_mid: 매도 시점(또는 현재) 매매기준율.
        target_krw_return: 목표 원화 수익률. 0.05 = +5%.
        cost: 비용 모델.

    주당 정액 수수료(FINRA TAF)까지 포함한 **정확한** 역산이다. 위 인수분해는
    설명을 위한 근사이고, 실제 계산은 다음 식을 푼다::

        [P(1 - fee_s - sec) - taf] * fx_s(1 - spr_s)
        --------------------------------------------- = 1 + T
              B(1 + fee_b) * fx_b(1 + spr_b)

    Returns:
        필요한 USD 매도 단가.
    """
    if buy_price_usd <= 0 or buy_fx_mid <= 0 or sell_fx_mid <= 0:
        raise ValueError("가격과 환율은 0보다 커야 한다")
    if target_krw_return <= -1.0:
        raise ValueError(f"목표 수익률은 -100% 초과여야 한다: {target_krw_return}")

    cost_side = (
        buy_price_usd * (1.0 + cost.buy_fee_rate) * buy_fx_mid * (1.0 + cost.effective_buy_spread)
    )
    needed_krw = cost_side * (1.0 + target_krw_return)
    net_usd = needed_krw / sell_fx_rate(sell_fx_mid, cost)
    denom = 1.0 - cost.sell_fee_rate - cost.sec_fee_rate
    return (net_usd + cost.finra_taf_per_share) / denom


def breakeven_sell_price_usd(
    buy_price_usd: float, buy_fx_mid: float, sell_fx_mid: float, cost: CostModel
) -> float:
    """원화 기준 본전이 되는 USD 매도가. 본전 스톱(breakeven stop) 설정에 쓴다."""
    return required_sell_price_usd(buy_price_usd, buy_fx_mid, sell_fx_mid, 0.0, cost)


def position_required_price_usd(
    position: Position, fx_mid: float, target_krw_return: float, cost: CostModel
) -> float:
    """분할 매수된 포지션 전체가 목표 원화 수익률에 도달하는 USD 가격.

    포지션의 원화 원가 총액이 이미 확정되어 있으므로, 필요 매도대금을 역산한 뒤
    수수료와 스프레드를 되돌려 단가로 환산한다.

    Returns:
        필요한 USD 단가. 포지션이 없으면 0.
    """
    if not position.is_open:
        return 0.0
    if fx_mid <= 0:
        raise ValueError("환율은 0보다 커야 한다")
    qty = position.qty
    needed_krw = position.cost_krw * (1.0 + target_krw_return)
    net_usd = needed_krw / sell_fx_rate(fx_mid, cost)
    # net_usd = qty*P*(1 - fee - sec) - qty*taf  =>  P 로 정리
    denom = qty * (1.0 - cost.sell_fee_rate - cost.sec_fee_rate)
    return (net_usd + qty * cost.finra_taf_per_share) / denom


@dataclass(frozen=True, slots=True)
class PnlBreakdown:
    """원화 수익률을 주가/환율/비용 기여분으로 분해한 결과.

    ``(1 + krw) = (1 + price) * (1 + fx) * (1 + cost_drag)`` 의 곱셈 관계가 정확히 성립한다.
    로그 기여도는 덧셈으로 분해되어 보고서에 쓰기 좋다.
    """

    krw_return: float
    price_return: float
    fx_return: float
    cost_drag: float

    @property
    def log_price(self) -> float:
        return math.log1p(self.price_return)

    @property
    def log_fx(self) -> float:
        return math.log1p(self.fx_return)

    @property
    def log_cost(self) -> float:
        return math.log1p(self.cost_drag)

    def as_dict(self) -> dict[str, float]:
        return {
            "krw_return": self.krw_return,
            "price_return": self.price_return,
            "fx_return": self.fx_return,
            "cost_drag": self.cost_drag,
        }


def decompose(
    buy_price_usd: float,
    buy_fx_mid: float,
    sell_price_usd: float,
    sell_fx_mid: float,
    cost: CostModel,
) -> PnlBreakdown:
    """원화 수익률을 주가·환율·비용 기여분으로 분해한다.

    "얼마를 벌었는데 그중 환율 때문에 얼마를 잃었나"를 보고서에 쓰기 위한 함수다.

    ``krw_return`` 은 :func:`krw_return` 과 정확히 일치하며(TAF 포함),
    ``cost_drag`` 는 곱셈 항등식이 성립하도록 잔차로 계산한다.
    """
    if buy_price_usd <= 0 or buy_fx_mid <= 0 or sell_price_usd <= 0 or sell_fx_mid <= 0:
        raise ValueError("가격과 환율은 0보다 커야 한다")
    price_r = sell_price_usd / buy_price_usd - 1.0
    fx_r = sell_fx_mid / buy_fx_mid - 1.0
    # 수량 1주로 계산해도 비율은 같다(TAF 는 주당 정액이라 단가에만 영향).
    krw_r = krw_return(1, buy_price_usd, buy_fx_mid, sell_price_usd, sell_fx_mid, cost)
    drag = (1.0 + krw_r) / ((1.0 + price_r) * (1.0 + fx_r)) - 1.0
    return PnlBreakdown(krw_return=krw_r, price_return=price_r, fx_return=fx_r, cost_drag=drag)


def _validate(qty: int, price_usd: float, fx_mid: float) -> None:
    if qty <= 0:
        raise ValueError(f"수량은 1 이상이어야 한다: {qty}")
    if price_usd <= 0:
        raise ValueError(f"가격은 0보다 커야 한다: {price_usd}")
    if fx_mid <= 0:
        raise ValueError(f"환율은 0보다 커야 한다: {fx_mid}")
