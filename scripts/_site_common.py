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
import re
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from koru_trade.config import StrategyConfig, load_strategy_config  # noqa: E402
from koru_trade.market_calendar import is_trading_day, next_trading_day  # noqa: E402

ACTIVE_CONFIG = ROOT / "config" / "strategy.yaml"
FALLBACK_CONFIG = ROOT / "config" / "frequent.example.yaml"
WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]
PLACEHOLDER = "__DATA__"

KST = ZoneInfo("Asia/Seoul")
NYT = ZoneInfo("America/New_York")
US_OPEN_ET = dt.time(9, 30)

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


def next_open_kst(now: dt.datetime | None = None) -> str:
    """다음 미국 정규장 개장 시각을 한국시간 ``"MM/DD(요일) HH:MM"`` 로.

    오늘이 거래일이고 아직 09:30 ET 전이면 오늘을 포함한다. 휴장일 판정은
    :mod:`koru_trade.market_calendar` 가 한다. 달력 없이 "평일이면 개장" 으로
    세다가 노동절을 평일로 안내한 적이 있다.

    Args:
        now: 기준 시각(tz 있는 값). None 이면 현재 시각. 테스트가 고정하려고 받는다.
    """
    now_ny = (now or dt.datetime.now(KST)).astimezone(NYT)
    inclusive = is_trading_day(now_ny.date()) and now_ny.time() < US_OPEN_ET
    day = next_trading_day(now_ny.date(), inclusive=inclusive)
    opens = dt.datetime.combine(day, US_OPEN_ET, NYT).astimezone(KST)
    return f"{opens:%m/%d}({WEEKDAY_KO[opens.weekday()]}) {opens:%H:%M}"


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
