"""알림 메시지 포매팅. 전부 순수 함수라 테스트할 수 있다.

메시지 설계 원칙
----------------
휴대폰 알림 한 줄로 **무엇을 해야 하는지**가 읽혀야 한다.
첫 줄에 행동과 종목, 둘째 줄에 수량과 가격, 그 다음이 맥락이다.
근거는 맨 아래로 민다 — 급할 때는 안 읽고, 여유 있을 때만 읽는 정보다.

색 관행은 한국 시장을 따른다. 🔴 매수(상승 기대) / 🔵 매도.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from koru_trade.config import StrategyConfig
from koru_trade.models import Action, Bar, Decision, ExitReason, Position
from koru_trade.pnl import krw_cost, krw_proceeds, position_krw_return, position_required_price_usd
from koru_trade.strategy import stop_price_usd

__all__ = [
    "format_blocked",
    "format_daily_summary",
    "format_decision",
    "format_error",
]

_ACTION_HEAD = {
    Action.ENTER: ("🔴", "매수 신호"),
    Action.SCALE_IN: ("🔴", "추가 매수"),
    Action.TAKE_PROFIT: ("🔵", "분할 익절"),
    Action.EXIT: ("⚠️", "청산"),
}

_EXIT_KO = {
    ExitReason.TAKE_PROFIT_LADDER: "익절 계단 도달",
    ExitReason.HARD_STOP_USD: "USD 손절선 도달",
    ExitReason.HARD_STOP_KRW: "원화 하드스톱 도달",
    ExitReason.TRAILING_STOP: "트레일링 스톱",
    ExitReason.BREAKEVEN_STOP: "본전 스톱",
    ExitReason.TIME_STOP: "보유기간 만료",
    ExitReason.REGIME_EXIT: "레짐 청산",
    ExitReason.KILL_SWITCH: "킬스위치",
    ExitReason.END_OF_BACKTEST: "백테스트 종료",
}


def _won(v: float) -> str:
    return f"{round(v):,}원"


def _won_signed(v: float) -> str:
    return f"{'+' if v >= 0 else ''}{round(v):,}원"


def format_decision(
    decision: Decision,
    bar: Bar,
    position: Position,
    cfg: StrategyConfig,
    *,
    dry_run: bool = True,
    now: dt.datetime | None = None,
) -> str:
    """매매 결정을 알림 본문으로.

    Args:
        decision: 전략이 낸 결정.
        bar: 판단 기준이 된 봉(현재가·환율).
        position: **결정 직전**의 포지션. 잔여 수량 계산에 쓴다.
        cfg: 전략 설정.
        dry_run: True 면 실제 주문이 나가지 않았음을 본문에 명시한다.
        now: 시각 표기용. None 이면 봉 시각.
    """
    icon, head = _ACTION_HEAD.get(decision.action, ("•", decision.action.value))
    ts = (now or bar.ts).strftime("%m/%d %H:%M")
    price = decision.limit_price_usd or bar.close
    amount = (
        krw_cost(decision.qty, price, bar.fx_rate, cfg.cost)
        if decision.action in (Action.ENTER, Action.SCALE_IN)
        else krw_proceeds(decision.qty, price, bar.fx_rate, cfg.cost)
    )

    lines = [
        f"{icon} <b>{head}</b> · {cfg.symbol}",
        f"<code>{ts}</code>",
        "",
        f"수량   <b>{decision.qty:,}주</b>",
        f"가격   ${price:,.2f}  (환율 {bar.fx_rate:,.0f})",
        f"금액   <b>{_won(amount)}</b>",
    ]

    if decision.action in (Action.ENTER, Action.SCALE_IN):
        lines += _entry_detail(decision, bar, position, cfg)
    else:
        lines += _exit_detail(decision, bar, position, cfg)

    if dry_run:
        lines += ["", "⚠️ DRY_RUN — 실제 주문은 나가지 않았다"]

    rationale = decision.rationale.strip()
    if rationale:
        lines += ["", f"<i>{_clip(rationale, 300)}</i>"]
    return "\n".join(lines)


def _entry_detail(
    decision: Decision, bar: Bar, position: Position, cfg: StrategyConfig
) -> list[str]:
    """매수 알림의 부가 정보 — 손절선과 첫 익절 목표가 가장 중요하다."""
    out: list[str] = [""]
    plan = decision.entry_plan
    if plan is not None:
        stop = plan.stop_price_usd
        out.append(f"손절선 ${stop:,.2f}  ({stop / bar.close - 1:+.1%})")
        out.append(
            f"계획   {' · '.join(f'{q}주' for q in plan.tranche_qty)} (총 {plan.total_qty:,}주)"
        )
        first = cfg.take_profit[0]
        target = bar.close * (1 + first.krw_return) / 1.0
        out.append(f"1단 익절 원화 {first.krw_return:+.0%} 부근 ${target:,.2f}")
    elif position.is_open:
        stop = stop_price_usd(position, cfg)
        if stop > 0:
            out.append(f"손절선 ${stop:,.2f} (유지)")
        nxt = _next_tp(position, bar, cfg)
        if nxt:
            out.append(nxt)
    return out


def _exit_detail(
    decision: Decision, bar: Bar, position: Position, cfg: StrategyConfig
) -> list[str]:
    """매도 알림의 부가 정보 — 실현손익과 잔여 수량."""
    out: list[str] = [""]
    if position.is_open:
        krw_r = position_krw_return(position, bar.close, bar.fx_rate, cfg.cost)
        sold = min(decision.qty, position.qty)
        share = sold / position.qty if position.qty else 0.0
        realized = (
            krw_proceeds(sold, decision.limit_price_usd or bar.close, bar.fx_rate, cfg.cost)
            - position.cost_krw * share
        )
        out.append(f"실현   <b>{_won_signed(realized)}</b>  (원화 {krw_r:+.2%})")
        left = position.qty - sold
        out.append(f"잔여   {left:,}주" + ("  · 전량 청산" if left == 0 else ""))
        if left:
            # 이번 결정으로 소진되는 계단은 "다음" 에서 빼야 한다.
            # 안 빼면 방금 판 계단을 다음 목표로 안내하게 된다.
            nxt = _next_tp(position, bar, cfg, consumed=set(decision.tp_levels))
            if nxt:
                out.append(nxt)
    if decision.reason is not None:
        out.append(f"사유   {_EXIT_KO.get(decision.reason, decision.reason.value)}")
    return out


def _next_tp(
    position: Position,
    bar: Bar,
    cfg: StrategyConfig,
    *,
    consumed: set[int] | None = None,
) -> str | None:
    """다음 익절 계단의 목표가.

    Args:
        consumed: 이번 결정으로 지금 막 소진되는 계단들.
            포지션의 ``tp_levels_hit`` 은 아직 갱신 전이므로 여기서 함께 제외한다.
    """
    done = set(position.tp_levels_hit) | (consumed or set())
    for i, step in enumerate(cfg.take_profit):
        if i in done:
            continue
        price = position_required_price_usd(position, bar.fx_rate, step.krw_return, cfg.cost)
        return f"다음 익절 원화 {step.krw_return:+.0%} = ${price:,.2f}"
    return None


def format_blocked(reason: str, cfg: StrategyConfig, *, now: dt.datetime | None = None) -> str:
    """리스크 한도로 매매가 막혔을 때. 조용히 넘어가면 안 되는 사건이다."""
    ts = (now or dt.datetime.now()).strftime("%m/%d %H:%M")
    return "\n".join(
        [
            f"🛑 <b>매매 차단</b> · {cfg.symbol}",
            f"<code>{ts}</code>",
            "",
            _clip(reason, 400),
            "",
            "<i>진입만 막힌다. 보유 포지션 청산은 계속 동작한다.</i>",
        ]
    )


def format_error(exc: BaseException, *, context: str = "") -> str:
    """봇이 예외로 죽었을 때. 알림이 없으면 멈춘 줄도 모른다."""
    where = f" ({context})" if context else ""
    return "\n".join(
        [
            f"❌ <b>봇 오류</b>{where}",
            "",
            f"<code>{_clip(type(exc).__name__ + ': ' + str(exc), 500)}</code>",
            "",
            "<i>매매가 중단됐을 수 있다. 로그를 확인하라.</i>",
        ]
    )


def format_daily_summary(
    stats: dict[str, Any], cfg: StrategyConfig, *, now: dt.datetime | None = None
) -> str:
    """하루 마감 요약."""
    ts = (now or dt.datetime.now()).strftime("%m/%d")
    pnl = float(stats.get("realized_krw_today", 0.0))
    lines = [
        f"📊 <b>{ts} 마감</b> · {cfg.symbol}",
        "",
        f"실현손익 <b>{_won_signed(pnl)}</b>",
        f"진입     {stats.get('entries_today', 0)}회",
        f"보유     {stats.get('position_qty', 0):,}주",
    ]
    if stats.get("in_cooldown"):
        lines.append(f"쿨다운   {stats.get('cooldown_until')} 까지")
    return "\n".join(lines)


def _clip(text: str, limit: int) -> str:
    """텔레그램 메시지 상한(4096자)을 넘지 않도록 자른다."""
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
