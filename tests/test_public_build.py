"""공개 배포본이 실제 운용액을 드러내지 않는지 검증.

페이지에 계좌번호도 토큰도 없다. 그런데 ``capital_krw`` 와
``risk.max_position_krw`` 는 **얼마를 굴리는지를 그대로 드러낸다.** 수량·
투입금액·실현손익이 전부 거기서 파생되므로, 공개 배포본은 기준 자본
100만원으로 환산해서 만든다.

되돌릴 수 없는 종류의 실수라 여기서 못 박는다. 한 번 공개 저장소에 올라간
숫자는 커밋을 지워도 포크와 캐시에 남는다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from tests.conftest import make_series

from koru_trade.config import RiskConfig, StrategyConfig
from koru_trade.models import Bar

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_desk  # noqa: E402
from _site_common import PUBLIC_CAPITAL_KRW, anonymize  # noqa: E402

EWY_VOL = 0.0391


@pytest.fixture
def bars() -> tuple[Bar, ...]:
    return make_series(150, drift=0.002, amplitude=0.01)


class TestAnonymize:
    def test_자본이_공개_기준으로_바뀐다(self, cfg: StrategyConfig) -> None:
        assert anonymize(cfg).capital_krw == PUBLIC_CAPITAL_KRW

    def test_이미_작은_자본도_기준값으로_맞춘다(self) -> None:
        """실제 운용액이 100만원보다 작아도 그 사실 자체가 정보다."""
        small = StrategyConfig(
            capital_krw=500_000.0,
            risk=RiskConfig(
                max_position_krw=900_000.0,
                max_daily_notional_krw=900_000.0,
                daily_loss_limit_krw=100_000.0,
            ),
        )
        pub = anonymize(small)
        assert pub.capital_krw == PUBLIC_CAPITAL_KRW
        assert pub.risk.max_position_krw == pytest.approx(1_800_000.0), "비율 1.8배가 유지돼야 한다"

    def test_한도도_같은_비율로_줄어든다(self, cfg: StrategyConfig) -> None:
        pub = anonymize(cfg)
        scale = PUBLIC_CAPITAL_KRW / cfg.capital_krw
        assert pub.risk.max_position_krw == pytest.approx(cfg.risk.max_position_krw * scale)
        assert pub.risk.daily_loss_limit_krw == pytest.approx(cfg.risk.daily_loss_limit_krw * scale)

    def test_비율이_보존된다(self, cfg: StrategyConfig) -> None:
        """비율이 깨지면 공개 페이지의 판정이 실제와 달라진다."""
        pub = anonymize(cfg)
        assert pub.risk.max_position_krw / pub.capital_krw == pytest.approx(
            cfg.risk.max_position_krw / cfg.capital_krw
        )

    def test_전략_규칙은_건드리지_않는다(self, cfg: StrategyConfig) -> None:
        """익절 문턱·손절·이평 기간이 바뀌면 다른 전략을 공개하는 셈이다."""
        pub = anonymize(cfg)
        assert pub.take_profit == cfg.take_profit
        assert pub.scale_in == cfg.scale_in
        assert (pub.ema_fast, pub.ema_slow) == (cfg.ema_fast, cfg.ema_slow)
        assert pub.max_stop_pct == cfg.max_stop_pct
        assert pub.symbol == cfg.symbol

    def test_원본을_바꾸지_않는다(self, cfg: StrategyConfig) -> None:
        before = cfg.capital_krw
        anonymize(cfg)
        assert cfg.capital_krw == before


class TestPayloadHasNoRealCapital:
    def test_공개_설정으로_만든_페이로드의_자본이_기준값이다(
        self, cfg: StrategyConfig, bars: tuple[Bar, ...]
    ) -> None:
        payload = build_desk.build_payload(bars, anonymize(cfg), EWY_VOL)
        assert payload["rules"]["capital"] == PUBLIC_CAPITAL_KRW

    def test_공개_안_하면_실제_자본이_그대로_실린다(
        self, cfg: StrategyConfig, bars: tuple[Bar, ...]
    ) -> None:
        """이 검사가 실패하면 위 검사가 아무것도 증명하지 못한다."""
        payload = build_desk.build_payload(bars, cfg, EWY_VOL)
        assert payload["rules"]["capital"] == cfg.capital_krw
        assert cfg.capital_krw != PUBLIC_CAPITAL_KRW, "픽스처가 하필 기준값과 같다"


class TestPublicBuilderContract:
    def test_누출_검사가_자본_필드를_본다(self) -> None:
        """검사 대상 경로가 페이로드 구조와 맞는지 확인한다.

        구조가 바뀌면 검사는 아무것도 못 찾고 조용히 통과한다. 그래서
        build_public 은 찾은 개수가 0 이면 실패하게 되어 있다.
        """
        text = (SCRIPTS / "build_public.py").read_text(encoding="utf-8")
        assert '("rules", "capital")' in text
        assert '("summary", "capital")' in text
        assert "checked == 0" in text, "필드를 못 찾았을 때 실패하는 분기가 사라졌다"

    def test_공개_모드_플래그가_두_생성기에_다_있다(self) -> None:
        for name in ("build_desk.py", "build_site.py"):
            text = (SCRIPTS / name).read_text(encoding="utf-8")
            assert '"--public"' in text, f"{name} 에 --public 이 없다"
            assert "anonymize(cfg)" in text, f"{name} 이 anonymize 를 안 부른다"
