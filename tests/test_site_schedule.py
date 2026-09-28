"""사이트 판정이 "어느 장에 대한 것인지" 를 지키는 테스트.

2026-09-28 밤, 금요일 종가로 낸 "사도 된다" 가 월요일 장이 한창일 때도 한눈에
페이지에 그대로 떠 있어서 "지금 더 사라" 로 읽혔다. 원인은 세 가지였다.

1. "다음 개장" 을 **페이지를 만든 시각** 에서 셌다. 판정은 확정 종가로 내리고 그다음
   거래일 개장에 사는 것이므로, 기준 봉에서 세야 한다.
2. 판정이 걸린 개장이 지나도 페이지가 그걸 몰랐다. 판정은 하루에 한 번 바뀌는데
   페이지는 그 사이 내내 열려 있다.
3. 장중에 빌드하면 야후가 주는 형성 중인 오늘 봉을 "확정 종가" 로 썼을 것이다.

네트워크는 쓰지 않는다. 자바스크립트 단계 판정은 node 가 있으면 실제로 돌려 본다.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from tests.conftest import make_series

from koru_trade.config import StrategyConfig
from koru_trade.market_calendar import is_trading_day, next_trading_day
from koru_trade.models import Bar
from koru_trade.strategy import _limit_price, entry_limit_price

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_desk  # noqa: E402
import build_glance  # noqa: E402
import build_public  # noqa: E402
import build_site  # noqa: E402
from _site_common import (  # noqa: E402
    CLOSE_SETTLE,
    PAYLOAD_RE,
    SITE_PERIOD,
    SITE_REFRESH_KST,
    confirmed_bars,
    verdict_schedule,
)

KST = ZoneInfo("Asia/Seoul")
NYT = ZoneInfo("America/New_York")
TEMPLATES = {
    "glance": build_glance.TEMPLATE,
    "desk": build_desk.TEMPLATE,
    "signals": build_site.TEMPLATE,
}


def _bars_ending(*days: dt.date) -> tuple[Bar, ...]:
    """주어진 날짜들로 일봉을 만든다. 로더처럼 자정 타임스탬프에 미국 거래일 날짜를 쓴다."""
    return tuple(
        Bar(
            ts=dt.datetime.combine(d, dt.time()),
            open=20.0,
            high=21.0,
            low=19.0,
            close=20.0 + i * 0.1,
            volume=1_000_000.0,
            fx_rate=1_360.0,
        )
        for i, d in enumerate(days)
    )


# ---------------------------------------------------------------------------
# verdict_schedule: 판정이 걸린 장
# ---------------------------------------------------------------------------


class TestVerdictSchedule:
    def test_금요일_종가_판정은_월요일_밤_개장에_걸린다(self) -> None:
        """2026-09-25(금) 종가 $21.59 로 낸 판정. 사용자가 실제로 헷갈린 그 판정이다."""
        w = verdict_schedule(dt.date(2026, 9, 25))
        assert w["open"] == "09/28(월) 22:30"
        assert w["openAt"] == "2026-09-28T22:30:00+09:00"
        assert w["close"] == "09/29(화) 05:00"
        assert w["closeAt"] == "2026-09-29T05:00:00+09:00"
        assert w["refresh"] == "09/29(화) 11:40"
        assert w["afterOpen"] == "09/29(화) 22:30"

    def test_만든_시각과_상관없이_기준_봉에서_센다(self) -> None:
        """같은 봉이면 언제 만들어도 같은 장을 가리킨다. 시각 인자 자체가 없다."""
        assert verdict_schedule(dt.date(2026, 9, 25)) == verdict_schedule(dt.date(2026, 9, 25))
        assert "now" not in verdict_schedule.__code__.co_varnames

    def test_휴장일을_건너뛴다(self) -> None:
        """2026-09-07 은 노동절이다. 달력 없이 "평일이면 개장" 으로 세다가
        "월요일이면 판가름난다" 고 잘못 안내한 적이 있다.
        """
        w = verdict_schedule(dt.date(2026, 9, 4))
        assert w["open"] == "09/08(화) 22:30"
        assert w["afterOpen"] == "09/09(수) 22:30"

    def test_서머타임이_끝나면_한국시간이_한_시간_늦어진다(self) -> None:
        """2026-11-01 에 미국 서머타임이 끝난다. 10/30(금) 판정은 11/02(월) 23:30 개장이다."""
        w = verdict_schedule(dt.date(2026, 10, 30))
        assert w["open"] == "11/02(월) 23:30"
        assert w["close"] == "11/03(화) 06:00"
        assert w["refresh"] == "11/03(화) 11:40"

    def test_조기_폐장일은_13시에_닫힌다(self) -> None:
        """11/25(수) 판정의 다음 장은 추수감사절 다음 날 11/27(금), 13:00 ET 조기 폐장이다."""
        w = verdict_schedule(dt.date(2026, 11, 25))
        assert w["open"] == "11/27(금) 23:30"
        assert w["close"] == "11/28(토) 03:00"
        assert w["refresh"] == "11/28(토) 11:40"
        assert w["afterOpen"] == "11/30(월) 23:30"

    def test_1년치_모든_거래일에서_시각_순서가_맞다(self) -> None:
        """개장 < 마감 <= 갱신 < 그다음 개장, 장 길이는 6.5시간 또는 조기 폐장 3.5시간."""
        day = dt.date(2026, 1, 2)
        checked = 0
        while day.year == 2026:
            if is_trading_day(day):
                w = verdict_schedule(day)
                opens = dt.datetime.fromisoformat(w["openAt"])
                closes = dt.datetime.fromisoformat(w["closeAt"])
                refresh = dt.datetime.fromisoformat(w["refreshAt"])
                session = next_trading_day(day)
                assert opens.astimezone(NYT) == dt.datetime.combine(session, dt.time(9, 30), NYT), (
                    day
                )
                assert closes - opens in (dt.timedelta(hours=6.5), dt.timedelta(hours=3.5)), day
                assert closes <= refresh < closes + dt.timedelta(days=1), day
                assert refresh.astimezone(KST).time() == SITE_REFRESH_KST, day
                assert opens.utcoffset() == dt.timedelta(hours=9), "한국시간 ISO 여야 한다"
                checked += 1
            day += dt.timedelta(days=1)
        assert checked > 240


# ---------------------------------------------------------------------------
# confirmed_bars: 형성 중인 봉 빼기
# ---------------------------------------------------------------------------


class TestConfirmedBars:
    FRI = dt.date(2026, 9, 25)
    MON = dt.date(2026, 9, 28)

    def test_장중에는_오늘_봉을_뺀다(self) -> None:
        """2026-09-29 01:00 KST = 9/28(월) 12:00 ET, 장이 한창이다. 그 봉은 $20.00 짜리 장중 값이었다."""
        bars = _bars_ending(self.FRI, self.MON)
        now = dt.datetime(2026, 9, 29, 1, 0, tzinfo=KST)
        kept = confirmed_bars(bars, now)
        assert kept[-1].ts.date() == self.FRI
        assert len(kept) == 1

    def test_프리장에_딸려_온_오늘_봉도_뺀다(self) -> None:
        """야후는 프리장(17:00 KST~)에도 오늘 줄을 만들어 준다."""
        bars = _bars_ending(self.FRI, self.MON)
        now = dt.datetime(2026, 9, 28, 18, 30, tzinfo=KST)
        assert confirmed_bars(bars, now)[-1].ts.date() == self.FRI

    def test_마감_직후_정산_여유_안에서는_아직_뺀다(self) -> None:
        bars = _bars_ending(self.FRI, self.MON)
        close = dt.datetime(2026, 9, 28, 16, 0, tzinfo=NYT)
        assert (
            confirmed_bars(bars, close + CLOSE_SETTLE - dt.timedelta(minutes=1))[-1].ts.date()
            == self.FRI
        )
        assert confirmed_bars(bars, close + CLOSE_SETTLE)[-1].ts.date() == self.MON

    def test_조기_폐장일은_13시_기준이다(self) -> None:
        """11/27 은 13:00 ET 에 닫힌다. 16:00 기준으로 세면 세 시간 동안 확정 봉을 버린다."""
        day = dt.date(2026, 11, 27)
        bars = _bars_ending(dt.date(2026, 11, 25), day)
        early = dt.datetime.combine(day, dt.time(13, 0), NYT)
        assert confirmed_bars(bars, early + dt.timedelta(minutes=5))[-1].ts.date() != day
        assert confirmed_bars(bars, early + CLOSE_SETTLE)[-1].ts.date() == day

    def test_어제_봉까지만_있으면_아무것도_안_뺀다(self) -> None:
        """오늘 봉이 아직 안 왔으면(프리장 전) 받은 그대로가 확정이다."""
        bars = _bars_ending(dt.date(2026, 9, 24), self.FRI)
        now = dt.datetime(2026, 9, 29, 1, 0, tzinfo=KST)
        assert confirmed_bars(bars, now) == bars

    def test_빈_입력은_빈_튜플이다(self) -> None:
        assert confirmed_bars((), dt.datetime(2026, 9, 29, tzinfo=KST)) == ()


# ---------------------------------------------------------------------------
# 지정가: 봇과 같은 값
# ---------------------------------------------------------------------------


class TestEntryLimit:
    def test_봇이_내는_지정가와_같은_함수다(self, cfg: StrategyConfig) -> None:
        """사이트가 식을 복사해 두면 limit_slippage_bps 를 바꿀 때 한쪽만 바뀐다."""
        for close in (19.27, 20.0, 21.59, 63.24):
            assert entry_limit_price(close, cfg) == _limit_price(close, cfg, buying=True)

    def test_종가보다_설정한_만큼_위다(self, cfg: StrategyConfig) -> None:
        """09-25 종가 $21.59, 15bp 면 $21.62 다. 대화에서 안내한 값과 같아야 한다."""
        c = StrategyConfig(limit_slippage_bps=15.0)
        assert entry_limit_price(21.59, c) == 21.62
        assert entry_limit_price(21.59, cfg) >= 21.59


# ---------------------------------------------------------------------------
# 페이로드: 세 페이지가 같은 판정 시각을 싣는다
# ---------------------------------------------------------------------------


@pytest.fixture
def series() -> tuple[Bar, ...]:
    return make_series(150, drift=0.0012, amplitude=0.012)


class TestPayloads:
    def test_한눈에_페이로드가_기준_봉의_장과_지정가를_싣는다(
        self, series: tuple[Bar, ...], cfg: StrategyConfig
    ) -> None:
        payload = build_glance.build_payload(series, cfg)
        last = series[-1]
        assert payload["when"] == verdict_schedule(last.ts.date())
        assert payload["limit"] == entry_limit_price(last.close, cfg)
        assert payload["bar"] == last.ts.strftime("%Y-%m-%d")
        assert re.fullmatch(r"\d{2}/\d{2}\([월화수목금토일]\)", payload["barDay"])
        assert "nextOpen" not in payload, "만든 시각에서 세던 옛 필드가 남아 있다"

    def test_상황실_페이로드가_같은_장을_가리킨다(
        self, series: tuple[Bar, ...], cfg: StrategyConfig
    ) -> None:
        glance = build_glance.build_payload(series, cfg)
        desk = build_desk.build_payload(series, cfg, 0.039)
        assert desk["when"] == glance["when"]
        assert desk["limit"] == glance["limit"]
        assert desk["barDay"] == glance["barDay"]

    def test_신호_원장_페이로드가_같은_장과_실제_검사표를_싣는다(
        self, series: tuple[Bar, ...], cfg: StrategyConfig
    ) -> None:
        glance = build_glance.build_payload(series, cfg)
        site = build_site.build_payload(series, cfg)
        assert site["when"] == glance["when"]
        assert site["next"]["limit"] == glance["limit"]
        cur = site["current"]
        assert [c["ok"] for c in cur["checks"]] == [c["ok"] for c in glance["checks"]]
        assert cur["allowed"] is all(c["ok"] for c in cur["checks"])
        assert all(isinstance(c["ok"], bool) for c in cur["checks"])

    def test_한눈에_페이로드가_막힌_까닭을_가른다(
        self, series: tuple[Bar, ...], cfg: StrategyConfig
    ) -> None:
        """추세만 막혔으면 종가 문턱으로 풀리고, 다른 조건이 막았으면 그것부터 풀려야 한다.

        이 둘을 섞어 "종가가 $X 이상이면 사도 된다로 바뀐다" 고 적으면, RSI 나 유동성처럼
        며칠씩 이어지는 차단이 걸린 날에는 오지 않을 전환을 약속하게 된다.
        """
        payload = build_glance.build_payload(series, cfg)
        failing = [c["plain"] for c in payload["checks"] if not c["ok"]]
        trend_plain = build_glance.PLAIN[build_glance.TREND_CHECK]
        assert payload["trendOk"] is (trend_plain not in failing)
        assert payload["others"] == [f for f in failing if f != trend_plain]

    def test_종가가_빠른_평균_위인데_추세가_막힌_날은_이유를_다르게_말한다(self) -> None:
        """09/21 처럼 종가는 빠른 평균 위인데 빠른 평균이 느린 평균 아래인 날.

        기본 문구 "값이 최근 10일 평균보다 아래" 는 그날 틀린 말이다.
        """
        assert build_glance._why("추세방향", above_line=True) == build_glance.TREND_ABOVE_LINE
        assert "아래라는 뜻" in build_glance._why("추세방향", above_line=False)
        assert build_glance._why("유동성", above_line=True) == build_glance.WHY["유동성"]


class TestSharedHistory:
    def test_세_페이지가_같은_기간의_시세를_받는다(self) -> None:
        """기간이 다르면 30일 평균의 첫 값이 달라지고, 문턱이 페이지마다 몇 센트씩 갈린다.

        한눈에만 6개월치를 받던 시절 같은 날 $21.53 과 $21.57 이 나왔다. 그 사이 종가면
        한 페이지는 "사도 된다", 다른 페이지는 "차단" 이다.
        """
        assert SITE_PERIOD == "3y"
        for module in (build_glance, build_desk, build_site):
            text = Path(module.__file__ or "").read_text(encoding="utf-8")
            assert "default=SITE_PERIOD" in text, module.__name__

    def test_같은_봉이면_세_페이지의_판정과_문턱이_같다(
        self, series: tuple[Bar, ...], cfg: StrategyConfig
    ) -> None:
        glance = build_glance.build_payload(series, cfg)
        site = build_site.build_payload(series, cfg)
        assert glance["flipAt"] == site["next"]["thresholdClose"]
        assert glance["allowed"] is site["current"]["allowed"]

    def test_공개_빌드는_세_페이지의_기준_봉이_다르면_발행하지_않는다(self) -> None:
        """생성기마다 제 시계로 형성 중인 봉을 뺀다. 마감 직후에 돌리면 갈릴 수 있다."""
        same = {"index.html": "2026-09-25", "desk.html": "2026-09-25", "signals.html": "2026-09-25"}
        assert build_public.same_bar(same) == "2026-09-25"
        assert build_public.same_bar({**same, "desk.html": "?"}) == "2026-09-25"
        with pytest.raises(SystemExit):
            build_public.same_bar({**same, "signals.html": "2026-09-28"})


class TestBuildersDropFormingBar:
    """생성기 main() 이 실제로 형성 중인 봉을 빼는지. 도우미 함수만 시험하면 호출을 지워도 모른다."""

    @pytest.fixture
    def with_future_bar(self) -> tuple[Bar, ...]:
        """확정 봉 뒤에 아직 오지 않은 날의 봉을 하나 붙인다. 언제 돌려도 형성 중이다."""
        base = make_series(150, drift=0.0012, amplitude=0.012)
        last = base[-1]
        future = Bar(
            ts=dt.datetime(2099, 1, 2),
            open=last.close,
            high=last.close * 1.01,
            low=last.close * 0.99,
            close=last.close * 0.995,
            volume=last.volume,
            fx_rate=last.fx_rate,
        )
        return (*base, future)

    @staticmethod
    def _payload(path: Path) -> dict[str, object]:
        match = PAYLOAD_RE.search(path.read_text(encoding="utf-8"))
        assert match is not None
        data = json.loads(match.group(1))
        assert isinstance(data, dict)
        return data

    @pytest.mark.parametrize(
        ("module", "field"),
        [(build_glance, "bar"), (build_desk, "bar"), (build_site, "generated")],
        ids=["glance", "desk", "signals"],
    )
    def test_main_이_형성_중인_봉을_뺀다(
        self,
        module: object,
        field: str,
        with_future_bar: tuple[Bar, ...],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(module, "load_bars", lambda *a, **k: with_future_bar)
        if module is build_desk:
            monkeypatch.setattr(build_desk, "underlying_daily_vol", lambda bars: (0.039, "ewy"))
        out = tmp_path / "page.html"
        assert module.main(["--out", str(out)]) == 0
        expected = with_future_bar[-2].ts.strftime("%Y-%m-%d")
        assert self._payload(out)[field] == expected


# ---------------------------------------------------------------------------
# 자바스크립트: 단계 판정과 실제 화면 문구
# ---------------------------------------------------------------------------


def _logic(template_text: str) -> str:
    bodies = re.findall(r"<script>(.*?)</script>", template_text, re.S)
    assert bodies, "페이지 로직 스크립트가 없다"
    return "\n".join(bodies)


def _function(script: str, name: str) -> str:
    """``function name(...) { ... }`` 를 중괄호 짝을 맞춰 잘라 낸다."""
    start = script.index("function " + name + "(")
    brace = script.index("{", start)
    depth = 0
    for i in range(brace, len(script)):
        if script[i] == "{":
            depth += 1
        elif script[i] == "}":
            depth -= 1
            if depth == 0:
                return script[start : i + 1]
    raise AssertionError(f"{name} 의 중괄호 짝이 맞지 않는다")


NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node 가 없다")

HARNESS = r"""
const vm = require("vm");
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
let clock = input.now;
class FakeDate extends Date {
  constructor(...a) { if (a.length) { super(...a); } else { super(clock); } }
  static now() { return clock; }
}
const els = {};
function el(id) {
  const base = {
    id: id, textContent: "", innerHTML: "", hidden: false, className: "", value: "",
    placeholder: "", style: {}, dataset: {}, attrs: {}, children: [],
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    addEventListener() {}, removeEventListener() {},
    appendChild(c) { this.children.push(c); return c; },
    removeChild() {}, insertBefore(c) { return c; },
    querySelector() { return el("q"); }, querySelectorAll() { return []; },
    getBoundingClientRect() { return { width: 600, height: 300, left: 0, top: 0, right: 600, bottom: 300 }; },
  };
  return new Proxy(base, {
    get(t, k) {
      if (k in t) { return t[k]; }
      if (typeof k === "symbol") { return undefined; }
      return function () { return el("x"); };
    },
    set(t, k, v) { t[k] = v; return true; },
  });
}
const store = {};
const storage = {
  getItem(k) { return k in store ? store[k] : null; },
  setItem(k, v) { store[k] = String(v); },
  removeItem(k) { delete store[k]; },
};
const ticks = [];
let reloaded = 0;
const sandbox = {
  document: {
    getElementById(id) {
      if (!els[id]) {
        els[id] = el(id);
        if (id === "payload") { els[id].textContent = input.payload; }
      }
      return els[id];
    },
    createElementNS() { return el("ns"); }, createElement() { return el("c"); },
    addEventListener() {}, documentElement: el("html"), body: el("body"), hidden: false,
    querySelector() { return el("q"); }, querySelectorAll() { return []; },
  },
  location: { search: input.search, reload() { reloaded += 1; } },
  localStorage: storage, sessionStorage: storage,
  setInterval(fn) { ticks.push(fn); return ticks.length; },
  setTimeout() { return 0; }, clearInterval() {}, clearTimeout() {},
  requestAnimationFrame() { return 0; },
  matchMedia() { return { matches: false, addEventListener() {}, addListener() {} }; },
  console: console, Date: FakeDate,
  IntersectionObserver: class { observe() {} unobserve() {} disconnect() {} },
  ResizeObserver: class { observe() {} unobserve() {} disconnect() {} },
};
sandbox.window = sandbox;
sandbox.addEventListener = function () {};
vm.createContext(sandbox);
vm.runInContext(input.script, sandbox);
if (input.later !== null) {
  clock = input.later;
  ticks.forEach(function (fn) { fn(); });
}
const out = {};
Object.keys(els).forEach(function (k) {
  const e = els[k];
  out[k] = { text: e.textContent, html: e.innerHTML, cls: e.className,
    state: e.attrs["data-state"] || null, hidden: e.hidden };
});
console.log(JSON.stringify({ reloaded: reloaded, els: out }));
"""


def _ms(iso: str, minutes: float = 0.0) -> int:
    moment = dt.datetime.fromisoformat(iso) + dt.timedelta(minutes=minutes)
    return int(moment.timestamp() * 1000)


def _render(
    page: str,
    payload: dict[str, Any],
    now_ms: int,
    *,
    later_ms: int | None = None,
    search: str = "",
) -> dict[str, object]:
    """페이지 스크립트를 가짜 DOM 에서 실제로 돌려 요소별 글자를 돌려준다."""
    script = _logic(TEMPLATES[page].read_text(encoding="utf-8"))
    stdin = json.dumps(
        {
            "payload": json.dumps(payload, ensure_ascii=False),
            "script": script,
            "search": search,
            "now": now_ms,
            "later": later_ms,
        },
        ensure_ascii=False,
    )
    out = subprocess.run(
        [NODE or "node", "-e", HARNESS],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout)
    assert isinstance(result, dict)
    return result


def _text(result: dict[str, object], element: str, key: str = "text") -> str:
    els = result["els"]
    assert isinstance(els, dict) and element in els, f"{element} 가 그려지지 않았다"
    value = els[element][key]
    return value if isinstance(value, str) else ""


Payloads = dict[tuple[str, str], dict[str, Any]]

GO_SERIES = {"n": 150, "drift": 0.002, "amplitude": 0.012}
STOP_SERIES = {"n": 150, "drift": 0.0012, "amplitude": 0.012}


@pytest.fixture(scope="module")
def payloads() -> Payloads:
    """페이지별 GO/STOP 페이로드. 신호 원장은 백테스트를 돌려서 한 번만 만든다."""
    cfg = StrategyConfig()
    made: Payloads = {}
    for kind, spec in (("go", GO_SERIES), ("stop", STOP_SERIES)):
        bars = make_series(spec["n"], drift=spec["drift"], amplitude=spec["amplitude"])
        made["glance", kind] = build_glance.build_payload(bars, cfg)
        made["desk", kind] = build_desk.build_payload(bars, cfg, 0.039)
        made["signals", kind] = build_site.build_payload(bars, cfg)
    assert made["glance", "go"]["allowed"] is True, "GO 픽스처가 게이트를 통과하지 않는다"
    assert made["glance", "stop"]["allowed"] is False, "STOP 픽스처가 막히지 않는다"
    return made


def _moments(payload: dict[str, Any]) -> dict[str, int]:
    when = payload["when"]
    assert isinstance(when, dict)
    return {
        "before": _ms(when["openAt"], -60),
        "live": _ms(when["openAt"], 60),
        "after": _ms(when["closeAt"], 60),
        "reload": _ms(when["refreshAt"], 10),
        "late": _ms(when["refreshAt"], 60),
    }


class TestSharedTimeCode:
    @pytest.mark.parametrize("name", ["viewNow", "stageOf", "keepFresh"])
    def test_세_페이지의_시각_판단_코드가_글자까지_같다(self, name: str) -> None:
        """한 페이지만 고치면 같은 순간에 페이지끼리 서로 다른 판정을 보여 준다."""
        texts = {
            page: _function(_logic(path.read_text(encoding="utf-8")), name)
            for page, path in TEMPLATES.items()
        }
        assert len(set(texts.values())) == 1, texts

    @pytest.mark.parametrize("page", list(TEMPLATES))
    def test_판정을_keepFresh_로_계속_다시_그린다(self, page: str) -> None:
        """열어 둔 채로 개장을 넘기면 "사도 된다" 가 그대로 남는다. 새로 고침을 기대하면 안 된다."""
        script = _logic(TEMPLATES[page].read_text(encoding="utf-8"))
        assert re.search(r"keepFresh\((W|WHEN), LOADED, renderVerdict\);", script)

    @needs_node
    def test_경계_시각에서_단계가_바뀐다(self) -> None:
        """개장 순간 live, 마감 순간 after, 갱신 시각부터는 화면을 연 시각에 따라 reload/late."""
        script = _logic(build_glance.TEMPLATE.read_text(encoding="utf-8"))
        stage_of = _function(script, "stageOf")
        when = verdict_schedule(dt.date(2026, 9, 25))
        refresh = when["refreshAt"]
        cases = [
            ("2026-09-28T22:29:59+09:00", None, "before"),
            ("2026-09-28T22:30:00+09:00", None, "live"),
            ("2026-09-29T04:59:59+09:00", None, "live"),
            ("2026-09-29T05:00:00+09:00", None, "after"),
            ("2026-09-29T11:39:59+09:00", None, "after"),
            # 갱신 시각이 지났다. 아침에 열어 둔 탭은 "늦어진다" 가 아니라 "새로 고침하라".
            ("2026-09-29T14:00:00+09:00", "2026-09-29T08:00:00+09:00", "reload"),
            # 갱신 30분 뒤에 새로 열었는데도 옛 판정이면 그때 "늦어진다".
            ("2026-09-29T14:00:00+09:00", "2026-09-29T12:10:00+09:00", "late"),
            ("2026-09-29T12:05:00+09:00", "2026-09-29T12:05:00+09:00", "reload"),
        ]
        program = (
            stage_of
            + "\nvar W = "
            + json.dumps(when)
            + ";\nvar cases = "
            + json.dumps([[t, loaded or t] for t, loaded, _ in cases])
            + ";\nconsole.log(JSON.stringify(cases.map(function (c) {"
            + " return stageOf(W, Date.parse(c[0]), Date.parse(c[1])); })));"
        )
        out = subprocess.run(
            [NODE or "node", "-e", program],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert refresh == "2026-09-29T11:40:00+09:00"
        assert json.loads(out.stdout) == [expected for _, _, expected in cases]


@needs_node
class TestRenderedGlance:
    """한눈에 페이지가 단계마다 실제로 무엇을 적는지. 09-28 밤의 사고를 그대로 재현한다."""

    def test_개장_전_통과_판정은_지정가와_함께_사도_된다(self, payloads: Payloads) -> None:
        p = payloads["glance", "go"]
        r = _render("glance", p, _moments(p)["before"])
        assert _text(r, "headline") == "사도 된다"
        assert _text(r, "verdict", "state") == "go"
        sub = _text(r, "subline", "html")
        assert f"${p['limit']:.2f}" in sub
        assert p["when"]["open"] in sub
        assert "더 사라는 뜻이 아니다" in _text(r, "subnote")

    def test_장이_열린_뒤에는_사도_된다를_거둔다(self, payloads: Payloads) -> None:
        """2026-09-28 밤의 사고. 이 테스트가 없으면 판정 면 코드를 되돌려도 전부 통과한다."""
        p = payloads["glance", "go"]
        r = _render("glance", p, _moments(p)["live"])
        assert _text(r, "headline") == "매수 시점 지남"
        assert _text(r, "verdict", "state") == "wait"
        assert "사도 된다" not in _text(r, "headline")

    def test_장중_차단_판정은_다음_종가_문턱을_말한다(self, payloads: Payloads) -> None:
        """넘어야 할 값은 빠른 평균이 아니라 flipAt 이다(AGENTS.md 5절)."""
        p = payloads["glance", "stop"]
        r = _render("glance", p, _moments(p)["live"])
        assert _text(r, "headline") == "사지 마라"
        assert _text(r, "verdict", "state") == "stop"
        sub = _text(r, "subline", "html")
        assert f"${p['flipAt']:.2f}" in sub
        assert "넘어야 하는 값" not in sub
        assert p["when"]["close"] in sub

    @pytest.mark.parametrize("kind", ["go", "stop"])
    def test_장이_끝나면_새_판정을_기다린다(self, payloads: Payloads, kind: str) -> None:
        p = payloads["glance", kind]
        r = _render("glance", p, _moments(p)["after"])
        assert _text(r, "headline") == "새 판정 대기"
        assert _text(r, "verdict", "state") == "wait"

    def test_갱신_뒤_새로_열었는데도_옛_판정이면_늦어진다고_말한다(
        self, payloads: Payloads
    ) -> None:
        p = payloads["glance", "go"]
        r = _render("glance", p, _moments(p)["late"])
        assert "갱신 늦어짐" in _text(r, "eyebrow")

    def test_아침에_열어_둔_탭은_늦어진다고_하지_않고_한_번_새로_고친다(
        self,
        payloads: Payloads,
    ) -> None:
        """폰은 뒤로 간 탭을 새로 읽지 않는다. 그 탭이 "갱신 늦어짐" 이라고 하면 거짓말이다."""
        p = payloads["glance", "go"]
        m = _moments(p)
        r = _render("glance", p, m["after"], later_ms=_ms(p["when"]["refreshAt"], 180))
        assert "갱신 늦어짐" not in _text(r, "eyebrow")
        assert _text(r, "headline") == "새로 고침"
        assert r["reloaded"] == 1

    def test_점검용_시각이면_화면에_표시하고_새로_고치지_않는다(self, payloads: Payloads) -> None:
        p = payloads["glance", "go"]
        live = dt.datetime.fromisoformat(p["when"]["openAt"]) + dt.timedelta(hours=1)
        search = "?now=" + live.isoformat().replace("+", "%2B")
        r = _render("glance", p, _ms(p["when"]["refreshAt"], 180), search=search)
        assert "점검용 시각" in _text(r, "eyebrow")
        assert _text(r, "headline") == "매수 시점 지남"
        assert r["reloaded"] == 0

    def test_깨진_점검_시각이_와도_페이지가_멈추지_않는다(self, payloads: Payloads) -> None:
        """decodeURIComponent("%") 는 예외를 던진다. 그대로 두면 스크립트 전체가 멈춘다."""
        p = payloads["glance", "go"]
        r = _render("glance", p, _moments(p)["before"], search="?now=%")
        assert _text(r, "headline") == "사도 된다"
        assert _text(r, "chklist", "html"), "판정 뒤의 그림·조건표까지 그려져야 한다"

    def test_가격이_아닌_조건이_막으면_그_조건을_말한다(self, payloads: Payloads) -> None:
        """종가는 기준선 위인데 다른 조건이 막은 날. "0센트 모자란다" 고 적으면 안 된다."""
        p = dict(payloads["glance", "go"])
        p["checks"] = [dict(c) for c in p["checks"]]
        p["checks"][3]["ok"] = False
        blocked = p["checks"][3]["plain"]
        p.update(allowed=False, trendOk=True, others=[blocked], gapCents=0)
        r = _render("glance", p, _moments(p)["before"])
        sub = _text(r, "subline", "html")
        assert "막힌 것" in sub and blocked in sub
        assert "0센트" not in sub
        assert "가격이 아니라" in _text(r, "gapDesc")
        assert "0센트" not in _text(r, "gapFig", "html")
        assert "막힌 것(" in _text(r, "flipCond", "html")


@needs_node
class TestRenderedDesk:
    def test_개장_전_통과는_매수_시점과_지정가를_보여_준다(self, payloads: Payloads) -> None:
        p = payloads["desk", "go"]
        r = _render("desk", p, _moments(p)["before"])
        assert "전부 통과" in _text(r, "vtxt")
        assert _text(r, "verdict", "cls") == "badge yes"
        assert _text(r, "whenK") == "매수 시점"
        assert f"${p['limit']:.2f}" in _text(r, "vwhen", "html")

    def test_장이_열린_뒤에는_통과를_거둔다(self, payloads: Payloads) -> None:
        p = payloads["desk", "go"]
        r = _render("desk", p, _moments(p)["live"])
        assert "매수 시점 지남" in _text(r, "vtxt")
        assert _text(r, "verdict", "cls") == "badge wait"
        assert _text(r, "whenK") == "다음 판정"

    def test_차단_판정에는_살_시각을_적지_않는다(self, payloads: Payloads) -> None:
        p = payloads["desk", "stop"]
        r = _render("desk", p, _moments(p)["live"])
        assert _text(r, "verdict", "cls") == "badge no"
        assert "진입 보류" in _text(r, "vtxt")
        assert _text(r, "whenK") == "다음 판정"

    def test_열어_둔_탭은_늦어진다고_하지_않는다(self, payloads: Payloads) -> None:
        p = payloads["desk", "go"]
        r = _render("desk", p, _moments(p)["after"], later_ms=_ms(p["when"]["refreshAt"], 180))
        assert "갱신 늦어짐" not in _text(r, "vtxt")
        assert "새로 고침" in _text(r, "vtxt")


@needs_node
class TestRenderedSignals:
    def test_차단_판정은_실제_검사표를_그린다(self, payloads: Payloads) -> None:
        """예전에는 7칸을 통과로 박아 두고 판정을 EMA10 > EMA30 하나로 흉내 냈다."""
        p = payloads["signals", "stop"]
        r = _render("signals", p, _moments(p)["before"])
        current = p["current"]
        assert isinstance(current, dict)
        checks = _text(r, "checks", "html")
        assert checks.count('class="chk no"') == len(current["blockers"]) > 0
        assert checks.count('class="chk ok"') == len(current["checks"]) - len(current["blockers"])
        assert "진입 보류" in _text(r, "vtxt")
        assert _text(r, "nowTitle") == "지금은 왜 신호가 없는가"

    def test_장이_열린_뒤에는_제목도_신호_있음을_거둔다(self, payloads: Payloads) -> None:
        p = payloads["signals", "go"]
        before = _render("signals", p, _moments(p)["before"])
        assert _text(before, "nowTitle") == "지금 판정 — 신호 있음"
        live = _render("signals", p, _moments(p)["live"])
        assert "매수 시점 지남" in _text(live, "nowTitle")
        assert "wait" in _text(live, "vbadge", "cls")
        after = _render("signals", p, _moments(p)["after"])
        assert _text(after, "nowTitle") == "새 판정 대기"

    def test_원장_템플릿이_읽는_필드가_페이로드에_다_있다(self, payloads: Payloads) -> None:
        """필드가 비면 브라우저는 오류 없이 그 자리만 비운다("+NaN%", "undefined 종가")."""
        p = payloads["signals", "go"]
        script = _logic(build_site.TEMPLATE.read_text(encoding="utf-8"))
        for field in set(re.findall(r"\bD\.([A-Za-z_]\w*)", script)):
            assert field in p, f"D.{field}"
        for alias, key in {
            "C": "current",
            "N": "next",
            "P": "plan",
            "S": "summary",
            "WHEN": "when",
        }.items():
            block = p[key]
            assert isinstance(block, dict)
            for field in set(re.findall(r"\b" + alias + r"\.([A-Za-z_]\w*)", script)):
                assert field in block, f"{alias}.{field}"


class TestGlanceContract:
    def test_한눈에_템플릿이_읽는_필드가_페이로드에_다_있다(
        self, series: tuple[Bar, ...], cfg: StrategyConfig
    ) -> None:
        """필드가 비면 브라우저는 오류 없이 그 자리만 비운다. 상황실은 test_build_desk 가,
        신호 원장은 TestRenderedSignals 가 같은 검사를 한다.
        """
        payload = build_glance.build_payload(series, cfg)
        script = _logic(build_glance.TEMPLATE.read_text(encoding="utf-8"))
        for field in set(re.findall(r"\bD\.([A-Za-z_]\w*)", script)):
            assert field in payload, f"D.{field}"
        for field in set(re.findall(r"\bW\.([A-Za-z_]\w*)", script)):
            assert field in payload["when"], f"W.{field}"
