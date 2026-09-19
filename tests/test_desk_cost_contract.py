"""상황실 페이지가 손익을 저장소 엔진과 같은 식으로 계산하는지 검증.

이 페이지는 본전선·손절선·익절 6단 가격을 **브라우저에서** 계산한다. 그 식이
``koru_trade.pnl`` 과 갈라지면, 화면은 멀쩡한데 값만 틀린다. 아무 오류도 안 난다.

실제로 갈라져 있었다. payload 가 매수측 비용만 보냈고(``fee``/``fxSpread``),
페이지는 그 한 쌍을 매도측에도 썼다. 매도측의 ``sell_fee_rate``·``sec_fee_rate``·
주당 정액 ``finra_taf_per_share``·``fx_spread_sell`` 이 하나도 건너가지 않았다.
대칭 설정에서도 왕복 0.29%p 가 벌어졌고, 환전 우대가 없는 설정(``fx_spread_sell``
0.0095)에서는 0.4%p 이상 **낙관적인 쪽으로** 틀렸다. 익절 1단이 원화 +5% 인
전략에서 그 크기는 결론을 뒤집는다.

여기서는 페이지의 자바스크립트 식을 파이썬으로 그대로 옮겨 ``pnl.krw_return`` 과
맞대어 본다. 한쪽을 고치고 다른 쪽을 안 고치면 이 파일이 깨진다.
"""

from __future__ import annotations

import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from tests.conftest import make_series

from koru_trade.config import StrategyConfig
from koru_trade.models import Bar
from koru_trade.pnl import CostModel, FxMode, krw_return

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_desk  # noqa: E402

EWY_VOL = 0.0391
"""네트워크 없이 고정한 기초지수 변동성. 실제 관측 수준의 값."""

# 페이지의 krwReturn 이 읽는 비용 키. 하나라도 payload 에서 빠지면
# 자바스크립트에서 undefined 가 되어 NaN 이 전파되고 화면이 조용히 빈다.
PAGE_COST_KEYS = ("fee", "sellFee", "secFee", "taf", "slip", "fxSpread", "fxSpreadSell")


def _payload(cfg: StrategyConfig, bars: tuple[Bar, ...], **kw: Any) -> dict[str, Any]:
    return build_desk.build_payload(bars, cfg, EWY_VOL, **kw)


def _page_krw_return(
    cost: dict[str, float], buy: float, bfx: float, sell: float, sfx: float
) -> float:
    """``desk_template.html`` 의 ``krwReturn`` 을 그대로 옮긴 것.

    페이지를 고치면 이 함수도 같이 고쳐야 한다. 그래야 두 식이 갈라졌을 때
    이 파일이 깨진다 — 그게 이 테스트의 존재 이유다.
    """
    in_usd = buy * (1 + cost["slip"])
    out_usd = sell * (1 - cost["slip"])
    paid = in_usd * (1 + cost["fee"]) * bfx * (1 + cost["fxSpread"])
    fees = out_usd * (cost["sellFee"] + cost["secFee"]) + cost["taf"]
    got = max(0.0, out_usd - fees) * sfx * (1 - cost["fxSpreadSell"])
    return got / paid - 1.0


def _engine_krw_return(c: CostModel, buy: float, bfx: float, sell: float, sfx: float) -> float:
    """저장소 엔진의 같은 왕복.

    슬리피지는 ``krw_cost``/``krw_proceeds`` 가 참조하지 않으므로(AGENTS.md 도메인
    함정) 체결가에 먼저 곱한다. 페이지도 같은 자리에서 곱한다.
    """
    return krw_return(1, buy * (1 + c.slippage_rate), bfx, sell * (1 - c.slippage_rate), sfx, c)


@pytest.fixture
def bars() -> tuple[Bar, ...]:
    return make_series(150, drift=0.002, amplitude=0.01)


COST_CASES = [
    pytest.param({}, id="기본(대칭)"),
    pytest.param({"sell_fee_rate": 0.0025}, id="매도수수료_비대칭"),
    pytest.param({"fx_spread_sell": 0.0095}, id="환전우대_없음"),
    pytest.param({"buy_fee_rate": 0.0005}, id="매수수수료_비대칭"),
    pytest.param({"finra_taf_per_share": 0.00166}, id="TAF_10배"),
    pytest.param({"sec_fee_rate": 0.000278}, id="SEC_10배"),
    pytest.param({"fx_mode": FxMode.HOLD_USD}, id="달러예수금_유지"),
    pytest.param({"slippage_rate": 0.0}, id="슬리피지_없음"),
]


class TestPageMatchesEngine:
    @pytest.mark.parametrize("override", COST_CASES)
    @pytest.mark.parametrize(("buy", "sell"), [(20.0, 20.0), (20.0, 23.0), (23.0, 19.0)])
    def test_페이지_식이_저장소_엔진과_같다(
        self,
        cfg: StrategyConfig,
        bars: tuple[Bar, ...],
        override: dict[str, Any],
        buy: float,
        sell: float,
    ) -> None:
        c = replace(cfg.cost, **override)
        payload = _payload(replace(cfg, cost=c), bars)

        page = _page_krw_return(payload["cost"], buy, 1350.0, sell, 1350.0)
        engine = _engine_krw_return(c, buy, 1350.0, sell, 1350.0)

        assert page == pytest.approx(engine, rel=1e-6, abs=1e-9), (
            f"페이지 {page:+.6%} vs 엔진 {engine:+.6%} — 손익 계산이 갈라졌다"
        )

    def test_환율이_움직여도_같다(self, cfg: StrategyConfig, bars: tuple[Bar, ...]) -> None:
        """원화 수익률은 환율 변화를 곱으로 받는다. 한쪽만 틀리면 여기서 벌어진다."""
        payload = _payload(cfg, bars)
        for bfx, sfx in ((1340.0, 1390.0), (1390.0, 1340.0)):
            page = _page_krw_return(payload["cost"], 20.0, bfx, 22.0, sfx)
            engine = _engine_krw_return(cfg.cost, 20.0, bfx, 22.0, sfx)
            assert page == pytest.approx(engine, rel=1e-6, abs=1e-9)


class TestCostPayload:
    def test_페이지가_읽는_비용_키가_전부_있다(
        self, cfg: StrategyConfig, bars: tuple[Bar, ...]
    ) -> None:
        cost = _payload(cfg, bars)["cost"]
        missing = [k for k in PAGE_COST_KEYS if k not in cost]
        assert not missing, f"payload['cost'] 에 없는 키를 페이지가 읽는다: {missing}"

    def test_템플릿이_읽는_비용_키와_payload_가_일치한다(
        self, cfg: StrategyConfig, bars: tuple[Bar, ...]
    ) -> None:
        """이 목록을 손으로 적어 두면 드리프트를 못 잡는다. 템플릿에서 뽑는다."""
        text = build_desk.TEMPLATE.read_text(encoding="utf-8")
        body = text[text.rindex("<script>") :]
        read = set(re.findall(r"\bC\.([A-Za-z_][A-Za-z0-9_]*)", body))
        cost = _payload(cfg, bars)["cost"]
        assert read, "템플릿에서 C.<필드> 를 하나도 못 찾았다 — 추출기가 망가졌다"
        missing = sorted(read - set(cost))
        assert not missing, f"템플릿이 읽는데 payload 에 없다: {missing}"

    def test_환전_스프레드는_실효값이다(self, cfg: StrategyConfig, bars: tuple[Bar, ...]) -> None:
        """fx_mode 가 HOLD_USD 면 환전을 안 하므로 스프레드가 0 이다.

        원시 ``fx_spread_buy`` 를 그대로 보내면 페이지가 없는 비용을 계산한다
        (왕복 0.641% vs 0.143%).
        """
        hold = replace(cfg, cost=replace(cfg.cost, fx_mode=FxMode.HOLD_USD))
        cost = _payload(hold, bars)["cost"]
        assert cost["fxSpread"] == 0.0
        assert cost["fxSpreadSell"] == 0.0
        assert hold.cost.fx_spread_buy > 0.0, "원시값은 0 이 아니어야 이 검사가 의미가 있다"

    def test_왕복비용에_슬리피지_왕복분이_들어_있다(
        self, cfg: StrategyConfig, bars: tuple[Bar, ...]
    ) -> None:
        cost = _payload(cfg, bars)["cost"]
        expected = cfg.cost.round_trip_drag + 2.0 * cfg.cost.slippage_rate
        assert cost["roundTrip"] == pytest.approx(expected, abs=1e-5)
        assert cost["roundTrip"] > cfg.cost.round_trip_drag


class TestVolatilitySource:
    def test_출처가_payload_에_실린다(self, cfg: StrategyConfig, bars: tuple[Bar, ...]) -> None:
        for src in ("ewy", "approx", "none"):
            payload = _payload(cfg, bars, vol_source=src)
            assert payload["ewyVolSource"] == src

    def test_감쇠는_영이_아닌_음수다(self, cfg: StrategyConfig, bars: tuple[Bar, ...]) -> None:
        """-0.0 으로 눌리면 페이지가 3배 상품에 대고 '안 녹는다' 고 말한다."""
        payload = _payload(cfg, bars)
        assert payload["decayDaily"] < 0.0
        assert str(payload["decayDaily"]) != "-0.0"

    def test_아주_작은_변동성도_영으로_눌리지_않는다(
        self, cfg: StrategyConfig, bars: tuple[Bar, ...]
    ) -> None:
        """5자리 반올림이면 sigma 0.0013 아래가 전부 -0.0 이 됐다."""
        payload = build_desk.build_payload(bars, cfg, 0.0015)
        assert payload["decayDaily"] < 0.0

    def test_표본이_모자라면_출처가_none_이다(self) -> None:
        """0.0 은 값이 아니라 결측이다. 그걸 실측처럼 보여주면 안 된다."""
        short = make_series(2)
        sigma, source = build_desk.underlying_daily_vol(short)
        assert source in {"ewy", "approx", "none"}
        if sigma == 0.0:
            assert source == "none"


class TestConfigName:
    def test_어떤_설정으로_만들었는지_실린다(
        self, cfg: StrategyConfig, bars: tuple[Bar, ...]
    ) -> None:
        """strategy.yaml 은 gitignore 다. 로컬과 클라우드가 다른 규칙으로 갈라질 수 있다."""
        payload = _payload(cfg, bars, config_name="frequent.example.yaml")
        assert payload["configName"] == "frequent.example.yaml"

    def test_기본값은_빈_문자열이지_None_이_아니다(
        self, cfg: StrategyConfig, bars: tuple[Bar, ...]
    ) -> None:
        """페이지가 ``D.configName || "-"`` 로 읽는다. None 이면 JSON 에 null 이 실린다."""
        assert _payload(cfg, bars)["configName"] == ""
