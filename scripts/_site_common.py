"""사이트 생성기 공통 로직.

``build_site.py``(신호 원장)와 ``build_desk.py``(상황실)가 공유한다.
두 페이지는 내용이 전혀 다르지만 **껍데기는 같다**: 어떤 설정을 쓸지 정하고,
템플릿의 ``__DATA__`` 자리에 JSON 을 끼워 넣고, 끼워 넣은 결과가 실제로
파싱되는지 확인한다.

이 셋을 한 곳에 둔 이유는 규칙이 바뀔 때 한쪽만 고치는 일을 막기 위해서다.
특히 ``resolve_config`` 의 대체 규칙은 로컬과 클라우드에서 다른 수치가 나오는
원인이 됐던 부분이라, 두 생성기가 반드시 같은 판단을 해야 한다.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from koru_trade import indicators as ind  # noqa: E402
from koru_trade.config import StrategyConfig, load_strategy_config  # noqa: E402
from koru_trade.data.repair import session_end  # noqa: E402
from koru_trade.market_calendar import next_trading_day  # noqa: E402
from koru_trade.models import Bar  # noqa: E402

ACTIVE_CONFIG = ROOT / "config" / "strategy.yaml"
FALLBACK_CONFIG = ROOT / "config" / "frequent.example.yaml"
WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]
PLACEHOLDER = "__DATA__"

KST = ZoneInfo("Asia/Seoul")
NYT = ZoneInfo("America/New_York")
US_OPEN_ET = dt.time(9, 30)

SITE_REFRESH_KST = dt.time(11, 40)
"""공개 사이트가 자동으로 다시 만들어지는 시각(작업 스케줄러 "KORU site").

페이지가 "새 판정은 몇 시쯤 반영된다" 고 안내할 때 쓴다. 등록 시각을 바꾸면
``scripts/update_site.bat`` 머리의 등록 명령과 AGENTS.md 8절도 같이 바꿔라.
"""

SITE_PERIOD = "3y"
"""세 페이지가 받는 시세 기간. 반드시 같아야 한다.

30일 평균은 처음 값을 단순평균으로 심기 때문에 받은 기간이 다르면 값이 조금 다르고,
다음 종가 문턱(:func:`threshold_close`)은 그 차이를 여러 배로 키운다. 한눈에만 6개월치를
받던 시절 같은 날 한눈에 $21.53, 신호 원장 $21.57 로 문턱이 갈렸고, 그 사이 종가면
한 페이지는 "사도 된다", 다른 페이지는 "차단" 이 될 수 있었다.
"""

CLOSE_SETTLE = dt.timedelta(minutes=20)
"""장 마감 뒤 이만큼 지나야 그날 일봉을 확정으로 본다.

16:00 종가 단일가 결과가 시세 제공자에 반영되기까지 몇 분 걸린다. 그 사이에 받은
종가는 마지막 체결가일 수 있다(:mod:`koru_trade.data.repair` 의 실측표 참고).
"""

PAYLOAD_RE = re.compile(r'<script id="payload" type="application/json">(.*?)</script>', re.S)

PUBLIC_CAPITAL_KRW = 1_000_000.0
"""공개 페이지에서 쓰는 기준 자본. 실제 운용액 대신 이 값으로 환산한다."""


def anonymize(cfg: StrategyConfig) -> StrategyConfig:
    """공개용 설정. 실제 운용액을 기준 자본으로 바꾼다.

    페이지에는 계좌번호도 토큰도 없지만 ``capital_krw`` 와
    ``risk.max_position_krw`` 는 사용자가 얼마를 굴리는지를 그대로 드러낸다.
    수량·투입금액·실현손익이 전부 여기서 파생되므로 한 곳만 바꾸면 된다.

    비율은 보존된다. 전략의 모든 문턱이 수익률 기준이라 100만원으로 환산해도
    판정과 계단은 똑같이 읽힌다. 주수만 실제와 달라진다.
    """
    import dataclasses as _dc

    scale = PUBLIC_CAPITAL_KRW / cfg.capital_krw
    return _dc.replace(
        cfg,
        capital_krw=PUBLIC_CAPITAL_KRW,
        risk=_dc.replace(
            cfg.risk,
            max_position_krw=cfg.risk.max_position_krw * scale,
            max_daily_notional_krw=cfg.risk.max_daily_notional_krw * scale,
            daily_loss_limit_krw=cfg.risk.daily_loss_limit_krw * scale,
        ),
    )


def resolve_config(explicit: str | None) -> tuple[StrategyConfig, Path]:
    """쓸 설정 파일을 정한다. 어느 것을 썼는지 호출부가 출력할 수 있게 경로도 준다.

    ``config/strategy.yaml`` 은 .gitignore 에 있다. 즉 저장소를 새로 받은 환경
    (클라우드 루틴 등)에는 그 파일이 없고, 그냥 내장 기본값으로 돌아가면 로컬과
    다른 수치가 조용히 나온다. 그래서 없으면 **커밋되어 있는** 프리셋으로 대체한다.
    """
    if explicit:
        path = Path(explicit)
    elif ACTIVE_CONFIG.exists():
        path = ACTIVE_CONFIG
    else:
        path = FALLBACK_CONFIG
    if not path.exists():
        raise SystemExit(f"설정 파일이 없다: {path}")
    return load_strategy_config(path), path


def kst_label(moment: dt.datetime) -> str:
    """tz 있는 시각을 한국시간 ``"MM/DD(요일) HH:MM"`` 으로."""
    k = moment.astimezone(KST)
    return f"{k:%m/%d}({WEEKDAY_KO[k.weekday()]}) {k:%H:%M}"


def session_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """그 거래일 정규장의 개장·마감 시각(tz 있는 ET). 조기 폐장일은 13:00 에 닫힌다."""
    return (
        dt.datetime.combine(day, US_OPEN_ET, NYT),
        dt.datetime.combine(day, session_end(day), NYT),
    )


def verdict_schedule(bar_day: dt.date) -> dict[str, str]:
    """``bar_day`` 종가로 낸 판정이 걸린 장, 그 장이 끝나는 시각, 새 판정이 반영되는 시각.

    판정은 확정 종가로 내리고 **그다음 거래일 개장**에 지정가로 산다. 그래서 "다음 개장"
    은 페이지를 만든 시각이 아니라 판정의 기준 봉에서 센다. 만든 시각에서 세던 시절,
    제공자가 종가를 늦게 채운 날이나 장중에 다시 만든 날에는 지난 판정을 **다음 날
    개장** 에 붙여 안내할 수 있었다.

    반환값의 ``*At`` 은 한국시간 ISO 문자열이다. 페이지가 보는 사람의 시계와 비교해
    "이 판정의 매수 시점이 지났나" 를 스스로 가린다. 판정 자체는 하루에 한 번만 바뀌는데
    페이지는 그 사이 내내 열려 있기 때문이다. 휴장일과 조기 폐장은
    :mod:`koru_trade.market_calendar` 가 판정한다(노동절을 평일로 안내한 적이 있다).

    Returns:
        ``openAt``/``open``: 판정이 걸린 장의 개장. ``closeAt``/``close``: 그 장의 마감.
        ``refreshAt``/``refresh``: 그 마감 뒤 첫 자동 갱신. ``afterOpen``: 그다음 장의 개장.
    """
    day = next_trading_day(bar_day)
    opens, closes = session_bounds(day)
    closes_kst = closes.astimezone(KST)
    refresh = dt.datetime.combine(closes_kst.date(), SITE_REFRESH_KST, KST)
    if refresh < closes_kst:
        refresh += dt.timedelta(days=1)
    after_open, _ = session_bounds(next_trading_day(day))
    return {
        "openAt": opens.astimezone(KST).isoformat(),
        "open": kst_label(opens),
        "closeAt": closes_kst.isoformat(),
        "close": kst_label(closes),
        "refreshAt": refresh.isoformat(),
        "refresh": kst_label(refresh),
        "afterOpen": kst_label(after_open),
    }


def confirmed_bars(bars: Sequence[Bar], now: dt.datetime | None = None) -> tuple[Bar, ...]:
    """장이 아직 안 끝난(형성 중인) 마지막 일봉을 뺀다.

    야후 일봉은 장중에도, 심지어 프리장에도 오늘 줄을 실시간 값으로 채워 준다. 그 줄을
    그대로 쓰면 "확정 종가 기준 판정" 이라고 적어 놓고 장중 가격으로 판정하게 된다 —
    2026-09-28 밤 장중에 다시 만들면 $20.00 짜리 형성 중인 봉이 판정 기준이 됐을 것이다.
    예약 갱신(11:40 KST)은 미국장이 닫힌 뒤라 괜찮지만, 손으로 돌리는 빌드는 언제든 돈다.

    그날 마감 시각에 :data:`CLOSE_SETTLE` 를 더한 뒤부터 확정으로 본다.

    Args:
        bars: 시간 오름차순 일봉. 타임스탬프의 날짜가 미국 거래일이다.
        now: 기준 시각(tz 있는 값). None 이면 현재 시각. 테스트가 고정하려고 받는다.
    """
    now_ny = (now or dt.datetime.now(KST)).astimezone(NYT)
    kept = list(bars)
    while kept:
        _, closes = session_bounds(kept[-1].ts.date())
        if now_ny >= closes + CLOSE_SETTLE:
            break
        kept.pop()
    return tuple(kept)


def render(payload: dict[str, Any], template: Path, out: Path) -> Path:
    """템플릿의 ``__DATA__`` 자리에 데이터를 끼워 넣어 HTML 을 쓴다.

    쓰고 나서 **다시 읽어 파싱까지 한다.** 깨진 JSON 을 끼워 넣으면 브라우저는
    오류를 보여주지 않고 화면만 통째로 비운다. 발행한 뒤에는 알아채기 어렵다.
    """
    text = template.read_text(encoding="utf-8")
    if text.count(PLACEHOLDER) != 1:
        raise SystemExit(f"템플릿에 {PLACEHOLDER} 가 정확히 1개 있어야 한다: {template}")

    # allow_nan=False: 파이썬 json 은 NaN/Infinity 를 그대로 뱉지만 브라우저의
    # JSON.parse 는 거부한다. 값 하나 때문에 페이지 전체가 백지가 된다.
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    if "</script" in data.lower():
        raise SystemExit("데이터에 script 종료 태그가 들어가 페이지가 깨진다")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text.replace(PLACEHOLDER, data), encoding="utf-8", newline="\n")

    written = out.read_text(encoding="utf-8")
    match = PAYLOAD_RE.search(written)
    if match is None:
        raise SystemExit("발행 파일에서 payload 블록을 찾지 못했다")
    json.loads(match.group(1))
    return out


def cost_payload(cfg: StrategyConfig) -> dict[str, float]:
    """페이지가 원화 손익을 계산할 때 읽는 비용 블록. 상황실과 한눈에가 같이 쓴다.

    페이지의 ``heldCost``/``heldProceeds`` 는 :func:`koru_trade.pnl.krw_cost` /
    :func:`~koru_trade.pnl.krw_proceeds` 를 그대로 옮긴 식이다. 매수측만 보내던
    시절에는 페이지가 그 한 쌍을 매도측에도 써서 본전·손절·익절 가격이 저장소 엔진과
    갈라졌다(대칭 설정에서도 0.29%p). 스프레드는 반드시 ``effective_*`` 를 보낸다 —
    ``fx_mode`` 가 HOLD_USD 면 환전을 안 하므로 원시 스프레드를 보내면 없는 비용을
    계산하게 된다(왕복 0.641% vs 0.143%).
    """
    c = cfg.cost
    return {
        "fee": round(c.buy_fee_rate, 6),
        "sellFee": round(c.sell_fee_rate, 6),
        "secFee": round(c.sec_fee_rate, 8),
        "taf": round(c.finra_taf_per_share, 8),
        "slip": round(c.slippage_rate, 6),
        "fxSpread": round(c.effective_buy_spread, 6),
        "fxSpreadSell": round(c.effective_sell_spread, 6),
        # round_trip_drag 는 수수료와 환전 스프레드만 센다. krw_cost/krw_proceeds 가
        # 슬리피지를 참조하지 않기 때문이다(AGENTS.md 도메인 함정). 가상의 진입·청산을
        # 말할 때는 슬리피지 왕복분을 더해야 실전과 같다(0.641% 가 아니라 0.939%).
        "roundTrip": round(c.round_trip_drag + 2.0 * c.slippage_rate, 5),
    }


def sell_rules(cfg: StrategyConfig) -> dict[str, Any]:
    """들고 있을 때 파는 규칙. 페이지의 ``sellPlan`` 이 이 값만 읽는다.

    ``strategy.decide`` 의 청산 문턱과 같다: 원화 하드 스톱, 달러 손절(1차 진입가 −
    ``stopAtr``×ATR, ``minStopPct``~``maxStopPct`` 로 클램프 — 둘 중 높은 쪽이 먼저
    걸린다), 이익 중 추세 꺾임(``trendFloor`` 초과 + 종가가 빠른 평균 아래), 타임
    스톱(``maxHold`` 영업일), 원화 익절 계단(``ladder`` — 비율은 첫 익절 전 수량 기준,
    마지막 계단은 잔량 전부).

    본전 스톱(1단 익절 뒤)·트레일링(2단 뒤)은 싣지 않는다. 운용 설정은 보유 1일이라
    그 둘이 걸리는 종가에서는 타임 스톱이 먼저 전량을 판다(3년 백테스트에서 둘을 꺼도
    결과가 비트 단위로 같았다). 보유 기간을 늘리면 여기에 더해야 한다.
    """
    return {
        "ladder": [{"k": s.krw_return, "f": s.sell_fraction} for s in cfg.take_profit],
        "hardStop": cfg.hard_stop_krw_return,
        "stopAtr": cfg.stop_atr_multiple,
        "minStopPct": cfg.min_stop_pct,
        "maxStopPct": cfg.max_stop_pct,
        "trendFloor": cfg.trend_break_min_krw_return,
        "maxHold": cfg.max_holding_days,
    }


def cent_below(value: float) -> float:
    """``value`` 보다 **엄격히** 작은 가장 큰 센트.

    추세 꺾임은 "종가 < 빠른 평균" 이라 평균값 자체에서는 걸리지 않는다. 평균을
    반올림해 적으면($21.0251 → $21.03) 그 값으로 끝난 날 걸린다고 잘못 안내하게 된다.
    """
    return (math.ceil(round(value * 100, 6)) - 1) / 100


def threshold_close(bars: Any, cfg: StrategyConfig) -> float:
    """다음 봉이 이 값 이상으로 마감해야 추세 정배열이 성립하는 종가.

    조건이 두 개(종가 > 빠른 평균, 빠른 평균 > 느린 평균)라 빠른 평균값 하나로는
    답이 안 나온다. 빠른 평균이 느린 평균 아래에 있으면 둘을 뒤집을 만큼 더
    올라야 한다 — 2026-09-24 밤 빠른 평균은 $21.08 이었지만 실제 필요 종가는
    $21.39 였다. 한눈에 페이지가 빠른 평균값을 기준가로 적던 시절 이 차이를
    틀리게 보여줬다. 그래서 두 생성기가 이 함수 하나를 쓴다.

    EMA 는 재귀식이라 닫힌 해를 쓸 수도 있지만 이분법이 더 읽기 쉽고
    필터 변경에도 견딘다.
    """
    closes = [b.close for b in bars]
    fast = ind.ema(closes, cfg.ema_fast)
    slow = ind.ema(closes, cfg.ema_slow)
    if fast is None or slow is None:
        return 0.0
    a_f = 2.0 / (cfg.ema_fast + 1)
    a_s = 2.0 / (cfg.ema_slow + 1)
    lo, hi = 1.0, 200.0
    for _ in range(90):
        mid = (lo + hi) / 2
        ema_f = mid * a_f + fast * (1 - a_f)
        ema_s = mid * a_s + slow * (1 - a_s)
        if mid > ema_f and ema_f > ema_s:
            hi = mid
        else:
            lo = mid
    return hi
