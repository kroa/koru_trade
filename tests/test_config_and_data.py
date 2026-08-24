"""설정 로딩과 데이터 적재 검증.

설정에서 오타 난 키가 조용히 무시되면 "설정했다고 믿었는데 기본값으로 돌던" 사고가 난다.
데이터 로더는 **조정주가**와 환율 정렬이 핵심이다. KORU 는 최근 3년 안에
역분할(1:10)과 액면분할(20:1)을 모두 겪었으므로 미조정 주가는 백테스트를 완전히 망친다.
"""

from __future__ import annotations

import datetime as dt
import itertools

import pytest
from tests.conftest import make_series

from koru_trade.config import (
    StrategyConfig,
    load_strategy_config,
    strategy_config_from_dict,
)
from koru_trade.data.loader import (
    DataUnavailableError,
    bars_from_frame,
    load_bars_from_csv,
    save_bars_to_csv,
)
from koru_trade.models import Bar


class TestStrategyConfigRoundTrip:
    def test_직렬화_후_복원하면_동일하다(self) -> None:
        original = StrategyConfig()
        restored = strategy_config_from_dict(original.to_dict())
        assert restored == original

    def test_수정된_설정도_왕복한다(self) -> None:
        from dataclasses import replace

        original = replace(
            StrategyConfig(), max_holding_days=3, stop_atr_multiple=1.5, min_adx=25.0
        )
        assert strategy_config_from_dict(original.to_dict()) == original

    def test_알_수_없는_키는_거부된다(self) -> None:
        """오타가 조용히 무시되면 안 된다."""
        data = StrategyConfig().to_dict()
        data["max_atr_pcnt"] = 0.05  # 오타
        with pytest.raises(ValueError, match="알 수 없는 설정 키"):
            strategy_config_from_dict(data)

    def test_비용_하위키_오타도_거부된다(self) -> None:
        data = StrategyConfig().to_dict()
        data["cost"]["buy_fee"] = 0.001
        with pytest.raises(ValueError, match="알 수 없는 cost 키"):
            strategy_config_from_dict(data)

    def test_리스크_하위키_오타도_거부된다(self) -> None:
        data = StrategyConfig().to_dict()
        data["risk"]["daily_loss"] = 1
        with pytest.raises(ValueError, match="알 수 없는 risk 키"):
            strategy_config_from_dict(data)


class TestYamlLoading:
    def test_경로가_없으면_기본값이다(self) -> None:
        assert load_strategy_config(None) == StrategyConfig()

    def test_없는_파일은_거부된다(self, tmp_path) -> None:
        with pytest.raises(FileNotFoundError, match="설정 파일이 없다"):
            load_strategy_config(tmp_path / "nope.yaml")

    def test_YAML을_읽어_설정을_만든다(self, tmp_path) -> None:
        import yaml

        path = tmp_path / "strategy.yaml"
        data = StrategyConfig().to_dict()
        data["max_holding_days"] = 3
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        assert load_strategy_config(path).max_holding_days == 3

    def test_최상위가_사전이_아니면_거부된다(self, tmp_path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("- 1\n- 2\n", encoding="utf-8")
        with pytest.raises(ValueError, match="최상위는 사전"):
            load_strategy_config(path)

    def test_빈_YAML은_기본값이_된다(self, tmp_path) -> None:
        path = tmp_path / "empty.yaml"
        path.write_text("", encoding="utf-8")
        assert load_strategy_config(path) == StrategyConfig()


class TestCsvRoundTrip:
    def test_저장하고_읽으면_동일하다(self, tmp_path) -> None:
        bars = make_series(50, drift=0.002, amplitude=0.01)
        path = save_bars_to_csv(bars, tmp_path / "bars.csv")
        loaded = load_bars_from_csv(path)
        assert len(loaded) == len(bars)
        for a, b in zip(bars, loaded, strict=True):
            assert a.ts == b.ts
            assert a.close == pytest.approx(b.close, rel=1e-6)
            assert a.fx_rate == pytest.approx(b.fx_rate, rel=1e-6)

    def test_없는_파일은_거부된다(self, tmp_path) -> None:
        with pytest.raises(DataUnavailableError, match="CSV 가 없다"):
            load_bars_from_csv(tmp_path / "nope.csv")

    def test_빈_CSV는_거부된다(self, tmp_path) -> None:
        path = tmp_path / "empty.csv"
        path.write_text("ts,open,high,low,close,volume,fx_rate\n", encoding="utf-8")
        with pytest.raises(DataUnavailableError, match="봉이 없다"):
            load_bars_from_csv(path)

    def test_순서가_뒤섞여도_정렬되어_읽힌다(self, tmp_path) -> None:
        bars = list(make_series(10))
        path = tmp_path / "shuffled.csv"
        save_bars_to_csv(list(reversed(bars)), path)
        loaded = load_bars_from_csv(path)
        assert [b.ts for b in loaded] == sorted(b.ts for b in bars)


class TestBarsFromFrame:
    @staticmethod
    def _frame(n: int = 10):
        pd = pytest.importorskip("pandas")
        idx = pd.date_range("2026-01-01", periods=n, freq="D")
        return pd.DataFrame(
            {
                "Open": [20.0 + i * 0.1 for i in range(n)],
                "High": [20.5 + i * 0.1 for i in range(n)],
                "Low": [19.5 + i * 0.1 for i in range(n)],
                "Close": [20.2 + i * 0.1 for i in range(n)],
                "Volume": [1_000_000] * n,
            },
            index=idx,
        )

    def test_환율이_주가_날짜에_맞춰_정렬된다(self) -> None:
        pd = pytest.importorskip("pandas")
        px = self._frame(10)
        # 환율은 3일치만 있다 -> 나머지는 직전 값으로 이월되어야 한다
        fx = pd.Series(
            [1400.0, 1410.0, 1420.0],
            index=pd.date_range("2026-01-01", periods=3, freq="D"),
        )
        bars = bars_from_frame(px, fx)
        assert len(bars) == 10
        assert bars[2].fx_rate == pytest.approx(1420.0)
        assert bars[9].fx_rate == pytest.approx(1420.0)  # 이월

    def test_환율이_없는_앞부분은_버려진다(self) -> None:
        """backfill 은 미래 정보이므로 절대 하지 않는다. 대신 버린다."""
        pd = pytest.importorskip("pandas")
        px = self._frame(10)
        fx = pd.Series(
            [1400.0, 1410.0],
            index=pd.date_range("2026-01-05", periods=2, freq="D"),
        )
        bars = bars_from_frame(px, fx)
        assert len(bars) == 6  # 1/5 부터
        assert bars[0].ts.date() == dt.date(2026, 1, 5)

    def test_환율이_전혀_없으면_거부된다(self) -> None:
        pd = pytest.importorskip("pandas")
        px = self._frame(5)
        fx = pd.Series([1400.0], index=pd.date_range("2027-01-01", periods=1, freq="D"))
        with pytest.raises(DataUnavailableError, match="유효한 봉이 하나도 없다"):
            bars_from_frame(px, fx)

    def test_중복_날짜는_마지막_값만_남는다(self) -> None:
        pd = pytest.importorskip("pandas")
        px = self._frame(5)
        px = pd.concat([px, px.iloc[[0]]])
        fx = pd.Series([1400.0] * 5, index=pd.date_range("2026-01-01", periods=5, freq="D"))
        bars = bars_from_frame(px, fx)
        assert len({b.ts for b in bars}) == len(bars)

    def test_타임존이_제거된다(self) -> None:
        pd = pytest.importorskip("pandas")
        px = self._frame(5)
        px.index = px.index.tz_localize("America/New_York")
        fx = pd.Series(
            [1400.0] * 5,
            index=pd.date_range("2026-01-01", periods=5, freq="D", tz="Asia/Seoul"),
        )
        bars = bars_from_frame(px, fx)
        assert all(b.ts.tzinfo is None for b in bars)

    def test_결측치가_있는_행은_건너뛴다(self) -> None:
        pd = pytest.importorskip("pandas")
        import numpy as np

        px = self._frame(5)
        px.iloc[2, px.columns.get_loc("Close")] = np.nan
        fx = pd.Series([1400.0] * 5, index=pd.date_range("2026-01-01", periods=5, freq="D"))
        bars = bars_from_frame(px, fx)
        assert len(bars) == 4


class TestRealFixture:
    """고정해 둔 실데이터 스냅샷의 무결성."""

    def test_시간순으로_정렬되어_있다(self, real_bars: tuple[Bar, ...]) -> None:
        for a, b in itertools.pairwise(real_bars):
            assert a.ts < b.ts

    def test_모든_봉이_유효하다(self, real_bars: tuple[Bar, ...]) -> None:
        for b in real_bars:
            assert b.low <= b.high
            assert b.close > 0
            assert b.fx_rate > 0

    def test_분할_구간에_비현실적_점프가_없다(self, real_bars: tuple[Bar, ...]) -> None:
        """조정주가를 썼다면 20:1 분할일에 -95% 짜리 봉이 없어야 한다.

        미조정 주가로 백테스트하면 그 하루가 전체 결과를 지배해 버린다.
        """
        for a, b in itertools.pairwise(real_bars):
            ratio = b.close / a.close
            assert 0.4 < ratio < 2.5, (
                f"{b.ts.date()} 에 {ratio:.2f}배 점프가 있다. 조정주가를 쓰지 않은 것으로 보인다"
            )

    def test_환율이_현실적_범위다(self, real_bars: tuple[Bar, ...]) -> None:
        for b in real_bars:
            assert 800 < b.fx_rate < 2500


class TestShippedPresets:
    """저장소에 포함된 예시 설정이 실제로 로딩되는지.

    문서에만 있고 깨진 설정 파일은 사용자를 오도한다.
    """

    @pytest.mark.parametrize("name", ["strategy.example.yaml", "daytrade.example.yaml"])
    def test_예시_설정이_로딩된다(self, name: str) -> None:
        from pathlib import Path as _P

        path = _P(__file__).resolve().parent.parent / "config" / name
        assert path.exists(), f"{name} 이 없다"
        cfg = load_strategy_config(path)
        assert cfg.symbol == "KORU"

    def test_단타_프리셋이_기본과_두_항목만_다르다(self) -> None:
        """프리셋 문서가 '딱 두 가지만 바꿨다' 고 주장한다. 실제로 그런지 확인한다."""
        from dataclasses import fields
        from pathlib import Path as _P

        base = StrategyConfig()
        day = load_strategy_config(
            _P(__file__).resolve().parent.parent / "config" / "daytrade.example.yaml"
        )
        diff = {
            f.name for f in fields(StrategyConfig) if getattr(base, f.name) != getattr(day, f.name)
        }
        assert diff == {"max_holding_days", "max_atr_pct"}, f"예상 밖의 차이: {diff}"
        assert day.max_holding_days == 1
        assert day.max_atr_pct == pytest.approx(0.20)

    def test_단타_프리셋의_익절_계단이_기본과_같다(self) -> None:
        """계단을 압축하면 기대값이 무너진다는 실측 근거를 코드로 고정한다."""
        from pathlib import Path as _P

        day = load_strategy_config(
            _P(__file__).resolve().parent.parent / "config" / "daytrade.example.yaml"
        )
        assert day.take_profit == StrategyConfig().take_profit
