"""설정. 전략 파라미터와 자격증명을 엄격히 분리한다.

분리 원칙
---------
* :class:`StrategyConfig` — 매매 규칙. **공개해도 안전**하며 YAML 로 저장하고 git 에 올린다.
* :class:`Credentials` — API 키·계좌번호. **절대 저장하지 않고** 환경변수에서만 읽는다.
  ``repr`` 과 로그에 원문이 나가지 않도록 마스킹된다.

민감정보가 실수로 커밋되는 가장 흔한 경로는 (1) 설정 파일에 키를 적고,
(2) 디버깅 중 객체를 print 하고, (3) 예외 메시지에 요청 헤더가 실리는 것이다.
이 모듈은 세 경로를 모두 차단한다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from enum import Enum
from pathlib import Path
from typing import Any

from koru_trade.pnl import CostModel, FxMode

__all__ = [
    "MAX_TARGET_KRW_RETURN",
    "MIN_TARGET_KRW_RETURN",
    "Credentials",
    "KisEnv",
    "RiskConfig",
    "ScaleInStep",
    "StrategyConfig",
    "TakeProfitStep",
    "is_dry_run",
    "load_credentials",
    "load_dotenv_if_present",
    "load_strategy_config",
    "mask_secret",
    "strategy_config_from_dict",
]

# 목표 원화 수익률 범위. 사용자 요구사항 "원화 환산 5~20% 사이 수익에서 분할 익절".
MIN_TARGET_KRW_RETURN = 0.05
MAX_TARGET_KRW_RETURN = 0.20


def mask_secret(value: str | None, keep: int = 4) -> str:
    """비밀값을 로그에 안전한 형태로 만든다.

    >>> mask_secret("PS1234567890ABCDEF")
    'PS12...(18자)'
    >>> mask_secret(None)
    '<미설정>'
    """
    if not value:
        return "<미설정>"
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}...({len(value)}자)"


class KisEnv(str, Enum):
    """한국투자증권 API 실행 환경."""

    PAPER = "paper"
    """모의투자."""

    LIVE = "live"
    """실전투자."""


@dataclass(frozen=True, slots=True)
class TakeProfitStep:
    """분할 익절 계단 한 칸.

    Attributes:
        krw_return: 이 계단이 발동하는 원화 수익률 문턱값. 0.05 = +5%.
        sell_fraction: 매도할 비율. **최초 계획 수량 기준**이다.
            잔여 수량 기준으로 하면 계단마다 실제 매도량이 달라져 검증이 어렵다.
    """

    krw_return: float
    sell_fraction: float

    def __post_init__(self) -> None:
        if not MIN_TARGET_KRW_RETURN <= self.krw_return <= MAX_TARGET_KRW_RETURN:
            raise ValueError(
                f"익절 문턱값은 {MIN_TARGET_KRW_RETURN:.0%}~{MAX_TARGET_KRW_RETURN:.0%} "
                f"범위여야 한다(사용자 요구사항): {self.krw_return:.2%}"
            )
        if not 0.0 < self.sell_fraction <= 1.0:
            raise ValueError(f"매도 비율은 0 초과 1 이하여야 한다: {self.sell_fraction}")


@dataclass(frozen=True, slots=True)
class ScaleInStep:
    """분할 매수 차수 한 칸.

    Attributes:
        atr_multiple: 1차 진입가 대비 하락폭을 ATR 배수로 지정한다.
            0.0 이면 진입과 동시에 체결(1차 매수).
            양수면 그만큼 **더 떨어졌을 때** 매수한다.
        weight: 총 계획 금액 중 이 차수가 차지하는 비중.
    """

    atr_multiple: float
    weight: float

    def __post_init__(self) -> None:
        if self.atr_multiple < 0:
            raise ValueError(f"atr_multiple 은 0 이상이어야 한다: {self.atr_multiple}")
        if not 0.0 < self.weight <= 1.0:
            raise ValueError(f"weight 는 0 초과 1 이하여야 한다: {self.weight}")


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """리스크 한도와 킬스위치.

    기본값은 KORU 의 실측 변동성(2019~2026 연평균 ATR 이 주가의 4~12%)을 근거로 정했다.
    3배 레버리지 상품에서 이 한도를 완화하는 것은 계좌를 거는 행위다.
    """

    max_position_krw: float = 5_000_000.0
    """1회 매매에 투입할 수 있는 최대 원화 금액(모든 분할 차수 합계)."""

    max_daily_notional_krw: float = 3_000_000.0
    """하루에 신규로 투입할 수 있는 최대 원화 금액."""

    daily_loss_limit_krw: float = 300_000.0
    """당일 실현손실이 이 금액에 도달하면 당일 매매를 전면 중단한다."""

    max_consecutive_losses: int = 3
    """연속 손절 횟수가 이 값에 도달하면 쿨다운에 들어간다."""

    loss_cooldown_days: int = 5
    """연속 손실 한도에 걸린 뒤 쉬어가는 영업일 수.

    **영구 정지로 만들면 안 된다.** 카운터를 되돌릴 방법이 없어 봇이 죽는다.
    이길 수 없으니 카운터가 줄지 않고, 카운터가 줄지 않으니 거래할 수 없다.
    쿨다운이 끝나면 카운터가 0으로 리셋되고 매매가 재개된다.
    """

    max_trades_per_day: int = 3
    """하루 최대 신규 진입 횟수. 과매매 방지."""

    max_open_positions: int = 1
    """동시에 보유할 수 있는 포지션 수. 단일 종목 전략이므로 1."""

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            if value <= 0:
                raise ValueError(f"{f.name} 은 0보다 커야 한다: {value}")
        if self.max_daily_notional_krw > self.max_position_krw * self.max_trades_per_day:
            raise ValueError(
                "max_daily_notional_krw 가 (max_position_krw * max_trades_per_day)를 "
                "초과하면 한도가 서로 모순된다"
            )


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """전략 파라미터 전체. 이 객체 하나가 매매 행동을 완전히 결정한다.

    백테스트와 실거래가 같은 객체를 쓰므로, 백테스트에서 검증한 파라미터가
    실거래에서 다르게 동작할 수 없다.
    """

    symbol: str = "KORU"
    exchange: str = "AMS"
    """한국투자증권 해외거래소 코드. KORU 는 NYSE Arca 상장이며 KIS 는 AMS 로 분류한다."""

    # --- 자금 배분 -----------------------------------------------------------
    capital_krw: float = 3_000_000.0
    """1회 매매에 배분할 총 원화 예산(모든 분할 차수의 합)."""

    # --- 분할 매수 -----------------------------------------------------------
    scale_in: tuple[ScaleInStep, ...] = (
        ScaleInStep(atr_multiple=0.0, weight=0.40),
        ScaleInStep(atr_multiple=0.7, weight=0.35),
        ScaleInStep(atr_multiple=1.4, weight=0.25),
    )
    """분할 매수 계획. 기본 3차 40/35/25.

    1차에 가장 크게 넣고 뒤로 갈수록 줄인다. 반대로 하는 것(마틴게일)은
    3배 레버리지에서 계좌를 파괴하는 전형적인 방식이다.
    간격을 ATR 배수로 잡는 이유는 변동성 레짐이 바뀌어도 자동으로 조정되기 때문이다.
    """

    scale_in_requires_reclaim: bool = True
    """추가 매수 시 종가가 직전 봉 고가를 회복했는지 확인한다.

    떨어지는 칼을 잡지 않기 위한 조건. 끄면 순수 하락 물타기가 된다.
    """

    # --- 분할 익절 (원화 기준) -----------------------------------------------
    take_profit: tuple[TakeProfitStep, ...] = (
        TakeProfitStep(krw_return=0.05, sell_fraction=0.30),
        TakeProfitStep(krw_return=0.09, sell_fraction=0.30),
        TakeProfitStep(krw_return=0.14, sell_fraction=0.25),
        TakeProfitStep(krw_return=0.20, sell_fraction=0.15),
    )
    """원화 수익률 기준 분할 익절 계단. 사용자 요구 5~20% 구간을 4단계로 나눴다.

    앞단(5%)에 30% 를 배치해 왕복 비용(약 0.7%)을 즉시 회수하고,
    뒤로 갈수록 비중을 줄여 변동성 감쇠에 노출되는 잔량을 최소화한다.
    """

    breakeven_stop_after_tp: int = 1
    """이 계단 수만큼 익절이 체결되면 손절선을 원화 본전으로 올린다.

    1 이면 첫 익절(+5%) 직후부터 이 매매는 원화 기준으로 손실이 날 수 없다.
    """

    # --- 손절 / 청산 ---------------------------------------------------------
    stop_atr_multiple: float = 2.0
    """1차 진입가 대비 ATR 배수 손절폭."""

    max_stop_pct: float = 0.12
    """ATR 기준 손절폭의 절대 상한. 변동성이 폭발해도 이보다 넓게 잡지 않는다."""

    min_stop_pct: float = 0.04
    """손절폭 하한. 너무 좁으면 정상 노이즈에 매번 털린다."""

    hard_stop_krw_return: float = -0.10
    """원화 평가수익률이 이 값에 도달하면 사유 불문 전량 청산한다.

    USD 기준 손절과 별개로 동작한다. 환율이 급락하면 주가가 안 빠져도 이쪽이 먼저 걸린다.
    """

    trailing_after_tp: int = 2
    """이 계단 수만큼 익절되면 트레일링 스톱을 켠다."""

    trailing_giveback: float = 0.35
    """트레일링 스톱 폭. 최고 원화 수익률 대비 이 비율만큼 반납하면 청산.

    0.35 이면 +14% 를 찍은 뒤 +9.1% 로 밀리면 나온다.
    """

    max_holding_days: int = 5
    """타임 스톱. 단타 전략이므로 이 기간을 넘기면 사유 불문 청산한다.

    3배 레버리지의 변동성 감쇠는 보유 기간에 비례해 누적되므로
    "언젠가 오르겠지" 는 이 상품에서 특히 비싼 생각이다.
    """

    # --- 진입 필터 -----------------------------------------------------------
    atr_period: int = 14
    rsi_period: int = 14
    ema_fast: int = 10
    ema_slow: int = 30

    max_atr_pct: float = 0.08
    """ATR/종가 가 이 값을 넘으면 진입 금지(변동성 레짐 필터).

    ATR 이 주가의 8% 라는 것은 하루 평균 진폭이 8% 라는 뜻이다.
    목표 익절폭 5% 가 하루 진폭 안에 완전히 들어가 버리므로,
    이 이상에서는 익절과 손절이 같은 날 무작위로 결정된다. 즉 전략이 아니라 도박이 된다.
    """

    min_rsi: float = 45.0
    max_rsi: float = 70.0
    """RSI 진입 허용 구간. 아래는 하락 추세, 위는 과열 추격 매수를 막는다."""

    min_adx: float = 18.0
    """추세 강도 하한. 횡보장에서 레버리지 상품을 들고 있으면 감쇠만 먹는다."""

    max_gap_pct: float = 0.05
    """당일 시가 갭 절대값 상한. 갭이 크면 지정가 체결 품질이 급격히 나빠진다."""

    min_avg_dollar_volume: float = 3_000_000.0
    """20일 평균 거래대금(USD) 하한. 유동성 필터."""

    # --- 환율 필터 -----------------------------------------------------------
    fx_lookback_days: int = 20
    max_fx_decline: float = -0.03
    """최근 ``fx_lookback_days`` 동안 USD/KRW 변화율이 이 값보다 낮으면 진입 금지.

    환율이 20일 만에 3% 넘게 빠지고 있다면, 목표인 원화 +5% 를 달성하려면
    주가가 8% 넘게 올라야 한다. 추세가 이어지는 한 확률이 나쁜 베팅이다.
    """

    fx_max_level: float | None = None
    """이 환율 이상에서는 신규 진입 금지(고환율 진입 방지). None 이면 미적용."""

    # --- 실행 --------------------------------------------------------------
    limit_slippage_bps: float = 15.0
    """지정가 주문을 현재가에서 얼마나 유리하게/불리하게 낼지(bp). 매수는 위로, 매도는 아래로."""

    use_market_order_on_stop: bool = True
    """손절 시에는 시장가를 허용한다. 지정가로 버티다 더 밀리는 것을 막는다."""

    cost: CostModel = field(default_factory=CostModel)
    risk: RiskConfig = field(default_factory=RiskConfig)

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol 은 필수다")
        if self.capital_krw <= 0:
            raise ValueError(f"capital_krw 는 0보다 커야 한다: {self.capital_krw}")
        if self.capital_krw > self.risk.max_position_krw:
            raise ValueError(
                f"capital_krw({self.capital_krw:,.0f})가 "
                f"max_position_krw({self.risk.max_position_krw:,.0f})를 초과한다"
            )
        if not self.scale_in:
            raise ValueError("scale_in 은 최소 1개 차수가 필요하다")
        if not self.take_profit:
            raise ValueError("take_profit 은 최소 1개 계단이 필요하다")

        weight_sum = sum(s.weight for s in self.scale_in)
        if abs(weight_sum - 1.0) > 1e-9:
            raise ValueError(f"scale_in weight 합계는 1.0이어야 한다: {weight_sum}")

        tp_sum = sum(s.sell_fraction for s in self.take_profit)
        if tp_sum - 1.0 > 1e-9:
            raise ValueError(f"take_profit sell_fraction 합계가 1.0을 초과한다: {tp_sum}")

        multiples = [s.atr_multiple for s in self.scale_in]
        if multiples != sorted(multiples):
            raise ValueError("scale_in 의 atr_multiple 은 오름차순이어야 한다")
        if multiples[0] != 0.0:
            raise ValueError("scale_in 의 1차는 atr_multiple=0.0(즉시 진입)이어야 한다")

        thresholds = [s.krw_return for s in self.take_profit]
        if thresholds != sorted(thresholds):
            raise ValueError("take_profit 의 krw_return 은 오름차순이어야 한다")
        if len(set(thresholds)) != len(thresholds):
            raise ValueError("take_profit 문턱값에 중복이 있다")

        drag = self.cost.round_trip_drag
        if thresholds[0] <= drag:
            raise ValueError(
                f"첫 익절 문턱값({thresholds[0]:.2%})이 왕복 비용({drag:.2%}) 이하다. "
                "이 계단은 구조적으로 손실이다"
            )

        if self.hard_stop_krw_return >= 0:
            raise ValueError("hard_stop_krw_return 은 음수여야 한다")
        if not 0.0 < self.trailing_giveback < 1.0:
            raise ValueError("trailing_giveback 은 0과 1 사이여야 한다")
        if self.min_stop_pct >= self.max_stop_pct:
            raise ValueError("min_stop_pct 는 max_stop_pct 보다 작아야 한다")
        if self.max_holding_days < 1:
            raise ValueError("max_holding_days 는 1 이상이어야 한다")
        if self.min_rsi >= self.max_rsi:
            raise ValueError("min_rsi 는 max_rsi 보다 작아야 한다")
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast 는 ema_slow 보다 작아야 한다")

    @property
    def total_tranches(self) -> int:
        return len(self.scale_in)

    @property
    def warmup_bars(self) -> int:
        """지표 계산에 필요한 최소 봉 개수. 백테스트 시작 구간을 건너뛰는 데 쓴다."""
        return max(
            self.ema_slow + 5,
            2 * self.atr_period + 2,
            self.rsi_period + 2,
            self.fx_lookback_days + 1,
            2 * 14 + 2,  # ADX(14)
            20,  # 거래대금 평균
        )

    def to_dict(self) -> dict[str, Any]:
        """YAML 직렬화용 사전. 자격증명은 애초에 이 객체에 없다."""
        return {
            "symbol": self.symbol,
            "exchange": self.exchange,
            "capital_krw": self.capital_krw,
            "scale_in": [
                {"atr_multiple": s.atr_multiple, "weight": s.weight} for s in self.scale_in
            ],
            "scale_in_requires_reclaim": self.scale_in_requires_reclaim,
            "take_profit": [
                {"krw_return": s.krw_return, "sell_fraction": s.sell_fraction}
                for s in self.take_profit
            ],
            "breakeven_stop_after_tp": self.breakeven_stop_after_tp,
            "stop_atr_multiple": self.stop_atr_multiple,
            "max_stop_pct": self.max_stop_pct,
            "min_stop_pct": self.min_stop_pct,
            "hard_stop_krw_return": self.hard_stop_krw_return,
            "trailing_after_tp": self.trailing_after_tp,
            "trailing_giveback": self.trailing_giveback,
            "max_holding_days": self.max_holding_days,
            "atr_period": self.atr_period,
            "rsi_period": self.rsi_period,
            "ema_fast": self.ema_fast,
            "ema_slow": self.ema_slow,
            "max_atr_pct": self.max_atr_pct,
            "min_rsi": self.min_rsi,
            "max_rsi": self.max_rsi,
            "min_adx": self.min_adx,
            "max_gap_pct": self.max_gap_pct,
            "min_avg_dollar_volume": self.min_avg_dollar_volume,
            "fx_lookback_days": self.fx_lookback_days,
            "max_fx_decline": self.max_fx_decline,
            "fx_max_level": self.fx_max_level,
            "limit_slippage_bps": self.limit_slippage_bps,
            "use_market_order_on_stop": self.use_market_order_on_stop,
            "cost": {
                "buy_fee_rate": self.cost.buy_fee_rate,
                "sell_fee_rate": self.cost.sell_fee_rate,
                "sec_fee_rate": self.cost.sec_fee_rate,
                "finra_taf_per_share": self.cost.finra_taf_per_share,
                "fx_spread_buy": self.cost.fx_spread_buy,
                "fx_spread_sell": self.cost.fx_spread_sell,
                "slippage_rate": self.cost.slippage_rate,
                "fx_mode": self.cost.fx_mode.value,
            },
            "risk": {
                "max_position_krw": self.risk.max_position_krw,
                "max_daily_notional_krw": self.risk.max_daily_notional_krw,
                "daily_loss_limit_krw": self.risk.daily_loss_limit_krw,
                "max_consecutive_losses": self.risk.max_consecutive_losses,
                "loss_cooldown_days": self.risk.loss_cooldown_days,
                "max_trades_per_day": self.risk.max_trades_per_day,
                "max_open_positions": self.risk.max_open_positions,
            },
        }


def strategy_config_from_dict(raw: dict[str, Any]) -> StrategyConfig:
    """사전에서 :class:`StrategyConfig` 를 만든다. 알 수 없는 키는 즉시 오류다.

    오타 난 파라미터가 조용히 무시되어 "설정했다고 믿었는데 기본값으로 돌던"
    사고를 막기 위해 엄격하게 검사한다.
    """
    data = dict(raw)
    known = {f.name for f in fields(StrategyConfig)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"알 수 없는 설정 키: {sorted(unknown)}")

    if "scale_in" in data:
        data["scale_in"] = tuple(ScaleInStep(**s) for s in data["scale_in"])
    if "take_profit" in data:
        data["take_profit"] = tuple(TakeProfitStep(**s) for s in data["take_profit"])
    if "cost" in data:
        cost_raw = dict(data["cost"])
        if "fx_mode" in cost_raw:
            cost_raw["fx_mode"] = FxMode(cost_raw["fx_mode"])
        known_cost = {f.name for f in fields(CostModel)}
        unknown_cost = set(cost_raw) - known_cost
        if unknown_cost:
            raise ValueError(f"알 수 없는 cost 키: {sorted(unknown_cost)}")
        data["cost"] = CostModel(**cost_raw)
    if "risk" in data:
        risk_raw = dict(data["risk"])
        known_risk = {f.name for f in fields(RiskConfig)}
        unknown_risk = set(risk_raw) - known_risk
        if unknown_risk:
            raise ValueError(f"알 수 없는 risk 키: {sorted(unknown_risk)}")
        data["risk"] = RiskConfig(**risk_raw)
    return StrategyConfig(**data)


def load_strategy_config(path: str | Path | None = None) -> StrategyConfig:
    """YAML 에서 전략 설정을 읽는다. 경로가 없거나 파일이 없으면 기본값을 쓴다."""
    if path is None:
        return StrategyConfig()
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"설정 파일이 없다: {p}")
    import yaml

    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"설정 파일 최상위는 사전이어야 한다: {p}")
    return strategy_config_from_dict(raw)


@dataclass(frozen=True)
class Credentials:
    """한국투자증권 API 자격증명.

    **이 객체는 절대 파일에 저장되지 않는다.** 환경변수에서만 읽는다.
    ``__repr__`` 과 ``__str__`` 이 마스킹되어 있어 로그나 예외 메시지에
    원문이 실릴 수 없다. ``slots`` 을 쓰지 않는 이유는 ``repr`` 재정의와
    dataclass 기본 동작을 명확히 제어하기 위해서다.
    """

    app_key: str
    app_secret: str
    account_no: str
    account_product_code: str
    env: KisEnv = KisEnv.PAPER

    def __post_init__(self) -> None:
        missing = [
            name
            for name in ("app_key", "app_secret", "account_no", "account_product_code")
            if not getattr(self, name)
        ]
        if missing:
            raise ValueError(f"자격증명 누락: {missing}. .env.example 을 참고해 .env 를 채워라")
        if not self.account_no.isdigit():
            raise ValueError("account_no 는 숫자여야 한다")
        if len(self.account_product_code) != 2 or not self.account_product_code.isdigit():
            raise ValueError("account_product_code 는 2자리 숫자여야 한다")

    @property
    def base_url(self) -> str:
        """환경에 맞는 API 도메인."""
        if self.env is KisEnv.LIVE:
            return "https://openapi.koreainvestment.com:9443"
        return "https://openapivts.koreainvestment.com:29443"

    @property
    def is_live(self) -> bool:
        return self.env is KisEnv.LIVE

    def __repr__(self) -> str:
        return (
            f"Credentials(app_key={mask_secret(self.app_key)}, "
            f"app_secret={mask_secret(self.app_secret)}, "
            f"account_no={mask_secret(self.account_no, 2)}, "
            f"account_product_code={self.account_product_code}, env={self.env.value})"
        )

    __str__ = __repr__


def load_credentials(env: dict[str, str] | None = None) -> Credentials:
    """환경변수에서 자격증명을 읽는다.

    ``.env`` 파일이 있으면 먼저 읽어 프로세스 환경에 주입한다.

    Args:
        env: 테스트용 환경변수 사전. None 이면 ``os.environ`` 을 쓴다.

    Raises:
        ValueError: 필수 항목이 없을 때. 메시지에 값은 절대 포함하지 않는다.
    """
    if env is None:
        load_dotenv_if_present()
        env = dict(os.environ)

    raw_env = env.get("KIS_ENV", "paper").strip().lower()
    try:
        kis_env = KisEnv(raw_env)
    except ValueError as exc:
        raise ValueError(f"KIS_ENV 는 'paper' 또는 'live' 여야 한다: {raw_env!r}") from exc

    return Credentials(
        app_key=env.get("KIS_APP_KEY", "").strip(),
        app_secret=env.get("KIS_APP_SECRET", "").strip(),
        account_no=env.get("KIS_ACCOUNT_NO", "").strip(),
        account_product_code=env.get("KIS_ACCOUNT_PRODUCT_CODE", "01").strip(),
        env=kis_env,
    )


def is_dry_run(env: dict[str, str] | None = None) -> bool:
    """DRY_RUN 여부. **기본값은 True** 다.

    환경변수가 없거나 값이 이상하면 안전한 쪽(주문 안 나감)으로 판정한다.
    실거래를 켜려면 명시적으로 ``KORU_DRY_RUN=false`` 를 설정해야 한다.
    """
    if env is None:
        load_dotenv_if_present()
        env = dict(os.environ)
    raw = env.get("KORU_DRY_RUN", "true").strip().lower()
    return raw not in ("false", "0", "no", "off")


def load_dotenv_if_present() -> None:
    """``.env`` 가 있으면 프로세스 환경에 주입한다. 없어도 조용히 넘어간다.

    **환경변수를 읽는 모든 진입점이 이 함수를 먼저 불러야 한다.**
    하나라도 빠뜨리면 그 설정만 조용히 무시된다.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - 선택적 의존성
        return
    dotenv_path = Path.cwd() / ".env"
    if dotenv_path.exists():
        load_dotenv(dotenv_path, override=False)
