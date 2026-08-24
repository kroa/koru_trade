"""진입 게이트 검증 — "지금 들어가도 되는가" 판정.

사용자가 물은 "지금 진입해도 문제 없을지 체크해줘" 에 답하는 코드가 여기다.
판정이 조용히 낙관적으로 기울면 안 되므로, 나쁜 레짐에서 반드시 막히는지 확인한다.
"""

from __future__ import annotations

import pytest
from tests.conftest import make_series

from koru_trade.backtest.pathsim import scan_forward_outcomes
from koru_trade.config import StrategyConfig
from koru_trade.entry_gate import Verdict, evaluate_gate
from koru_trade.models import Bar


class TestGuards:
    def test_봉이_부족하면_거부된다(self, cfg: StrategyConfig) -> None:
        with pytest.raises(ValueError, match="최소"):
            evaluate_gate(make_series(10), cfg)


class TestSnapshot:
    def test_레짐_지표가_모두_채워진다(self, cfg: StrategyConfig) -> None:
        bars = make_series(200, drift=0.002, amplitude=0.015)
        snap = evaluate_gate(bars, cfg).snapshot
        assert snap.price_usd > 0
        assert snap.fx_rate > 0
        assert snap.krw_price == pytest.approx(snap.price_usd * snap.fx_rate)
        assert snap.atr_pct > 0
        assert snap.decay_annual <= 0
        assert 0 <= snap.rsi <= 100
        assert snap.format_table()

    def test_원화_목표가격이_현재가보다_높다(self, cfg: StrategyConfig) -> None:
        """원화 +5% 를 달성하려면 주가는 5% 이상 올라야 한다(비용 때문에)."""
        bars = make_series(200, drift=0.002, amplitude=0.015)
        snap = evaluate_gate(bars, cfg).snapshot
        assert snap.price_needed_tp1 > snap.price_usd * 1.05
        assert snap.price_needed_tp_last > snap.price_needed_tp1

    def test_환율이_빠지면_필요가격이_더_올라간다(self, cfg: StrategyConfig) -> None:
        """사용자 요구 '환율로 손해보지 않도록' 이 리포트에 정량적으로 드러나야 한다."""
        stable = evaluate_gate(make_series(200, drift=0.002, amplitude=0.015), cfg).snapshot
        # 환율이 빠지는 경우: 필요 상승률이 커진다 (같은 주가·환율 수준에서 비교)
        assert stable.price_needed_tp1 / stable.price_usd - 1 >= 0.05

    def test_변동성_감쇠가_변동성의_제곱에_비례한다(self, cfg: StrategyConfig) -> None:
        calm = evaluate_gate(make_series(200, drift=0.001, amplitude=0.01), cfg).snapshot
        wild = evaluate_gate(make_series(200, drift=0.001, amplitude=0.04), cfg).snapshot
        assert wild.decay_annual < calm.decay_annual


class TestVerdict:
    def test_고변동성_레짐은_AVOID다(self, cfg: StrategyConfig) -> None:
        """ATR 이 익절 목표보다 크면 전략이 성립하지 않는다."""
        bars = make_series(250, drift=0.001, amplitude=0.15)
        report = evaluate_gate(bars, cfg)
        assert report.verdict is Verdict.AVOID
        assert any("치명" in r for r in report.reasons)
        assert not report.allowed

    def test_하락추세는_최소한_WAIT다(self, cfg: StrategyConfig) -> None:
        bars = make_series(250, drift=-0.004, amplitude=0.01)
        report = evaluate_gate(bars, cfg)
        assert report.verdict in (Verdict.WAIT, Verdict.AVOID)

    def test_환율_급락은_경고로_보고된다(self, cfg: StrategyConfig) -> None:
        bars = make_series(250, drift=0.002, amplitude=0.01, fx_drift=-0.003)
        report = evaluate_gate(bars, cfg)
        assert any("환율" in r for r in report.reasons)

    def test_좋은_레짐에서는_ENTER가_나온다(self, cfg: StrategyConfig) -> None:
        """게이트가 무조건 막기만 하면 쓸모가 없다. 통과 경로도 존재해야 한다."""
        bars = make_series(240, drift=0.0022, amplitude=0.012)
        report = evaluate_gate(bars, cfg)
        assert report.verdict is Verdict.ENTER, report.reasons
        assert report.allowed
        assert report.planned_qty

    def test_리포트가_사람이_읽을_수_있다(self, cfg: StrategyConfig) -> None:
        bars = make_series(250, drift=0.001, amplitude=0.02)
        text = evaluate_gate(bars, cfg).format_report()
        for section in ("시장 레짐 진단", "진입 규칙 검사", "전방 시뮬레이션", "최종 판정"):
            assert section in text

    def test_모든_판정에_근거가_붙는다(self, cfg: StrategyConfig) -> None:
        for drift, amp in ((-0.004, 0.01), (0.0022, 0.012), (0.001, 0.15)):
            report = evaluate_gate(make_series(250, drift=drift, amplitude=amp), cfg)
            assert report.reasons
            assert all(isinstance(r, str) and r for r in report.reasons)


class TestConditionalSampling:
    def test_유사레짐_표본이_전체보다_작거나_같다(self, cfg: StrategyConfig) -> None:
        bars = make_series(300, drift=0.001, amplitude=0.02)
        report = evaluate_gate(bars, cfg)
        assert report.conditional.n <= report.unconditional.n

    def test_최근구간_표본이_제한된다(self, cfg: StrategyConfig) -> None:
        bars = make_series(400, drift=0.001, amplitude=0.02)
        report = evaluate_gate(bars, cfg, recent_days=60)
        assert report.recent.n <= report.unconditional.n

    def test_미리_계산한_표본을_재사용한다(self, cfg: StrategyConfig) -> None:
        """대용량 데이터에서 두 번 계산하지 않기 위한 경로."""
        bars = make_series(300, drift=0.001, amplitude=0.02)
        outcomes = scan_forward_outcomes(bars, cfg)
        report = evaluate_gate(bars, cfg, outcomes=outcomes)
        assert report.unconditional.n == len(outcomes)

    def test_표본이_적으면_주의를_남긴다(self, cfg: StrategyConfig) -> None:
        bars = make_series(60, drift=0.002, amplitude=0.01)
        report = evaluate_gate(bars, cfg, min_conditional_samples=1000)
        assert any("통계적 판단 근거가 약하다" in r for r in report.reasons)


class TestRealData:
    """실제 KORU 데이터에 대한 판정. 회귀 방지용 스냅샷 테스트."""

    def test_현재_레짐에서_진입이_막힌다(
        self, real_bars: tuple[Bar, ...], cfg: StrategyConfig
    ) -> None:
        """2026-08 기준 KORU 는 ATR 이 주가의 15% 로 극단적 고변동 구간이다.

        이 상태에서 게이트가 통과시키면 안전장치가 고장난 것이다.
        """
        report = evaluate_gate(real_bars, cfg)
        assert report.verdict is Verdict.AVOID
        assert not report.allowed

    def test_현재_ATR이_익절목표보다_크다(
        self, real_bars: tuple[Bar, ...], cfg: StrategyConfig
    ) -> None:
        snap = evaluate_gate(real_bars, cfg).snapshot
        assert snap.atr_pct > cfg.take_profit[0].krw_return

    def test_전방시뮬_표본이_충분히_모인다(
        self, real_bars: tuple[Bar, ...], cfg: StrategyConfig
    ) -> None:
        report = evaluate_gate(real_bars, cfg)
        assert report.unconditional.n > 300
        assert report.conditional.n > 0

    def test_리포트_전체가_렌더링된다(
        self, real_bars: tuple[Bar, ...], cfg: StrategyConfig
    ) -> None:
        text = evaluate_gate(real_bars, cfg).format_report()
        assert len(text) > 1000
        assert "AVOID" in text
