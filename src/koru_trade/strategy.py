"""매매 의사결정 엔진.

**이 모듈은 전부 순수 함수다.** 네트워크도, 파일도, 시계도 건드리지 않는다.
같은 입력에는 언제나 같은 결정을 돌려준다.
그래서 백테스트와 실거래가 이 코드를 그대로 공유하며, 두 결과가 어긋날 수 없다.

판단 우선순위
-------------
청산이 항상 진입보다 먼저다. 순서를 바꾸면 손절이 필요한 순간에 물타기를 하게 된다.

1. 리스크 킬스위치 (진입만 차단, 청산은 절대 막지 않는다)
2. 원화 하드 스톱 (:attr:`StrategyConfig.hard_stop_krw_return`)
3. USD 하드 스톱 (1차 진입가 - ATR 배수)
4. 본전 스톱 (1단 익절 후)
5. 트레일링 스톱 (2단 익절 후)
6. 타임 스톱 (보유일 초과)
7. 원화 분할 익절 계단
8. 분할 매수 (추가 차수)
9. 신규 진입
10. HOLD
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass

from koru_trade import indicators as ind
from koru_trade.config import StrategyConfig
from koru_trade.models import (
    Action,
    Bar,
    Decision,
    EntryPlan,
    ExitReason,
    Position,
)
from koru_trade.pnl import krw_cost, position_krw_return, position_required_price_usd
from koru_trade.risk import RiskState, check_entry_allowed, check_scale_in_allowed

__all__ = [
    "EntryCheck",
    "EntrySignal",
    "allocate_tranches",
    "bar_interval_minutes",
    "business_days_between",
    "decide",
    "evaluate_entry",
    "plan_entry",
    "scale_in_trigger_price",
    "stop_price_usd",
]


@dataclass(frozen=True, slots=True)
class EntryCheck:
    """진입 필터 1개의 판정 결과. 리포트에 그대로 출력된다."""

    name: str
    passed: bool
    detail: str
    value: float | None = None
    threshold: float | None = None

    @property
    def mark(self) -> str:
        return "통과" if self.passed else "차단"


@dataclass(frozen=True, slots=True)
class EntrySignal:
    """진입 필터 전체의 종합 판정."""

    checks: tuple[EntryCheck, ...]

    @property
    def allowed(self) -> bool:
        """모든 필터를 통과했는지."""
        return all(c.passed for c in self.checks)

    @property
    def blockers(self) -> tuple[EntryCheck, ...]:
        """차단된 필터들."""
        return tuple(c for c in self.checks if not c.passed)

    @property
    def summary(self) -> str:
        if self.allowed:
            return f"진입 조건 {len(self.checks)}개 전부 통과"
        names = ", ".join(c.name for c in self.blockers)
        return f"{len(self.blockers)}/{len(self.checks)}개 조건 미충족: {names}"


# ---------------------------------------------------------------------------
# 가격 수준 계산 (백테스트 엔진도 그대로 재사용한다)
# ---------------------------------------------------------------------------


def stop_price_usd(position: Position, cfg: StrategyConfig) -> float:
    """손절 가격(USD).

    1차 진입가에서 ``stop_atr_multiple * ATR`` 만큼 아래다.
    단 ``min_stop_pct`` ~ ``max_stop_pct`` 로 클램프한다.

    평단이 아니라 **1차 진입가**를 기준으로 삼는 것이 핵심이다.
    분할 매수로 평단이 내려가도 손절선은 그대로 있어야
    "1회 매매의 최대 손실" 이 사전에 확정된다.
    """
    entry = position.first_entry_price_usd
    if entry <= 0:
        return 0.0
    atr = position.entry_atr_usd
    raw_pct = (cfg.stop_atr_multiple * atr / entry) if atr > 0 else cfg.max_stop_pct
    pct = min(max(raw_pct, cfg.min_stop_pct), cfg.max_stop_pct)
    return entry * (1.0 - pct)


def scale_in_trigger_price(position: Position, tranche_index: int, cfg: StrategyConfig) -> float:
    """``tranche_index`` 차수의 추가 매수 트리거 가격(USD).

    1차 진입가에서 ATR 배수만큼 내려온 지점이다. 0차는 진입가 자체.
    """
    if tranche_index < 0 or tranche_index >= len(cfg.scale_in):
        raise ValueError(f"존재하지 않는 차수다: {tranche_index}")
    entry = position.first_entry_price_usd
    return entry - position.entry_atr_usd * cfg.scale_in[tranche_index].atr_multiple


def bar_interval_minutes(bars: Sequence[Bar]) -> float | None:
    """봉 간격(분)을 데이터에서 추정한다.

    최근 구간의 **양수 간격 중 최솟값**을 쓴다. 평균이나 최댓값을 쓰면
    밤사이 공백(마지막 봉 15:30 -> 다음 날 09:30 = 1080분)이 섞여 들어와
    간격이 터무니없이 커진다.

    Returns:
        분 단위 간격. 봉이 2개 미만이면 None.
    """
    if len(bars) < 2:
        return None
    window = bars[max(1, len(bars) - 30) :]
    prev = bars[max(0, len(bars) - 31)]
    gaps: list[float] = []
    for bar in window:
        delta = (bar.ts - prev.ts).total_seconds() / 60.0
        if delta > 0:
            gaps.append(delta)
        prev = bar
    return min(gaps) if gaps else None


def _bars_since(bars: Sequence[Bar], opened: dt.datetime) -> int:
    """진입 봉 이후 지나간 봉 개수.

    뒤에서부터 세다가 진입 시각 이전을 만나면 멈춘다. 보유 기간이 짧은
    단타에서는 전체 히스토리를 훑지 않아도 되므로 비용이 거의 없다.
    진입 봉 자체는 0으로 센다(그 봉에서는 아직 아무것도 지나지 않았다).
    """
    held = 0
    for bar in reversed(bars):
        if bar.ts <= opened:
            break
        held += 1
    return held


def business_days_between(start: dt.datetime, end: dt.datetime) -> int:
    """두 시점 사이의 영업일 수(주말 제외, 미국 공휴일은 무시).

    타임 스톱을 달력일이 아니라 영업일로 세기 위한 근사다.
    공휴일을 무시하므로 실제보다 살짝 길게 세는데, 이는 보수적인 방향은 아니지만
    주말을 세는 것보다 훨씬 정확하다.
    """
    if end <= start:
        return 0
    d0, d1 = start.date(), end.date()
    days = (d1 - d0).days
    full_weeks, remainder = divmod(days, 7)
    count = full_weeks * 5
    cursor = d0
    for _ in range(remainder):
        cursor += dt.timedelta(days=1)
        if cursor.weekday() < 5:
            count += 1
    return count


# ---------------------------------------------------------------------------
# 수량 배분
# ---------------------------------------------------------------------------


def allocate_tranches(total_qty: int, weights: Sequence[float]) -> tuple[int, ...]:
    """총 수량을 비중에 따라 차수별로 나눈다.

    최대잔여법(largest remainder)을 쓰되 **모든 차수가 최소 1주**를 갖도록 보정한다.
    총 수량이 차수보다 적으면 분할이 불가능하므로 1차에 전량 배정한다.

    >>> allocate_tranches(10, [0.4, 0.35, 0.25])
    (4, 4, 2)
    >>> allocate_tranches(2, [0.4, 0.35, 0.25])
    (2,)
    """
    n = len(weights)
    if total_qty <= 0 or n == 0:
        return ()
    if total_qty < n:
        return (total_qty,)

    raw = [total_qty * w for w in weights]
    alloc = [max(1, int(f)) for f in raw]
    diff = total_qty - sum(alloc)

    if diff > 0:
        order = sorted(range(n), key=lambda i: raw[i] - int(raw[i]), reverse=True)
        for i in range(diff):
            alloc[order[i % n]] += 1
    elif diff < 0:
        # 초과분은 마지막 차수부터 회수한다(앞 차수의 비중을 지키기 위해).
        for i in reversed(range(n)):
            while diff < 0 and alloc[i] > 1:
                alloc[i] -= 1
                diff += 1
            if diff == 0:
                break
    return tuple(alloc)


def plan_entry(
    bars: Sequence[Bar], cfg: StrategyConfig, capital_krw: float | None = None
) -> EntryPlan | None:
    """진입 계획(차수별 수량·손절가·투입금액)을 계산한다.

    Returns:
        계획. 자본이 1주도 못 사거나 ATR 을 계산할 수 없으면 None.
    """
    if not bars:
        return None
    atr = ind.atr(bars, cfg.atr_period)
    if atr is None or atr <= 0:
        return None

    last = bars[-1]
    budget = cfg.capital_krw if capital_krw is None else capital_krw
    unit_cost = krw_cost(1, last.close, last.fx_rate, cfg.cost)
    total_qty = int(budget // unit_cost)
    if total_qty < 1:
        return None

    weights = [s.weight for s in cfg.scale_in]
    tranche_qty = allocate_tranches(total_qty, weights)
    if not tranche_qty:
        return None

    stop_pct_raw = cfg.stop_atr_multiple * atr / last.close
    stop_pct = min(max(stop_pct_raw, cfg.min_stop_pct), cfg.max_stop_pct)
    return EntryPlan(
        tranche_qty=tranche_qty,
        entry_atr_usd=atr,
        stop_price_usd=last.close * (1.0 - stop_pct),
        total_notional_krw=sum(tranche_qty) * unit_cost,
    )


# ---------------------------------------------------------------------------
# 진입 필터
# ---------------------------------------------------------------------------


def evaluate_entry(bars: Sequence[Bar], cfg: StrategyConfig) -> EntrySignal:
    """신규 진입 조건을 전부 평가한다.

    하나라도 막히면 진입하지 않는다. 각 필터가 왜 존재하는지는
    :class:`~koru_trade.config.StrategyConfig` 의 해당 필드 설명에 있다.

    Returns:
        모든 필터의 개별 결과를 담은 :class:`EntrySignal`.
    """
    checks: list[EntryCheck] = []

    need = cfg.warmup_bars
    if len(bars) < need:
        checks.append(
            EntryCheck(
                "데이터충분성",
                False,
                f"봉 {len(bars)}개로는 지표를 계산할 수 없다(최소 {need}개 필요)",
                float(len(bars)),
                float(need),
            )
        )
        return EntrySignal(tuple(checks))
    checks.append(
        EntryCheck("데이터충분성", True, f"봉 {len(bars)}개 확보", float(len(bars)), float(need))
    )

    last = bars[-1]
    closes = [b.close for b in bars]

    # 1. 변동성 레짐 --------------------------------------------------------
    atr_p = ind.atr_pct(bars, cfg.atr_period)
    if atr_p is None:
        checks.append(EntryCheck("변동성레짐", False, "ATR 을 계산할 수 없다"))
    else:
        ok = atr_p <= cfg.max_atr_pct
        checks.append(
            EntryCheck(
                "변동성레짐",
                ok,
                f"ATR({cfg.atr_period})/종가 = {atr_p:.2%} "
                f"(상한 {cfg.max_atr_pct:.2%}). "
                + (
                    "일중 진폭이 목표 익절폭을 삼킬 만큼 크다. 진입은 도박이 된다."
                    if not ok
                    else "익절/손절 폭이 일중 진폭 안에서 구분 가능하다."
                ),
                atr_p,
                cfg.max_atr_pct,
            )
        )

    # 2. 추세 방향 ----------------------------------------------------------
    ema_f = ind.ema(closes, cfg.ema_fast)
    ema_s = ind.ema(closes, cfg.ema_slow)
    if ema_f is None or ema_s is None:
        checks.append(EntryCheck("추세방향", False, "EMA 를 계산할 수 없다"))
    else:
        ok = last.close > ema_f and ema_f > ema_s
        checks.append(
            EntryCheck(
                "추세방향",
                ok,
                f"종가 ${last.close:.2f} / EMA{cfg.ema_fast} ${ema_f:.2f} / "
                f"EMA{cfg.ema_slow} ${ema_s:.2f} — "
                + ("정배열 상승 추세" if ok else "역배열 또는 이평 아래. 3배 상품은 역추세 금지"),
                last.close / ema_s - 1.0,
                0.0,
            )
        )

    # 3. 모멘텀 -------------------------------------------------------------
    rsi_v = ind.rsi(closes, cfg.rsi_period)
    if rsi_v is None:
        checks.append(EntryCheck("모멘텀(RSI)", False, "RSI 를 계산할 수 없다"))
    else:
        ok = cfg.min_rsi <= rsi_v <= cfg.max_rsi
        if rsi_v < cfg.min_rsi:
            why = "하락 모멘텀이 살아 있다(떨어지는 칼)"
        elif rsi_v > cfg.max_rsi:
            why = "과열 구간 추격 매수다"
        else:
            why = "허용 구간"
        checks.append(
            EntryCheck(
                "모멘텀(RSI)",
                ok,
                f"RSI({cfg.rsi_period}) = {rsi_v:.1f} "
                f"(허용 {cfg.min_rsi:.0f}~{cfg.max_rsi:.0f}) — {why}",
                rsi_v,
                cfg.min_rsi,
            )
        )

    # 4. 추세 강도 ----------------------------------------------------------
    adx_v = ind.adx(bars, 14)
    if adx_v is None:
        checks.append(EntryCheck("추세강도(ADX)", False, "ADX 를 계산할 수 없다"))
    else:
        ok = adx_v >= cfg.min_adx
        checks.append(
            EntryCheck(
                "추세강도(ADX)",
                ok,
                f"ADX(14) = {adx_v:.1f} (하한 {cfg.min_adx:.0f}) — "
                + ("추세 존재" if ok else "횡보장. 레버리지 감쇠만 먹는다"),
                adx_v,
                cfg.min_adx,
            )
        )

    # 5. 갭 -----------------------------------------------------------------
    gap = ind.gap_pct(bars)
    if gap is None:
        checks.append(EntryCheck("시가갭", True, "갭 정보 없음(첫 봉)"))
    else:
        ok = abs(gap) <= cfg.max_gap_pct
        checks.append(
            EntryCheck(
                "시가갭",
                ok,
                f"시가 갭 {gap:+.2%} (허용 절대값 {cfg.max_gap_pct:.2%}) — "
                + ("정상" if ok else "갭이 커서 지정가 체결 품질이 나쁘다"),
                gap,
                cfg.max_gap_pct,
            )
        )

    # 6. 유동성 -------------------------------------------------------------
    window = bars[-20:]
    avg_dollar_vol = sum(b.close * b.volume for b in window) / len(window)
    ok = avg_dollar_vol >= cfg.min_avg_dollar_volume
    checks.append(
        EntryCheck(
            "유동성",
            ok,
            f"20일 평균 거래대금 ${avg_dollar_vol:,.0f} (하한 ${cfg.min_avg_dollar_volume:,.0f})",
            avg_dollar_vol,
            cfg.min_avg_dollar_volume,
        )
    )

    # 7. 환율 추세 ----------------------------------------------------------
    lookback = cfg.fx_lookback_days
    if len(bars) > lookback:
        fx_now = last.fx_rate
        fx_then = bars[-(lookback + 1)].fx_rate
        fx_chg = fx_now / fx_then - 1.0
        ok = fx_chg >= cfg.max_fx_decline
        checks.append(
            EntryCheck(
                "환율추세",
                ok,
                f"USD/KRW {lookback}일 변화 {fx_chg:+.2%} "
                f"({fx_then:,.1f} -> {fx_now:,.1f}, 하한 {cfg.max_fx_decline:+.2%}) — "
                + (
                    "원화 강세 역풍. 목표 원화 수익률 달성에 주가가 그만큼 더 올라야 한다"
                    if not ok
                    else "환율 역풍이 허용 범위 내"
                ),
                fx_chg,
                cfg.max_fx_decline,
            )
        )
    else:
        checks.append(EntryCheck("환율추세", False, f"환율 이력이 {lookback + 1}개 미만이다"))

    # 8. 환율 수준 ----------------------------------------------------------
    if cfg.fx_max_level is not None:
        ok = last.fx_rate <= cfg.fx_max_level
        checks.append(
            EntryCheck(
                "환율수준",
                ok,
                f"현재 환율 {last.fx_rate:,.1f} (상한 {cfg.fx_max_level:,.1f}) — "
                + ("정상" if ok else "고환율 구간 진입은 환차손 위험이 크다"),
                last.fx_rate,
                cfg.fx_max_level,
            )
        )

    return EntrySignal(tuple(checks))


# ---------------------------------------------------------------------------
# 통합 판단
# ---------------------------------------------------------------------------


def decide(
    bars: Sequence[Bar],
    position: Position,
    risk_state: RiskState,
    cfg: StrategyConfig,
    *,
    now: dt.datetime | None = None,
) -> Decision:
    """현재 봉에서 무엇을 할지 결정한다.

    Args:
        bars: 시간 오름차순 봉 목록. 마지막이 현재 봉이다.
            **미래 봉을 넣으면 안 된다** — look-ahead bias 의 유일한 침입 경로다.
        position: 현재 포지션(없으면 빈 포지션).
        risk_state: 당일 리스크 상태.
        cfg: 전략 설정.
        now: 현재 시각. None 이면 마지막 봉의 타임스탬프를 쓴다.

    Returns:
        실행할 :class:`~koru_trade.models.Decision`.
    """
    if not bars:
        return Decision(Action.HOLD, rationale="봉 데이터가 없다")

    last = bars[-1]
    price = last.close
    fx = last.fx_rate
    ts = now or last.ts

    if position.is_open:
        return _decide_open(bars, position, risk_state, cfg, price, fx, ts)
    return _decide_flat(bars, position, risk_state, cfg, price, fx)


def _decide_open(
    bars: Sequence[Bar],
    position: Position,
    risk_state: RiskState,
    cfg: StrategyConfig,
    price: float,
    fx: float,
    ts: dt.datetime,
) -> Decision:
    """포지션 보유 중의 판단. 청산 조건을 익절보다 먼저 본다."""
    krw_r = position_krw_return(position, price, fx, cfg.cost)
    stop = stop_price_usd(position, cfg)
    metrics = {
        "krw_return": krw_r,
        "price_usd": price,
        "fx_rate": fx,
        "stop_price_usd": stop,
        "peak_krw_return": position.peak_krw_return,
        "qty": float(position.qty),
    }
    tp_hit = len(position.tp_levels_hit)

    # (1) 원화 하드 스톱 ----------------------------------------------------
    if krw_r <= cfg.hard_stop_krw_return:
        return Decision(
            Action.EXIT,
            qty=position.qty,
            reason=ExitReason.HARD_STOP_KRW,
            rationale=(
                f"원화 평가수익률 {krw_r:+.2%} 가 하드스톱 "
                f"{cfg.hard_stop_krw_return:+.2%} 이하다. 전량 청산한다"
            ),
            metrics=metrics,
        )

    # (2) USD 하드 스톱 -----------------------------------------------------
    if stop > 0 and price <= stop:
        return Decision(
            Action.EXIT,
            qty=position.qty,
            reason=ExitReason.HARD_STOP_USD,
            rationale=(
                f"현재가 ${price:.2f} 가 손절선 ${stop:.2f} "
                f"(1차 진입가 ${position.first_entry_price_usd:.2f} 기준) 이하다. 전량 청산한다"
            ),
            metrics=metrics,
        )

    # (3) 본전 스톱 (1단 익절 이후) -----------------------------------------
    if tp_hit >= cfg.breakeven_stop_after_tp > 0:
        breakeven = position_required_price_usd(position, fx, 0.0, cfg.cost)
        if price <= breakeven:
            return Decision(
                Action.EXIT,
                qty=position.qty,
                reason=ExitReason.BREAKEVEN_STOP,
                rationale=(
                    f"{tp_hit}단 익절 후 본전 스톱 발동. 현재가 ${price:.2f} <= "
                    f"원화 본전가 ${breakeven:.2f}. 이익을 손실로 되돌리지 않는다"
                ),
                metrics={**metrics, "breakeven_usd": breakeven},
            )

    # (4) 트레일링 스톱 (2단 익절 이후) -------------------------------------
    if tp_hit >= cfg.trailing_after_tp > 0 and position.peak_krw_return > 0:
        floor = position.peak_krw_return * (1.0 - cfg.trailing_giveback)
        if krw_r <= floor:
            return Decision(
                Action.EXIT,
                qty=position.qty,
                reason=ExitReason.TRAILING_STOP,
                rationale=(
                    f"최고 원화수익률 {position.peak_krw_return:+.2%} 대비 "
                    f"{cfg.trailing_giveback:.0%} 를 반납해 {krw_r:+.2%} 로 밀렸다"
                    f"(트레일 기준 {floor:+.2%}). 잔량 청산한다"
                ),
                metrics={**metrics, "trail_floor": floor},
            )

    # (5-a) 장 마감 전 청산 --------------------------------------------------
    # 오버나이트 갭을 피하는 단타 규칙. 봉 타임스탬프가 ET 벽시계라는 전제다.
    cutoff_min = cfg.close_minutes_before_session_end
    if cutoff_min is not None:
        # 봉 간격보다 좁은 여유는 의미가 없다. 1시간봉의 마지막 봉은 15:30 인데
        # 15:50 을 기준으로 잡으면 영영 발동하지 않고 밤새 들고 가게 된다.
        interval = bar_interval_minutes(bars) or 0.0
        margin = max(float(cutoff_min), interval)
        end = cfg.session_end_time
        cutoff = (dt.datetime.combine(ts.date(), end) - dt.timedelta(minutes=margin)).time()
        if ts.time() >= cutoff:
            return Decision(
                Action.EXIT,
                qty=position.qty,
                reason=ExitReason.TIME_STOP,
                rationale=(
                    f"장 마감({cfg.session_end_et} ET) 직전 봉이다"
                    f"(여유 {margin:.0f}분, 봉 간격 {interval:.0f}분). "
                    f"현재 원화수익률 {krw_r:+.2%}. 오버나이트 갭을 피해 전량 청산한다"
                ),
                metrics=metrics,
            )

    # (5-b) 타임 스톱 -------------------------------------------------------
    opened = position.opened_at
    if opened is not None:
        if cfg.max_holding_bars is not None:
            # 분봉에서는 영업일이 의미가 없다. 진입 봉 이후 지나간 봉을 센다.
            held_bars = _bars_since(bars, opened)
            if held_bars >= cfg.max_holding_bars:
                return Decision(
                    Action.EXIT,
                    qty=position.qty,
                    reason=ExitReason.TIME_STOP,
                    rationale=(
                        f"보유 {held_bars}봉으로 상한 {cfg.max_holding_bars}봉에 도달했다. "
                        f"현재 원화수익률 {krw_r:+.2%}. 레버리지 감쇠를 피해 청산한다"
                    ),
                    metrics={**metrics, "held_bars": float(held_bars)},
                )
        else:
            held = business_days_between(opened, ts)
            if held >= cfg.max_holding_days:
                return Decision(
                    Action.EXIT,
                    qty=position.qty,
                    reason=ExitReason.TIME_STOP,
                    rationale=(
                        f"보유 {held}영업일로 최대 보유기간 {cfg.max_holding_days}일에 도달했다. "
                        f"현재 원화수익률 {krw_r:+.2%}. 레버리지 감쇠를 피해 청산한다"
                    ),
                    metrics={**metrics, "held_days": float(held)},
                )

    # (6) 원화 분할 익절 계단 -----------------------------------------------
    triggered = tuple(
        i
        for i, step in enumerate(cfg.take_profit)
        if i not in position.tp_levels_hit and krw_r >= step.krw_return
    )
    if triggered:
        after = set(position.tp_levels_hit) | set(triggered)
        if len(after) >= len(cfg.take_profit):
            qty = position.qty
            tail = "마지막 계단이므로 잔량 전부를 청산한다"
        else:
            frac = sum(cfg.take_profit[i].sell_fraction for i in triggered)
            qty = min(position.qty, max(1, round(position.ladder_base_qty * frac)))
            tail = f"기준수량 {position.ladder_base_qty}주의 {frac:.0%}"
        levels = ", ".join(f"+{cfg.take_profit[i].krw_return:.0%}" for i in triggered)
        return Decision(
            Action.TAKE_PROFIT,
            qty=qty,
            reason=ExitReason.TAKE_PROFIT_LADDER,
            tp_levels=triggered,
            rationale=(
                f"원화 수익률 {krw_r:+.2%} 로 익절 계단 [{levels}] 도달. {qty}주 매도({tail})"
            ),
            metrics=metrics,
        )

    # (7) 분할 매수 ---------------------------------------------------------
    scale = _maybe_scale_in(bars, position, risk_state, cfg, price, fx, metrics)
    if scale is not None:
        return scale

    return Decision(
        Action.HOLD,
        rationale=(
            f"보유 유지. 원화수익률 {krw_r:+.2%}, 다음 익절 문턱 "
            f"{_next_tp_threshold(position, cfg)}, 손절선 ${stop:.2f}"
        ),
        metrics=metrics,
    )


def _maybe_scale_in(
    bars: Sequence[Bar],
    position: Position,
    risk_state: RiskState,
    cfg: StrategyConfig,
    price: float,
    fx: float,
    metrics: dict[str, float],
) -> Decision | None:
    """추가 분할 매수 여부를 판단한다. 조건 미충족이면 None."""
    if position.tp_levels_hit:
        return None  # 익절이 시작된 포지션에는 추가 매수하지 않는다
    idx = position.tranche_count
    if idx >= len(cfg.scale_in) or idx >= len(position.planned_tranche_qty):
        return None

    trigger = scale_in_trigger_price(position, idx, cfg)
    if price > trigger:
        return None

    stop = stop_price_usd(position, cfg)
    if price <= stop:
        return None  # 손절선 아래에서는 절대 추가 매수하지 않는다

    if cfg.scale_in_requires_reclaim:
        last = bars[-1]
        if last.close < last.open:
            return None  # 음봉(하락 지속) 중에는 받지 않는다

    qty = position.planned_tranche_qty[idx]
    if qty <= 0:
        return None

    notional = krw_cost(qty, price, fx, cfg.cost)
    blocked = check_scale_in_allowed(risk_state, cfg.risk, planned_notional_krw=notional)
    if blocked:
        return Decision(
            Action.BLOCKED,
            rationale=f"{idx + 1}차 분할매수가 리스크 한도에 걸렸다: {blocked}",
            metrics=metrics,
        )

    return Decision(
        Action.SCALE_IN,
        qty=qty,
        limit_price_usd=_limit_price(price, cfg, buying=True),
        rationale=(
            f"{idx + 1}차 분할매수 트리거 ${trigger:.2f} 도달(현재 ${price:.2f}). "
            f"{qty}주 추가 매수. 손절선 ${stop:.2f} 는 유지된다"
        ),
        metrics={**metrics, "trigger_usd": trigger, "tranche": float(idx)},
    )


def _decide_flat(
    bars: Sequence[Bar],
    position: Position,
    risk_state: RiskState,
    cfg: StrategyConfig,
    price: float,
    fx: float,
) -> Decision:
    """포지션이 없을 때의 판단."""
    signal = evaluate_entry(bars, cfg)
    metrics = {c.name_key: c.value for c in _keyed(signal.checks) if c.value is not None}
    metrics["price_usd"] = price
    metrics["fx_rate"] = fx

    if not signal.allowed:
        detail = " | ".join(f"[{c.name}] {c.detail}" for c in signal.blockers)
        return Decision(
            Action.HOLD,
            rationale=f"진입 보류 — {signal.summary}. {detail}",
            metrics=metrics,
        )

    plan = plan_entry(bars, cfg)
    if plan is None:
        return Decision(
            Action.HOLD,
            rationale=(
                f"진입 조건은 충족했으나 자본 {cfg.capital_krw:,.0f}원으로 "
                f"${price:.2f} 를 1주도 살 수 없거나 ATR 을 계산할 수 없다"
            ),
            metrics=metrics,
        )

    blocked = check_entry_allowed(
        risk_state,
        cfg.risk,
        planned_notional_krw=plan.total_notional_krw,
        open_positions=1 if position.is_open else 0,
    )
    if blocked:
        return Decision(
            Action.BLOCKED,
            rationale=f"진입 신호가 있었으나 리스크 한도에 걸렸다: {blocked}",
            metrics=metrics,
        )

    first_qty = plan.tranche_qty[0]
    return Decision(
        Action.ENTER,
        qty=first_qty,
        limit_price_usd=_limit_price(price, cfg, buying=True),
        entry_plan=plan,
        rationale=(
            f"{signal.summary}. 1차 {first_qty}주 매수 "
            f"(총 계획 {plan.total_qty}주 / {plan.tranche_qty}, "
            f"투입 예정 {plan.total_notional_krw:,.0f}원). "
            f"손절선 ${plan.stop_price_usd:.2f}, ATR ${plan.entry_atr_usd:.2f}"
        ),
        metrics={
            **metrics,
            "planned_qty": float(plan.total_qty),
            "stop_price_usd": plan.stop_price_usd,
            "entry_atr_usd": plan.entry_atr_usd,
        },
    )


def _limit_price(price: float, cfg: StrategyConfig, *, buying: bool) -> float:
    """지정가를 계산한다. 매수는 현재가보다 살짝 위, 매도는 살짝 아래로 낸다.

    체결 확률을 확보하기 위한 관행적 처리다. 폭은 ``limit_slippage_bps`` 로 조절한다.
    """
    adj = cfg.limit_slippage_bps / 10_000.0
    return round(price * (1.0 + adj) if buying else price * (1.0 - adj), 2)


def _next_tp_threshold(position: Position, cfg: StrategyConfig) -> str:
    """다음 익절 문턱값을 사람이 읽을 문자열로."""
    for i, step in enumerate(cfg.take_profit):
        if i not in position.tp_levels_hit:
            return f"+{step.krw_return:.0%}"
    return "없음(전 계단 소진)"


@dataclass(frozen=True, slots=True)
class _KeyedCheck:
    name_key: str
    value: float | None


def _keyed(checks: Sequence[EntryCheck]) -> list[_KeyedCheck]:
    """지표값을 metrics 사전에 넣기 위한 안전한 키 변환."""
    mapping = {
        "변동성레짐": "atr_pct",
        "추세방향": "trend_gap",
        "모멘텀(RSI)": "rsi",
        "추세강도(ADX)": "adx",
        "시가갭": "gap_pct",
        "유동성": "avg_dollar_volume",
        "환율추세": "fx_change",
        "환율수준": "fx_level",
        "데이터충분성": "bar_count",
    }
    return [_KeyedCheck(mapping.get(c.name, c.name), c.value) for c in checks]
