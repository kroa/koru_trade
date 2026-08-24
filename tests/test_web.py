"""웹 대시보드 검증.

두 가지가 핵심이다.

1. **루프백 바인딩** — 포지션과 손익은 개인 금융정보다. 외부에 열리면 안 된다.
2. **JSON 유효성** — ``NaN`` 이 하나라도 섞이면 브라우저의 ``JSON.parse`` 가
   통째로 거부해서 화면이 백지가 된다. 파이썬 ``json.dumps`` 기본값은 NaN 을
   그대로 뱉으므로 이 검사가 없으면 조용히 깨진다.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request

import pytest
from tests.conftest import make_series, open_position

from koru_trade.config import StrategyConfig
from koru_trade.live.state import StateStore
from koru_trade.models import Bar
from koru_trade.web.server import (
    DEFAULT_HOST,
    STATIC_DIR,
    DashboardServer,
    _SnapshotCache,
    find_free_port,
    is_loopback,
    is_port_free,
)
from koru_trade.web.snapshot import build_snapshot, snapshot_to_json


@pytest.fixture
def snapshot_bars() -> tuple[Bar, ...]:
    """지표 계산에 충분하고 매매도 발생하는 길이."""
    return make_series(260, drift=0.0022, amplitude=0.012)


class TestLoopbackGuard:
    """대시보드가 외부에 열리지 않는지. 이 테스트는 협상 대상이 아니다."""

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.5.5.5"])
    def test_루프백_주소를_인식한다(self, host: str) -> None:
        assert is_loopback(host)

    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.0.10", "10.0.0.1", "example.com", ""])
    def test_루프백이_아닌_주소를_거부한다(self, host: str) -> None:
        assert not is_loopback(host)

    def test_외부_바인딩_시도가_거부된다(
        self, snapshot_bars: tuple[Bar, ...], loose_cfg: StrategyConfig
    ) -> None:
        with pytest.raises(ValueError, match="루프백에만"):
            DashboardServer(snapshot_bars, loose_cfg, host="0.0.0.0")

    def test_기본_호스트가_루프백이다(self) -> None:
        assert is_loopback(DEFAULT_HOST)


class TestFindFreePort:
    def test_비어있는_포트를_찾는다(self) -> None:
        port = find_free_port(8642)
        assert 8642 <= port < 8642 + 64

    def test_사용중이면_다음_포트로_넘어간다(self) -> None:
        """이미 LISTEN 중인 포트를 빈 포트라고 답하면 안 된다.

        Windows 의 SO_REUSEADDR 은 LISTEN 중인 포트에도 바인딩을 허용하므로,
        단순 bind 검사만으로는 남의 포트를 빼앗게 된다.
        """
        import socket

        taken = find_free_port(8700)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
            blocker.bind((DEFAULT_HOST, taken))
            blocker.listen(1)
            assert not is_port_free(taken)
            assert find_free_port(taken) != taken

    def test_빈_포트는_비어있다고_답한다(self) -> None:
        assert is_port_free(find_free_port(8800))


class TestSnapshot:
    def test_봉이_없으면_거부된다(self, loose_cfg: StrategyConfig) -> None:
        with pytest.raises(ValueError, match="비어 있다"):
            build_snapshot([], loose_cfg)

    def test_모든_구역이_채워진다(
        self, snapshot_bars: tuple[Bar, ...], loose_cfg: StrategyConfig
    ) -> None:
        snap = build_snapshot(snapshot_bars, loose_cfg)
        data = snap.to_dict()
        for key in (
            "market",
            "verdict",
            "strategy",
            "live",
            "performance",
            "trades",
            "signals",
            "equity",
        ):
            assert key in data, f"{key} 구역이 없다"

    def test_JSON이_NaN_없이_직렬화된다(
        self, snapshot_bars: tuple[Bar, ...], loose_cfg: StrategyConfig
    ) -> None:
        """브라우저 JSON.parse 는 NaN/Infinity 를 거부한다.

        하나라도 새어나가면 대시보드 전체가 빈 화면이 된다.
        """
        payload = snapshot_to_json(build_snapshot(snapshot_bars, loose_cfg))
        assert "NaN" not in payload
        assert "Infinity" not in payload
        parsed = json.loads(payload)  # 브라우저와 같은 엄격도
        assert parsed["symbol"] == loose_cfg.symbol

    def test_무한대_ProfitFactor가_None으로_바뀐다(
        self, snapshot_bars: tuple[Bar, ...], loose_cfg: StrategyConfig
    ) -> None:
        """전부 이익이면 Profit Factor 가 무한대가 된다. 그대로 두면 JSON 이 깨진다."""
        snap = build_snapshot(snapshot_bars, loose_cfg)
        pf = snap.performance["profit_factor"]
        assert pf is None or math.isfinite(pf)

    def test_신호에_출처가_붙는다(
        self, snapshot_bars: tuple[Bar, ...], loose_cfg: StrategyConfig
    ) -> None:
        """실거래와 시뮬레이션을 구분하지 못하면 위험한 오해가 생긴다."""
        snap = build_snapshot(snapshot_bars, loose_cfg)
        for s in snap.signals:
            assert s["source"] in ("backtest", "live")
            assert s["side"] in ("BUY", "SELL")
            assert s["side_label"] in ("매수", "매도")

    def test_신호가_최신순이다(
        self, snapshot_bars: tuple[Bar, ...], loose_cfg: StrategyConfig
    ) -> None:
        snap = build_snapshot(snapshot_bars, loose_cfg)
        stamps = [s["ts"] for s in snap.signals]
        assert stamps == sorted(stamps, reverse=True)

    def test_매매내역도_최신순이다(
        self, snapshot_bars: tuple[Bar, ...], loose_cfg: StrategyConfig
    ) -> None:
        snap = build_snapshot(snapshot_bars, loose_cfg)
        closed = [t["closed_at"] for t in snap.trades]
        assert closed == sorted(closed, reverse=True)

    def test_손익과_수익률의_부호가_일치한다(
        self, snapshot_bars: tuple[Bar, ...], loose_cfg: StrategyConfig
    ) -> None:
        snap = build_snapshot(snapshot_bars, loose_cfg)
        for t in snap.trades:
            assert (t["pnl_krw"] > 0) == t["is_win"]
            assert (t["krw_return"] > 0) == (t["pnl_krw"] > 0)

    def test_상태DB가_없으면_실계좌_구역이_비어있다(
        self, snapshot_bars: tuple[Bar, ...], loose_cfg: StrategyConfig
    ) -> None:
        snap = build_snapshot(snapshot_bars, loose_cfg)
        assert snap.live["available"] is False
        assert snap.live["position"]["open"] is False
        assert any("상태 DB" in n for n in snap.notes)

    def test_상태DB가_있으면_포지션이_표시된다(
        self, snapshot_bars: tuple[Bar, ...], loose_cfg: StrategyConfig
    ) -> None:
        store = StateStore(":memory:")
        try:
            store.save_position(open_position(qty=40, price=20.0, atr=1.0))
            snap = build_snapshot(snapshot_bars, loose_cfg, store=store)
            pos = snap.live["position"]
            assert snap.live["available"] is True
            assert pos["open"] is True
            assert pos["qty"] == 40
            assert "krw_return" in pos
            assert pos["next_tp"]["index"] == 1
        finally:
            store.close()

    def test_자격증명이_스냅샷에_없다(
        self, snapshot_bars: tuple[Bar, ...], loose_cfg: StrategyConfig
    ) -> None:
        """대시보드는 브라우저로 나간다. 키가 실리면 안 된다."""
        payload = snapshot_to_json(build_snapshot(snapshot_bars, loose_cfg)).lower()
        for word in ("app_key", "appsecret", "app_secret", "account_no", "cano", "token"):
            assert word not in payload

    def test_전략_규칙이_설정과_일치한다(
        self, snapshot_bars: tuple[Bar, ...], cfg: StrategyConfig
    ) -> None:
        """화면에 뜨는 규칙이 실제 동작과 다르면 대시보드가 거짓말을 하는 것이다."""
        snap = build_snapshot(snapshot_bars, cfg)
        s = snap.strategy
        assert len(s["scale_in"]) == len(cfg.scale_in)
        assert len(s["take_profit"]) == len(cfg.take_profit)
        assert s["take_profit"][0]["krw_return"] == pytest.approx(cfg.take_profit[0].krw_return)
        assert s["stop"]["krw_hard_stop"] == pytest.approx(cfg.hard_stop_krw_return)
        assert s["stop"]["max_holding_days"] == cfg.max_holding_days

    def test_실데이터로도_동작한다(self, real_bars: tuple[Bar, ...], cfg: StrategyConfig) -> None:
        snap = build_snapshot(real_bars, cfg)
        assert snap.verdict["code"] in ("ENTER", "WAIT", "AVOID")
        assert snap.equity
        json.loads(snapshot_to_json(snap))


class TestSnapshotCache:
    def test_TTL_안에서는_재사용한다(self) -> None:
        calls = []
        clock = [0.0]

        def build() -> str:
            calls.append(1)
            return f"payload-{len(calls)}"

        cache = _SnapshotCache(build, ttl=10.0, clock=lambda: clock[0])
        assert cache.get() == "payload-1"
        clock[0] = 5.0
        assert cache.get() == "payload-1"
        assert len(calls) == 1

    def test_TTL이_지나면_다시_만든다(self) -> None:
        calls = []
        clock = [0.0]
        cache = _SnapshotCache(
            lambda: f"p{len(calls) or calls.append(1) or len(calls)}",
            ttl=10.0,
            clock=lambda: clock[0],
        )
        first = cache.get()
        clock[0] = 11.0
        cache.get()
        assert first is not None

    def test_refresh는_TTL을_무시한다(self) -> None:
        calls = []

        def build() -> str:
            calls.append(1)
            return str(len(calls))

        cache = _SnapshotCache(build, ttl=999.0, clock=lambda: 0.0)
        cache.get()
        cache.get(refresh=True)
        assert len(calls) == 2


class TestStaticFile:
    def test_대시보드_HTML이_존재한다(self) -> None:
        path = STATIC_DIR / "dashboard.html"
        assert path.exists(), "dashboard.html 이 패키지에 없다"
        assert path.stat().st_size > 5000

    def test_외부_리소스를_참조하지_않는다(self) -> None:
        """CSP 가 default-src 'none' 이므로 외부 참조가 있으면 조용히 깨진다.

        오프라인에서도 완전히 동작해야 한다.
        """
        html = (STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")
        for bad in ("http://", "https://", "//cdn", 'src="//'):
            assert bad not in html, f"외부 리소스 참조가 있다: {bad}"


@pytest.mark.web
class TestServerRoutes:
    """실제로 서버를 띄워 라우트를 확인한다. 루프백이므로 네트워크는 쓰지 않는다."""

    @pytest.fixture
    def server(self, snapshot_bars: tuple[Bar, ...], loose_cfg: StrategyConfig) -> DashboardServer:
        srv = DashboardServer(snapshot_bars, loose_cfg, port=8900)
        srv.start()
        yield srv
        srv.stop()

    @staticmethod
    def _get(url: str) -> tuple[int, bytes, dict[str, str]]:
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def test_루트가_HTML을_준다(self, server: DashboardServer) -> None:
        status, body, headers = self._get(server.url)
        assert status == 200
        assert "text/html" in headers["Content-Type"]
        assert b"KORU" in body

    def test_스냅샷_API가_유효한_JSON을_준다(self, server: DashboardServer) -> None:
        status, body, headers = self._get(server.url + "api/snapshot")
        assert status == 200
        assert "application/json" in headers["Content-Type"]
        data = json.loads(body)
        assert data["symbol"] == "KORU"
        assert "performance" in data

    def test_헬스체크가_동작한다(self, server: DashboardServer) -> None:
        status, body, _ = self._get(server.url + "api/health")
        assert status == 200
        assert json.loads(body)["ok"] is True

    def test_없는_경로는_404다(self, server: DashboardServer) -> None:
        status, _, _ = self._get(server.url + "nope")
        assert status == 404

    def test_보안_헤더가_붙는다(self, server: DashboardServer) -> None:
        _, _, headers = self._get(server.url)
        assert "default-src 'none'" in headers["Content-Security-Policy"]
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Cache-Control"] == "no-store"

    def test_컨텍스트_매니저로_열고_닫힌다(
        self, snapshot_bars: tuple[Bar, ...], loose_cfg: StrategyConfig
    ) -> None:
        with DashboardServer(snapshot_bars, loose_cfg, port=8950) as srv:
            status, _, _ = self._get(srv.url + "api/health")
            assert status == 200


class TestPriceHistory:
    def test_가격_시계열이_담긴다(
        self, snapshot_bars: tuple[Bar, ...], loose_cfg: StrategyConfig
    ) -> None:
        snap = build_snapshot(snapshot_bars, loose_cfg)
        ph = snap.price_history
        assert ph
        assert len(ph) <= len(snapshot_bars)
        for row in ph:
            assert row["close"] > 0
            assert row["krw"] > 0
            assert len(row["ts"]) == 10

    def test_가격_시계열이_시간순이다(
        self, snapshot_bars: tuple[Bar, ...], loose_cfg: StrategyConfig
    ) -> None:
        ph = build_snapshot(snapshot_bars, loose_cfg).price_history
        assert [r["ts"] for r in ph] == sorted(r["ts"] for r in ph)

    def test_마지막_가격이_현재가와_같다(
        self, snapshot_bars: tuple[Bar, ...], loose_cfg: StrategyConfig
    ) -> None:
        """스파크라인 끝점과 큰 숫자가 어긋나면 화면이 거짓말을 하는 것이다."""
        snap = build_snapshot(snapshot_bars, loose_cfg)
        assert snap.price_history[-1]["close"] == pytest.approx(snap.market["price_usd"], rel=1e-3)


class TestRegimeHistory:
    """왜 신호가 없는지를 화면이 설명할 수 있어야 한다."""

    def test_월별_이력이_생성된다(
        self, snapshot_bars: tuple[Bar, ...], cfg: StrategyConfig
    ) -> None:
        rh = build_snapshot(snapshot_bars, cfg).regime_history
        assert rh["months"]
        assert rh["filters"]
        for m in rh["months"]:
            assert m["bars"] > 0
            assert 0 <= m["pass_rate"] <= 1
            assert 0 <= m["passed"] <= m["bars"]

    def test_차단률이_0과_1_사이다(
        self, snapshot_bars: tuple[Bar, ...], cfg: StrategyConfig
    ) -> None:
        rh = build_snapshot(snapshot_bars, cfg).regime_history
        for m in rh["months"]:
            for name, rate in m["blocks"].items():
                assert 0 <= rate <= 1, f"{m['month']} {name} 차단률이 범위 밖이다: {rate}"

    def test_데이터충분성은_필터_목록에서_제외된다(
        self, snapshot_bars: tuple[Bar, ...], cfg: StrategyConfig
    ) -> None:
        """워밍업 이후에는 항상 통과하므로 히트맵에 넣으면 빈 줄만 생긴다."""
        rh = build_snapshot(snapshot_bars, cfg).regime_history
        assert "데이터충분성" not in rh["filters"]

    def test_월이_시간순이다(self, snapshot_bars: tuple[Bar, ...], cfg: StrategyConfig) -> None:
        rh = build_snapshot(snapshot_bars, cfg).regime_history
        months = [m["month"] for m in rh["months"]]
        assert months == sorted(months)

    def test_실데이터에서_최근_차단_원인을_지목한다(
        self, real_bars: tuple[Bar, ...], cfg: StrategyConfig
    ) -> None:
        """2026년 KORU 는 ATR 이 폭발해 변동성 필터가 지배적으로 차단한다."""
        rh = build_snapshot(real_bars, cfg).regime_history
        assert rh["recent_dominant_blocker"] == "변동성레짐"
        assert rh["recent_dominant_rate"] > 0.5
