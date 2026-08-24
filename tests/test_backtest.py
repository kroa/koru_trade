"""백테스트 엔진, 성과 지표, 전방 시뮬레이션 검증.

백테스트가 거짓말을 하는 경로는 대부분 체결 모델에 있다.
여기서는 그 경로를 하나씩 막는다.

* 미래 정보를 쓰지 않는다 (look-ahead)
* 현금과 원가가 보존된다
* 같은 봉에서 손절과 익절이 겹치면 손절이 이긴다 (보수적 가정)
* 결과가 결정론적이다
"""

from __future__ import annotations

import itertools
import math

import pytest
from tests.conftest import make_series

from koru_trade.backtest import compute_metrics, run_backtest
from koru_trade.backtest.metrics import _max_drawdown, _max_streak, _sharpe, _sortino
from koru_trade.backtest.pathsim import (
    Terminal,
    scan_forward_outcomes,
    simulate_forward,
    summarize_outcomes,
)
from koru_trade.backtest.walkforward import (
    bootstrap_confidence,
    format_sweep,
    format_walkforward,
    parameter_sweep,
    walk_forward,
)
from koru_trade.config import StrategyConfig
from koru_trade.models import Bar, ExitReason, OrderSide


class TestEngineGuards:
    def test_봉이_2개_미만이면_거부된다(self, cfg: StrategyConfig) -> None:
        with pytest.raises(ValueError, match="최소 2개"):
            run_backtest(make_series(1), cfg)

    def test_시간_역순_봉은_거부된다(self, cfg: StrategyConfig) -> None:
        bars = list(make_series(50))
        bars[10], bars[20] = bars[20], bars[10]
        with pytest.raises(ValueError, match="오름차순"):
            run_backtest(bars, cfg)

    def test_0이하_초기자산은_거부된다(self, cfg: StrategyConfig) -> None:
        with pytest.raises(ValueError, match="0보다 커야"):
            run_backtest(make_series(60), cfg, initial_capital_krw=0)


class TestEngineInvariants:
    def test_결과가_결정론적이다(self, loose_cfg: StrategyConfig) -> None:
        bars = make_series(200, drift=0.002, amplitude=0.02)
        first = run_backtest(bars, loose_cfg)
        second = run_backtest(bars, loose_cfg)
        assert len(first.trades) == len(second.trades)
        assert first.final_equity_krw == pytest.approx(second.final_equity_krw)
        assert [d.action for d in first.decisions] == [d.action for d in second.decisions]

    def test_매수_수량과_매도_수량이_일치한다(self, loose_cfg: StrategyConfig) -> None:
        """포지션이 최종적으로 청산되므로 총 매수 = 총 매도여야 한다."""
        bars = make_series(250, drift=0.002, amplitude=0.03)
        res = run_backtest(bars, loose_cfg)
        bought = sum(f.qty for f in res.fills if f.side is OrderSide.BUY)
        sold = sum(f.qty for f in res.fills if f.side is OrderSide.SELL)
        assert bought == sold

    def test_현금_증감의_합이_최종자산과_맞는다(self, loose_cfg: StrategyConfig) -> None:
        bars = make_series(250, drift=0.002, amplitude=0.03)
        res = run_backtest(bars, loose_cfg)
        net = sum(f.cash_delta_krw for f in res.fills)
        assert res.final_equity_krw == pytest.approx(res.initial_capital_krw + net, rel=1e-6)

    def test_매매기록의_손익합이_최종자산_변화와_같다(self, loose_cfg: StrategyConfig) -> None:
        bars = make_series(250, drift=0.002, amplitude=0.03)
        res = run_backtest(bars, loose_cfg)
        total_pnl = sum(t.pnl_krw for t in res.trades)
        assert res.final_equity_krw - res.initial_capital_krw == pytest.approx(total_pnl, rel=1e-6)

    def test_모든_봉에_자산곡선_점이_있다(self, loose_cfg: StrategyConfig) -> None:
        bars = make_series(120, drift=0.002)
        res = run_backtest(bars, loose_cfg)
        assert len(res.equity_curve) == res.bars_processed

    def test_마지막에_포지션이_남지_않는다(self, loose_cfg: StrategyConfig) -> None:
        bars = make_series(250, drift=0.002, amplitude=0.03)
        res = run_backtest(bars, loose_cfg)
        bought = sum(f.qty for f in res.fills if f.side is OrderSide.BUY)
        sold = sum(f.qty for f in res.fills if f.side is OrderSide.SELL)
        assert bought == sold  # 강제 청산이 동작했다는 뜻

    def test_모든_판단에_근거가_기록된다(self, loose_cfg: StrategyConfig) -> None:
        res = run_backtest(make_series(120, drift=0.002), loose_cfg)
        assert all(d.rationale for d in res.decisions)


class TestNoLookAhead:
    def test_미래_봉을_붙여도_과거_결정이_바뀌지_않는다(self, loose_cfg: StrategyConfig) -> None:
        """look-ahead bias 를 잡는 가장 직접적인 테스트.

        200봉으로 돌린 결과의 앞 150봉 구간 결정은,
        150봉만 주고 돌린 결과와 완전히 같아야 한다.
        """
        full = make_series(200, drift=0.002, amplitude=0.02)
        short = full[:150]

        res_full = run_backtest(full, loose_cfg)
        res_short = run_backtest(short, loose_cfg)

        n = len(res_short.decisions) - 1  # 마지막 봉은 강제청산이 섞이므로 제외
        for i in range(n):
            a, b = res_full.decisions[i], res_short.decisions[i]
            assert a.ts == b.ts
            assert a.action is b.action, f"{i}번째 봉에서 결정이 달라졌다"
            assert a.qty == b.qty


class TestFillModel:
    def test_지정가보다_높게_열리면_미체결로_취소된다(self, loose_cfg: StrategyConfig) -> None:
        """매수 지정가는 시가나 저가가 그 가격 이하여야 체결된다.

        매 봉이 전일 종가보다 크게 갭업해서 열리면 지정가가 닿지 않는다.
        """
        import datetime as dt

        from tests.conftest import BASE_TS

        bars: list[Bar] = []
        price = 20.0
        for i in range(200):
            gap_open = price * 1.03  # 전일 종가 대비 +3% 갭업
            close = gap_open * 1.01
            bars.append(
                Bar(
                    ts=BASE_TS + dt.timedelta(days=i),
                    open=gap_open,
                    high=close * 1.002,
                    low=gap_open * 0.999,  # 저가도 지정가 위
                    close=close,
                    volume=5_000_000,
                    fx_rate=1400.0,
                )
            )
            price = close
        res = run_backtest(bars, loose_cfg)
        assert res.unfilled_orders > 0

    def test_손절이_익절보다_먼저_체결된다(self, loose_cfg: StrategyConfig) -> None:
        """같은 봉에서 손절선과 익절선을 모두 스치면 손절로 처리한다.

        진입 후 하루 만에 고가는 +30%, 저가는 -30% 인 봉을 만들어 확인한다.
        """
        import datetime as dt

        base = list(make_series(loose_cfg.warmup_bars + 2, drift=0.002, amplitude=0.005))
        last_ts = base[-1].ts
        wild = Bar(
            ts=last_ts + dt.timedelta(days=1),
            open=base[-1].close,
            high=base[-1].close * 1.35,
            low=base[-1].close * 0.60,
            close=base[-1].close * 0.95,
            volume=5_000_000,
            fx_rate=base[-1].fx_rate,
        )
        follow = Bar(
            ts=wild.ts + dt.timedelta(days=1),
            open=wild.close,
            high=wild.close * 1.01,
            low=wild.close * 0.99,
            close=wild.close,
            volume=5_000_000,
            fx_rate=wild.fx_rate,
        )
        res = run_backtest([*base, wild, follow], loose_cfg)
        stops = [
            f
            for f in res.fills
            if f.exit_reason in (ExitReason.HARD_STOP_USD, ExitReason.HARD_STOP_KRW)
        ]
        tps = [f for f in res.fills if f.exit_reason is ExitReason.TAKE_PROFIT_LADDER]
        if stops or tps:
            assert stops, "손절과 익절이 겹친 봉에서 익절이 먼저 났다(낙관 편향)"


class TestMetrics:
    def test_매매가_없으면_모든_지표가_0이다(self) -> None:
        rep = compute_metrics([], [], 1_000_000)
        assert rep.n_trades == 0
        assert rep.win_rate == 0
        assert rep.profit_factor == 0
        assert not rep.is_viable
        assert "한 건도 발생하지 않았다" in rep.verdict

    def test_최대낙폭이_정확하다(self) -> None:
        import datetime as dt

        curve = [
            (dt.datetime(2026, 1, 1), 100.0),
            (dt.datetime(2026, 1, 2), 120.0),
            (dt.datetime(2026, 1, 12), 60.0),
            (dt.datetime(2026, 1, 20), 90.0),
        ]
        dd, days = _max_drawdown(curve)
        assert dd == pytest.approx(-0.5)
        assert days == 10

    def test_최대_연속손실이_계산된다(self, loose_cfg: StrategyConfig) -> None:
        from koru_trade.models import TradeRecord

        def trade(pnl: float) -> TradeRecord:
            import datetime as dt

            return TradeRecord(
                "KORU",
                dt.datetime(2026, 1, 1),
                dt.datetime(2026, 1, 2),
                1,
                10,
                20.0,
                20.0,
                1400.0,
                1400.0,
                1_000_000,
                1_000_000 + pnl,
                (),
            )

        trades = [trade(x) for x in (-1, -1, 1, -1, -1, -1, 1)]
        assert _max_streak(trades) == 3

    def test_변동이_없으면_샤프가_0이다(self) -> None:
        assert _sharpe([0.0] * 10) == 0.0
        assert _sortino([0.0] * 10) == 0.0
        assert _sharpe([]) == 0.0

    def test_하락이_없으면_소르티노가_0이다(self) -> None:
        assert _sortino([0.01] * 10) == 0.0

    def test_실데이터_지표가_전부_유한하다(
        self, real_bars: tuple[Bar, ...], cfg: StrategyConfig
    ) -> None:
        res = run_backtest(real_bars, cfg)
        rep = compute_metrics(res.trades, res.equity_curve, res.initial_capital_krw)
        for value in (
            rep.total_return,
            rep.cagr,
            rep.max_drawdown,
            rep.sharpe,
            rep.sortino,
            rep.calmar,
            rep.expectancy_krw,
        ):
            assert math.isfinite(value)
        assert rep.format_table()
        assert rep.verdict

    def test_판정_문자열이_문제를_구체적으로_적는다(
        self, real_bars: tuple[Bar, ...], cfg: StrategyConfig
    ) -> None:
        res = run_backtest(real_bars, cfg)
        rep = compute_metrics(res.trades, res.equity_curve, res.initial_capital_krw)
        if not rep.is_viable:
            assert "실거래 부적합" in rep.verdict


class TestPathSim:
    def test_진입_인덱스가_범위_밖이면_None이다(self, cfg: StrategyConfig) -> None:
        bars = make_series(100, drift=0.002)
        assert simulate_forward(bars, 0, cfg) is None
        assert simulate_forward(bars, 999, cfg) is None

    def test_지표_이력이_부족하면_None이다(self, cfg: StrategyConfig) -> None:
        assert simulate_forward(make_series(10), 5, cfg) is None

    def test_상승장에서는_익절로_끝난다(self, loose_cfg: StrategyConfig) -> None:
        bars = make_series(120, drift=0.02, amplitude=0.005)
        out = simulate_forward(bars, 60, loose_cfg)
        assert out is not None
        assert out.terminal in (Terminal.TP_FULL, Terminal.TIMEOUT_PARTIAL)
        assert out.krw_return > 0

    def test_하락장에서는_손절로_끝난다(self, loose_cfg: StrategyConfig) -> None:
        bars = make_series(120, drift=-0.03, amplitude=0.005)
        out = simulate_forward(bars, 60, loose_cfg)
        assert out is not None
        assert out.terminal is Terminal.STOP
        assert out.krw_return < 0

    def test_MFE는_MAE보다_항상_크거나_같다(self, cfg: StrategyConfig) -> None:
        bars = make_series(200, drift=0.001, amplitude=0.02)
        for o in scan_forward_outcomes(bars, cfg, stride=7):
            assert o.max_favorable_krw >= o.max_adverse_krw

    def test_데이터_끝_표본은_제외된다(self, cfg: StrategyConfig) -> None:
        bars = make_series(200, drift=0.001, amplitude=0.02)
        assert all(o.terminal is not Terminal.DATA_END for o in scan_forward_outcomes(bars, cfg))

    def test_stride가_0이하면_거부된다(self, cfg: StrategyConfig) -> None:
        with pytest.raises(ValueError, match="stride"):
            scan_forward_outcomes(make_series(100), cfg, stride=0)

    def test_빈_표본_요약이_안전하다(self) -> None:
        stats = summarize_outcomes([], "빈 표본")
        assert stats.n == 0
        assert stats.win_rate == 0.0
        assert stats.format_table()

    def test_요약_비율의_합이_1을_넘지_않는다(self, cfg: StrategyConfig) -> None:
        bars = make_series(300, drift=0.001, amplitude=0.02)
        stats = summarize_outcomes(scan_forward_outcomes(bars, cfg, stride=3))
        if stats.n:
            total = stats.tp_full_rate + stats.stop_rate + stats.timeout_rate
            assert total == pytest.approx(1.0, abs=1e-9)

    def test_분위수가_순서를_지킨다(self, cfg: StrategyConfig) -> None:
        bars = make_series(300, drift=0.001, amplitude=0.02)
        stats = summarize_outcomes(scan_forward_outcomes(bars, cfg, stride=3))
        if stats.n > 4:
            assert stats.p05 <= stats.p25 <= stats.median_krw <= stats.p75 <= stats.p95

    def test_실데이터_전방시뮬이_동작한다(
        self, real_bars: tuple[Bar, ...], cfg: StrategyConfig
    ) -> None:
        outcomes = scan_forward_outcomes(real_bars, cfg, stride=5)
        assert len(outcomes) > 50
        stats = summarize_outcomes(outcomes)
        assert math.isfinite(stats.expectancy)
        assert 0.0 <= stats.win_rate <= 1.0


class TestWalkForward:
    def test_구간이_2개_미만이면_거부된다(
        self, real_bars: tuple[Bar, ...], cfg: StrategyConfig
    ) -> None:
        with pytest.raises(ValueError, match="n_folds"):
            walk_forward(real_bars, cfg, n_folds=1)

    def test_데이터가_부족하면_거부된다(self, cfg: StrategyConfig) -> None:
        with pytest.raises(ValueError, match="봉이 부족"):
            walk_forward(make_series(50), cfg, n_folds=4)

    def test_구간이_요청한_수만큼_나온다(
        self, real_bars: tuple[Bar, ...], cfg: StrategyConfig
    ) -> None:
        folds = walk_forward(real_bars, cfg, n_folds=3)
        assert len(folds) == 3
        assert format_walkforward(folds)

    def test_구간이_시간순으로_이어진다(
        self, real_bars: tuple[Bar, ...], cfg: StrategyConfig
    ) -> None:
        folds = walk_forward(real_bars, cfg, n_folds=4)
        for a, b in itertools.pairwise(folds):
            assert a.end_label <= b.start_label


class TestBootstrap:
    def test_표본이_2개_미만이면_0을_반환한다(self) -> None:
        result = bootstrap_confidence([0.05])
        assert result.mean == 0.0
        assert result.ci_low == 0.0

    def test_시드가_같으면_결과가_같다(self) -> None:
        data = [0.05, -0.10, 0.09, -0.03, 0.14, 0.02, -0.08]
        a = bootstrap_confidence(data, n_sims=500, seed=42)
        b = bootstrap_confidence(data, n_sims=500, seed=42)
        assert a.ci_low == b.ci_low
        assert a.ruin_probability == b.ruin_probability

    def test_신뢰구간이_평균을_감싼다(self) -> None:
        data = [0.05, -0.10, 0.09, -0.03, 0.14, 0.02, -0.08, 0.06, 0.01]
        r = bootstrap_confidence(data, n_sims=1000)
        assert r.ci_low <= r.mean <= r.ci_high

    def test_전부_손실이면_양수확률이_0이다(self) -> None:
        r = bootstrap_confidence([-0.05] * 20, n_sims=300)
        assert r.prob_positive == 0.0
        assert r.ci_high < 0

    def test_파산확률이_0과_1_사이다(self) -> None:
        data = [0.05, -0.30, 0.09, -0.25, 0.14]
        r = bootstrap_confidence(data, n_sims=500)
        assert 0.0 <= r.ruin_probability <= 1.0
        assert r.format_table()

    def test_무한대나_NaN은_걸러진다(self) -> None:
        r = bootstrap_confidence([0.05, math.inf, math.nan, -0.03], n_sims=200)
        assert r.n_samples == 2


class TestSweep:
    def test_민감도표가_생성된다(self, real_bars: tuple[Bar, ...], cfg: StrategyConfig) -> None:
        rows = parameter_sweep(real_bars[-400:], cfg)
        assert len(rows) > 10
        assert format_sweep(rows)
        assert {r.parameter for r in rows} >= {
            "max_atr_pct",
            "stop_atr_multiple",
            "max_holding_days",
        }
