"""들고 있을 때 "얼마나, 어떻게 파는지" 를 보여 주는 계산이 저장소 엔진과 같은지.

한눈에 페이지의 "들고 있다면" 칸과 상황실 계산기는 같은 함수(``sellPlan``)를 쓴다.
페이지는 이 계산을 **브라우저에서** 한다. 식이 ``koru_trade.pnl``·``strategy.decide`` 와
갈라지면 화면은 멀쩡한데 파는 가격만 틀린다. 그래서 페이지의 자바스크립트를 node 로
실제로 돌려 파이썬 값과 맞대 본다.

- 원화 손익: ``pnl.krw_return`` (실제 평단 + 지정가 매도라 슬리피지 없음)
- 익절·본전·손절·추세 꺾임 가격: ``pnl.required_sell_price_usd``, ``strategy.stop_price_usd``
- 익절 수량: ``strategy.decide`` 의 계단 규칙(첫 익절 전 수량의 비율, 반올림은 파이썬
  ``round``, 마지막 계단은 남은 것 전부, 이미 판 계단은 다시 나오지 않는다)
- 한도 초과: 원화 투입 원가 기준(내림). 넘친 만큼을 빼고 남는 수량으로 계단을 짠다.

예시 보유는 전부 중립 값이다(AGENTS.md 11절: 실제 보유·평단을 저장소에 쓰지 않는다).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar

import pytest
from tests.conftest import make_series, open_position
from tests.test_site_schedule import (
    NODE,
    TEMPLATES,
    _function,
    _logic,
    _moments,
    _render,
    _text,
    needs_node,
)

from koru_trade.config import StrategyConfig, TakeProfitStep
from koru_trade.pnl import FxMode, krw_cost, krw_return, required_sell_price_usd
from koru_trade.strategy import stop_price_usd

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_desk  # noqa: E402
import build_glance  # noqa: E402
from _site_common import cent_below, cost_payload, sell_rules  # noqa: E402

SHARED = ["heldCost", "heldProceeds", "heldReturn", "heldPriceFor", "roundHalfEven", "sellPlan"]
HOLD_PAGES = {"glance": TEMPLATES["glance"], "desk": TEMPLATES["desk"]}

OPERATING = StrategyConfig(
    trend_break_min_krw_return=0.0015,
    max_holding_days=1,
    take_profit=(
        TakeProfitStep(krw_return=0.05, sell_fraction=0.24),
        TakeProfitStep(krw_return=0.07, sell_fraction=0.20),
        TakeProfitStep(krw_return=0.10, sell_fraction=0.18),
        TakeProfitStep(krw_return=0.13, sell_fraction=0.16),
        TakeProfitStep(krw_return=0.16, sell_fraction=0.12),
        TakeProfitStep(krw_return=0.20, sell_fraction=0.10),
    ),
)
"""운용 설정과 같은 모양(6단 계단, 추세 꺾임 0.15%, 보유 1일). 로컬 설정 파일에 기대지 않는다."""

COST_CASES = [
    pytest.param({}, id="기본"),
    pytest.param({"sell_fee_rate": 0.0025}, id="매도수수료_비대칭"),
    pytest.param({"fx_spread_sell": 0.0095}, id="환전우대_없음"),
    pytest.param({"finra_taf_per_share": 0.00166}, id="TAF_10배"),
    pytest.param({"fx_mode": FxMode.HOLD_USD}, id="달러예수금_유지"),
]


def _shared_js() -> str:
    script = _logic(HOLD_PAGES["glance"].read_text(encoding="utf-8"))
    return "\n".join(_function(script, name) for name in SHARED)


def _node(program: str) -> Any:
    out = subprocess.run(
        [NODE or "node", "-e", program],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _call(expr: str, **env: Any) -> Any:
    """공유 함수를 불러 결과를 JSON 으로 받는다. ``env`` 는 JS 변수로 넘긴다."""
    decls = "".join(f"var {k} = {json.dumps(v)};\n" for k, v in env.items())
    return _node(_shared_js() + "\n" + decls + f"console.log(JSON.stringify({expr}));")


def _decide_ladder(base: int, fractions: list[float]) -> list[int]:
    """``strategy.decide`` 의 익절 계단 수량 규칙을 그대로 옮긴 것.

    계단마다 ``min(남은 수량, max(1, round(기준 수량 × 비율)))``, 마지막 계단은 남은 것 전부.
    """
    left, out = base, []
    for i, f in enumerate(fractions):
        q = left if i == len(fractions) - 1 else min(left, max(1, round(base * f)))
        left -= q
        out.append(q)
    return out


def _plan(cfg: StrategyConfig, pos: dict[str, float], mkt: dict[str, float]) -> Any:
    full_pos = {"lim": 0, "base0": 0, **pos}
    full_mkt = {"atr": 0.0, **mkt}
    return _call(
        "sellPlan(C, R, pos, mkt)",
        C=cost_payload(cfg),
        R=sell_rules(cfg),
        pos=full_pos,
        mkt=full_mkt,
    )


# ---------------------------------------------------------------------------
# 두 페이지가 같은 코드·같은 입력을 쓴다
# ---------------------------------------------------------------------------


class TestSharedCode:
    @pytest.mark.parametrize("name", SHARED)
    def test_한눈에와_상황실의_계산_코드가_글자까지_같다(self, name: str) -> None:
        """한 페이지만 고치면 같은 보유를 두 페이지가 다른 가격으로 안내한다."""
        texts = {
            page: _function(_logic(path.read_text(encoding="utf-8")), name)
            for page, path in HOLD_PAGES.items()
        }
        assert len(set(texts.values())) == 1, texts

    def test_두_페이지가_같은_비용_블록과_규칙을_싣는다(self) -> None:
        bars = make_series(150, drift=0.0012, amplitude=0.012, fx_start=1354.3)
        glance = build_glance.build_payload(bars, OPERATING)
        desk = build_desk.build_payload(bars, OPERATING, 0.039)
        assert glance["cost"] == desk["cost"] == cost_payload(OPERATING)
        assert glance["sell"] == sell_rules(OPERATING)
        for key in glance["sell"]:
            assert desk["rules"][key] == glance["sell"][key], key
        assert glance["atr"] == desk["atr"]
        assert glance["trendBelow"] == desk["trendBelow"]

    def test_두_페이지가_같은_환율_자릿수를_쓴다(self) -> None:
        """한눈에만 정수 환율을 쓰던 시절 익절가가 1센트씩 갈렸다."""
        bars = make_series(150, drift=0.0012, amplitude=0.012, fx_start=1354.3)
        glance = build_glance.build_payload(bars, OPERATING)
        desk = build_desk.build_payload(bars, OPERATING, 0.039)
        assert glance["fx"]["rate"] == desk["fx"] == 1354.3
        assert glance["fx"]["rate"] != round(glance["fx"]["rate"]), (
            "픽스처 환율이 정수라 검사가 무의미하다"
        )

    def test_추세_꺾임_상한은_빠른_평균보다_엄격히_아래인_센트다(self) -> None:
        """평균을 반올림해 적으면($21.0251 → $21.03) 그 종가에서는 규칙이 안 걸린다."""
        assert cent_below(21.0251) == 21.02
        assert cent_below(21.03) == 21.02
        assert cent_below(21.0299) == 21.02
        assert cent_below(21.0301) == 21.03


# ---------------------------------------------------------------------------
# 엔진과 같은 값
# ---------------------------------------------------------------------------


@needs_node
class TestMatchesEngine:
    @pytest.mark.parametrize("override", COST_CASES)
    def test_원화_손익이_pnl_과_같다(self, override: dict[str, Any]) -> None:
        c = replace(OPERATING.cost, **override)
        C = cost_payload(replace(OPERATING, cost=c))
        cases = [
            (20.0, 1350.0, 21.0, 1350.0),
            (20.0, 1340.0, 23.0, 1390.0),
            (23.0, 1390.0, 19.0, 1340.0),
        ]
        got = _call(
            "cases.map(function (x) { return heldReturn(C, x[0], x[1], x[2], x[3]); })",
            C=C,
            cases=cases,
        )
        for (avg, bfx, price, sfx), page in zip(cases, got, strict=True):
            assert page == pytest.approx(krw_return(1, avg, bfx, price, sfx, c), rel=1e-5, abs=1e-8)

    @pytest.mark.parametrize("override", COST_CASES)
    def test_목표_가격이_required_sell_price_usd_와_같다(self, override: dict[str, Any]) -> None:
        c = replace(OPERATING.cost, **override)
        C = cost_payload(replace(OPERATING, cost=c))
        targets = [-0.10, 0.0, 0.0015, 0.05, 0.07, 0.10, 0.20]
        got = _call(
            "targets.map(function (t) { return heldPriceFor(C, t, 20.0, 1360, 1340); })",
            C=C,
            targets=targets,
        )
        for t, page in zip(targets, got, strict=True):
            assert page == pytest.approx(required_sell_price_usd(20.0, 1360, 1340, t, c), rel=1e-5)

    def test_반올림이_파이썬_round_와_같다(self) -> None:
        """계단 수량은 decide 가 round() 로 정한다. 0.5 에서 JS Math.round 는 올리고 파이썬은 짝수로 간다."""
        xs = [q * f for q in range(1, 401) for f in (0.24, 0.20, 0.18, 0.16, 0.12, 0.10, 0.5, 0.25)]
        assert _call("xs.map(roundHalfEven)", xs=xs) == [round(x) for x in xs]


@needs_node
class TestSellPlan:
    POS: ClassVar[dict[str, float]] = {"qty": 300, "avg": 20.0, "bfx": 1350, "lim": 5_000_000}
    MKT: ClassVar[dict[str, float]] = {"price": 21.0, "fx": 1350, "below": 21.19, "atr": 2.2}

    def test_한도를_넘긴_보유의_파는_계획이_엔진과_같다(self) -> None:
        c = OPERATING.cost
        plan = _plan(OPERATING, self.POS, self.MKT)

        allowed = int(5_000_000 // krw_cost(1, 20.0, 1350, c))
        assert plan["excess"] == 300 - allowed > 0
        assert plan["hold"] == plan["base"] == allowed
        assert plan["sold"] == 0
        assert plan["used"] == pytest.approx(300 * krw_cost(1, 20.0, 1350, c) / 5_000_000)
        assert plan["ret"] == pytest.approx(krw_return(300, 20.0, 1350, 21.0, 1350, c), rel=1e-6)
        assert plan["breakeven"] == pytest.approx(required_sell_price_usd(20.0, 1350, 1350, 0.0, c))

        fractions = [s.sell_fraction for s in OPERATING.take_profit]
        assert [s["qty"] for s in plan["ladder"]] == _decide_ladder(allowed, fractions)
        assert [s["rest"] for s in plan["ladder"]] == _decide_ladder(allowed, fractions)
        for step, rule in zip(plan["ladder"], OPERATING.take_profit, strict=True):
            expected = required_sell_price_usd(20.0, 1350, 1350, rule.krw_return, c)
            assert step["price"] == pytest.approx(expected, rel=1e-6)

    def test_추세_꺾임_구간_끝에서_규칙이_실제로_걸린다(self) -> None:
        """decide 는 원화 수익률 > 0.15% 이고 종가 < 빠른 평균일 때 전량 판다. 둘 다 엄격하다.

        화면에 적힌 구간의 양 끝 가격으로 끝났을 때 규칙이 걸려야 한다. 반올림해 적으면
        끝 가격에서는 안 걸리는데 "전량 판다" 고 안내하게 된다.
        """
        c = OPERATING.cost
        ema = 21.1951
        # 평단을 여러 개 본다. 하나만 보면 반올림이 마침 위로 가서 틀린 코드도 통과한다.
        for avg in (19.50, 19.63, 19.77, 19.91, 20.00, 20.14, 20.28, 20.47):
            plan = _plan(
                OPERATING, {**self.POS, "avg": avg}, {**self.MKT, "below": cent_below(ema)}
            )
            band = plan["band"]
            assert band is not None, avg
            exact_lo = required_sell_price_usd(avg, 1350, 1350, 0.0015, c)
            assert band["lo"] > exact_lo and band["lo"] - exact_lo <= 0.01, avg
            assert band["hi"] == 21.19
            for close in (band["lo"], band["hi"]):
                assert krw_return(1, avg, 1350, close, 1350, c) > 0.0015, (avg, close)
                assert close < ema

    def test_하한이_빠른_평균보다_위면_구간이_없다(self) -> None:
        """평단이 10일 평균보다 한참 위면, 이익이면서 평균 아래로 끝나는 종가가 없다."""
        plan = _plan(OPERATING, {"qty": 100, "avg": 25.0, "bfx": 1350}, self.MKT)
        assert plan["band"] is None

    def test_추세_꺾임을_안_쓰는_설정이면_구간이_없다(self) -> None:
        off = replace(OPERATING, trend_break_min_krw_return=None)
        assert _plan(off, {"qty": 100, "avg": 20.0, "bfx": 1350}, self.MKT)["band"] is None

    def test_익절로_일부_판_뒤에도_계단을_처음_수량에_고정한다(self) -> None:
        """남은 수량으로 계단을 다시 짜면 판 +5% 가 또 나타나 결국 +5% 에서 거의 다 판다.

        decide 는 첫 익절 때의 수량(ladder_base_qty)에 계단을 고정하고, 판 계단은 다시
        걸지 않는다.
        """
        full = _plan(OPERATING, {"qty": 180, "avg": 20.0, "bfx": 1350}, self.MKT)
        first = full["ladder"][0]["qty"]
        after = _plan(
            OPERATING, {"qty": 180 - first, "avg": 20.0, "bfx": 1350, "base0": 180}, self.MKT
        )
        assert after["base"] == 180 and after["sold"] == first
        assert [s["qty"] for s in after["ladder"]] == [s["qty"] for s in full["ladder"]]
        assert after["ladder"][0]["rest"] == 0
        assert [s["rest"] for s in after["ladder"][1:]] == [s["qty"] for s in full["ladder"][1:]]

    @pytest.mark.parametrize("qty", [1, 7, 8, 15, 21, 25, 43, 100, 180, 333])
    def test_기본_계단이_decide_와_같은_수량을_낸다(self, qty: int) -> None:
        cfg = StrategyConfig()
        plan = _plan(cfg, {"qty": qty, "avg": 20.0, "bfx": 1350}, self.MKT)
        fractions = [s.sell_fraction for s in cfg.take_profit]
        assert [s["qty"] for s in plan["ladder"]] == _decide_ladder(qty, fractions)
        assert sum(s["qty"] for s in plan["ladder"]) == qty

    @pytest.mark.parametrize("qty", [1, 12, 25, 27, 43, 180, 200])
    def test_운용_계단이_decide_와_같은_수량을_낸다(self, qty: int) -> None:
        plan = _plan(OPERATING, {"qty": qty, "avg": 20.0, "bfx": 1350}, self.MKT)
        fractions = [s.sell_fraction for s in OPERATING.take_profit]
        assert [s["qty"] for s in plan["ladder"]] == _decide_ladder(qty, fractions)
        assert sum(s["qty"] for s in plan["ladder"]) == qty

    def test_한도_안_수량은_내림이다(self) -> None:
        """반올림하면 내 한도를 한 주 넘는 수량을 "한도 안" 이라고 적게 된다."""
        per_share = krw_cost(1, 20.0, 1350, OPERATING.cost)
        plan = _plan(
            OPERATING, {"qty": 300, "avg": 20.0, "bfx": 1350, "lim": 180.6 * per_share}, self.MKT
        )
        assert plan["hold"] == 180

    def test_변동성이_낮으면_달러_손절이_먼저_걸린다(self) -> None:
        """엔진은 원화 −10% 와 달러 손절(평단 − 2×ATR, 4~12% 로 클램프) 중 높은 쪽에서 판다.

        ATR 이 가격의 2% 면 달러 손절은 −4% 라 원화 −10% 보다 훨씬 위다. 원화 손절만 적으면
        규칙이 이미 판 뒤에도 들고 있으라고 안내하게 된다.
        """
        c = OPERATING.cost
        plan = _plan(OPERATING, {"qty": 100, "avg": 20.0, "bfx": 1350}, {**self.MKT, "atr": 0.4})
        position = open_position(qty=100, price=20.0, fx=1350.0, cost=c, atr=0.4)
        assert position.first_entry_price_usd == 20.0 and position.entry_atr_usd == 0.4
        engine_usd = stop_price_usd(position, OPERATING)
        engine_krw = required_sell_price_usd(20.0, 1350, 1350, OPERATING.hard_stop_krw_return, c)
        assert plan["stop"] == pytest.approx(max(engine_usd, engine_krw))
        assert plan["stopBy"] == "usd"

    def test_변동성이_크면_원화_손절이_먼저다(self) -> None:
        plan = _plan(OPERATING, {"qty": 100, "avg": 20.0, "bfx": 1350}, {**self.MKT, "atr": 2.2})
        assert plan["stopBy"] == "krw"
        assert plan["stop"] == pytest.approx(
            required_sell_price_usd(
                20.0, 1350, 1350, OPERATING.hard_stop_krw_return, OPERATING.cost
            )
        )


# ---------------------------------------------------------------------------
# 실제 화면 문구
# ---------------------------------------------------------------------------


def _store(**values: str) -> dict[str, str]:
    return {"koru-desk-pos": json.dumps(values)}


STORE = _store(qty="300", avg="20.00", cur="21.00", efx="1350", lim="5000000")


@pytest.fixture(scope="module")
def glance() -> dict[str, Any]:
    """환율이 정수가 아닌 픽스처. 정수면 환율 자릿수 버그를 못 잡는다.

    마지막 종가가 빠른 평균 위인 오름세다. 아래면 지난 종가에 이미 추세 꺾임이 걸려
    "지금 전량 판다" 만 나오고 파는 순서표는 그려지지 않는다(그건 따로 시험한다).
    """
    bars = make_series(150, drift=0.002, amplitude=0.012, fx_start=1354.3)
    payload = build_glance.build_payload(bars, OPERATING)
    assert payload["price"] > payload["line"], "픽스처 종가가 빠른 평균 아래다"
    return payload


@pytest.fixture(scope="module")
def desk() -> dict[str, Any]:
    bars = make_series(150, drift=0.002, amplitude=0.012, fx_start=1354.3)
    return build_desk.build_payload(bars, OPERATING, 0.039)


@needs_node
class TestRenderedGlanceHold:
    def test_파는_순서를_보여_준다(self, glance: dict[str, Any]) -> None:
        r = _render("glance", glance, _moments(glance)["before"], storage=STORE)
        assert r["els"]["holdOut"]["hidden"] is False
        out = _text(r, "holdOut", "html")
        assert "한도를 <b>" in out and "먼저" in out
        assert "넘친 것까지 전부에 적용된다" in out, "넘친 주식도 마감 정리·손절 대상이다"
        assert "지정가 매도" in out
        assert glance["when"]["close"] in out, "마감 직전에 볼 것에 그 장의 마감 시각이 있어야 한다"
        assert "산 다음 거래일 종가까지만" in out
        assert "$21.00" in out, "넣은 지금 가격으로 계산해야 한다"
        assert not re.search(r"\+\d+\.\d% 아래다", out), "손절선 거리에 부호가 붙으면 안 된다"

    def test_입력이_없으면_안내만_보인다(self, glance: dict[str, Any]) -> None:
        r = _render("glance", glance, _moments(glance)["before"])
        assert r["els"]["holdOut"]["hidden"] is True
        assert r["els"]["holdEmpty"]["hidden"] is False

    def test_손절선_아래면_지금_전량_판다고_한다(self, glance: dict[str, Any]) -> None:
        """손절선이 지금 가격보다 위인데 "닿으면 판다… +11% 아래다" 라고 적은 적이 있다."""
        store = _store(qty="100", avg="25.00", cur="20.36")
        out = _text(
            _render("glance", glance, _moments(glance)["before"], storage=store), "holdOut", "html"
        )
        assert "지금 전량 판다" in out and "손절선" in out
        assert "닿으면 <b>전량</b>" not in out
        assert not re.search(r"\+\d+\.\d% 아래다", out)

    def test_지난_종가가_추세_꺾임_구간이면_지금_전량_판다고_한다(
        self, glance: dict[str, Any]
    ) -> None:
        """decide 는 추세 꺾임을 익절 계단보다 먼저 본다. 그 종가에 이미 전량 파는 날이었다."""
        p = dict(glance, price=20.40, trendBelow=21.02)
        store = _store(qty="100", avg="19.00", efx="1354.3")
        out = _text(_render("glance", p, _moments(p)["before"], storage=store), "holdOut", "html")
        assert "지금 전량 판다" in out and "추세 꺾임 구간" in out
        assert "도달 — 지금" not in out, "전량 파는 날에 부분 익절을 시키면 안 된다"

    def test_장이_끝나면_보유_기한이_지났다고_한다(self, glance: dict[str, Any]) -> None:
        """보유 1일 규칙에서 그 장에 샀던 사람의 기한은 새 판정으로 바뀌지 않는다."""
        out = _text(
            _render("glance", glance, _moments(glance)["after"], storage=STORE), "holdOut", "html"
        )
        assert "보유 기한이 지났다" in out
        assert "다시 계산된다" in out

    def test_열어_둔_탭도_장이_끝나면_문구가_바뀐다(self, glance: dict[str, Any]) -> None:
        """폰 탭은 다시 읽히지 않는다. 마감을 넘기면 옛 "마감 전에 판다" 가 남으면 안 된다."""
        m = _moments(glance)
        r = _render("glance", glance, m["live"], later_ms=m["after"], storage=STORE)
        out = _text(r, "holdOut", "html")
        assert "보유 기한이 지났다" in out

    def test_일부_판_뒤에는_판_계단을_지운다(self, glance: dict[str, Any]) -> None:
        store = _store(qty="137", avg="20.00", cur="21.60", efx="1354.3", base0="180")
        out = _text(
            _render("glance", glance, _moments(glance)["before"], storage=store), "holdOut", "html"
        )
        assert "팔았음" in out
        assert "이미 판 43주" in out

    def test_같은_입력이면_두_페이지가_같은_가격과_수량을_적는다(
        self, glance: dict[str, Any], desk: dict[str, Any]
    ) -> None:
        store = _store(qty="300", avg="20.00", cur="21.00", efx="1354.3", lim="5000000")
        rg = _render("glance", glance, _moments(glance)["before"], storage=store)
        rd = _render("desk", desk, _moments(desk)["before"], storage=store)
        row = r"<tr[^>]*><td>원화 \+(\d+)%</td><td class=\"num\">\$([\d.]+)</td>.*?(\d+)주</td>"
        g_rows = re.findall(row, _text(rg, "holdOut", "html"))
        d_rows = re.findall(row, _text(rd, "ladder", "html"))
        assert len(g_rows) == len(OPERATING.take_profit)
        assert g_rows == d_rows


@needs_node
class TestRenderedDeskHold:
    def test_상황실이_한도_초과와_추세_꺾임_구간을_적는다(self, desk: dict[str, Any]) -> None:
        r = _render("desk", desk, _moments(desk)["before"], storage=STORE)
        note = _text(r, "posnote", "html")
        assert "한도를" in note and "넘었다" in note
        assert "넘친 것까지 전부에 적용된다" in note
        ladder = _text(r, "ladder", "html")
        assert ladder.count("<tr") == 1 + len(OPERATING.take_profit)

    def test_장이_끝나면_보유_기한이_지났다고_한다(self, desk: dict[str, Any]) -> None:
        r = _render("desk", desk, _moments(desk)["after"], storage=STORE)
        assert "보유 기한이 지났다" in _text(r, "posnote", "html")

    def test_음수_입력이_와도_음수_주식을_적지_않는다(self, desk: dict[str, Any]) -> None:
        """두 페이지가 같은 저장값을 읽는다. 한쪽만 음수를 걸러 내면 계획이 갈린다."""
        store = _store(qty="300", avg="20.00", cur="-21.00", efx="-1350", lim="5000000")
        r = _render("desk", desk, _moments(desk)["before"], storage=store)
        both = _text(r, "posnote", "html") + _text(r, "ladder", "html")
        assert not re.search(r"-\d+주", both)
        assert "%)" in both and "-1" not in _text(r, "posnote", "html").split("한도의")[1][:4]

    def test_손절선_아래면_칸에_표시한다(self, desk: dict[str, Any]) -> None:
        store = _store(qty="100", avg="25.00", cur="20.36")
        r = _render("desk", desk, _moments(desk)["before"], storage=store)
        assert "이미 아래 — 지금 전량" in _text(r, "posstrip", "html")
        assert "지금 전량 판다" in _text(r, "posnote", "html")
