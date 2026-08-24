"""로컬 대시보드 HTTP 서버.

표준 라이브러리만 쓴다. Flask/FastAPI 를 넣지 않은 이유는 두 가지다.

1. 이 저장소는 실거래 봇이다. 의존성이 늘수록 공급망 위험이 늘고,
   ``pip install`` 한 번으로 돌아야 할 것이 안 돌게 된다.
2. 대시보드는 라우트가 세 개뿐이다. 프레임워크가 필요한 규모가 아니다.

보안
----
* **바인딩은 127.0.0.1 고정이다.** 포지션과 손익은 개인 금융정보다.
  ``0.0.0.0`` 으로 열면 같은 네트워크(카페 와이파이 등)의 아무나 볼 수 있다.
  :func:`serve` 는 루프백이 아닌 host 를 거부한다.
* 읽기 전용이다. 이 서버로는 주문을 낼 수 없다. POST 라우트가 아예 없다.
* 자격증명은 스냅샷에 담기지 않는다.
"""

from __future__ import annotations

import contextlib
import http.server
import ipaddress
import json
import logging
import socket
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from koru_trade.config import StrategyConfig
from koru_trade.live.state import StateStore
from koru_trade.models import Bar
from koru_trade.web.snapshot import build_snapshot, snapshot_to_json

logger = logging.getLogger(__name__)

__all__ = ["DashboardServer", "find_free_port", "is_loopback", "is_port_free", "serve"]

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_PORT = 8642
DEFAULT_HOST = "127.0.0.1"
DEFAULT_TTL = 120.0
"""스냅샷 캐시 수명(초). 백테스트를 매 요청마다 다시 돌리면 화면이 느려진다."""


def is_loopback(host: str) -> bool:
    """루프백 주소인지. 대시보드는 여기에만 열린다.

    >>> is_loopback("127.0.0.1")
    True
    >>> is_loopback("0.0.0.0")
    False
    """
    if host in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _apply_exclusive(sock: socket.socket) -> None:
    """포트를 **독점적으로** 잡도록 소켓 옵션을 건다.

    Windows 의 ``SO_REUSEADDR`` 은 유닉스와 의미가 다르다. 유닉스에서는
    TIME_WAIT 상태의 포트를 재사용하게 해줄 뿐이지만, **Windows 에서는 이미
    다른 프로세스가 LISTEN 중인 포트에도 바인딩이 성공한다.**
    빈 포트를 찾는다면서 남이 쓰는 포트를 빼앗게 된다.

    그래서 Windows 에서는 ``SO_EXCLUSIVEADDRUSE`` 를 건다.
    """
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if exclusive is not None:  # Windows
        sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)


def is_port_free(port: int, host: str = DEFAULT_HOST) -> bool:
    """포트가 실제로 비어 있는지 확인한다.

    두 가지를 모두 본다. 하나만으로는 부족하다.

    1. **바인딩**이 되는가 (``SO_REUSEADDR`` 없이 — 위 :func:`_apply_exclusive` 참조)
    2. 그 포트로 **연결이 되는가** — 되면 누군가 LISTEN 중이다
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        if probe.connect_ex((host, port)) == 0:
            return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        _apply_exclusive(sock)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def find_free_port(preferred: int = DEFAULT_PORT, host: str = DEFAULT_HOST) -> int:
    """``preferred`` 부터 위로 훑어 비어 있는 포트를 찾는다.

    사용자가 여러 프로그램을 띄워 둔 상태를 가정한다.
    선호 포트가 막혀 있으면 조용히 다음 것을 쓴다.

    Args:
        preferred: 먼저 시도할 포트.
        host: 바인딩할 호스트.

    Returns:
        비어 있는 포트 번호.

    Raises:
        OSError: 64개를 훑어도 빈 포트가 없을 때.
    """
    for candidate in range(preferred, preferred + 64):
        if candidate > 65535:
            break
        if is_port_free(candidate, host):
            if candidate != preferred:
                logger.info("포트 %d 가 사용 중이라 %d 를 쓴다", preferred, candidate)
            return candidate
    raise OSError(f"{preferred} 부터 64개 포트를 훑었으나 빈 포트가 없다")


class _ExclusiveHTTPServer(http.server.ThreadingHTTPServer):
    """남의 포트를 빼앗지 않는 HTTP 서버.

    ``ThreadingHTTPServer`` 는 ``allow_reuse_address = 1`` 이 기본이라
    Windows 에서 이미 LISTEN 중인 포트를 가로챌 수 있다.
    """

    allow_reuse_address = False

    def server_bind(self) -> None:
        _apply_exclusive(self.socket)
        super().server_bind()


class _SnapshotCache:
    """스냅샷을 TTL 동안 재사용한다. 백테스트는 몇 초씩 걸린다."""

    def __init__(
        self,
        builder: Callable[[], str],
        ttl: float = DEFAULT_TTL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._builder = builder
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._payload: str | None = None
        self._built_at = 0.0

    def get(self, *, refresh: bool = False) -> str:
        with self._lock:
            now = self._clock()
            fresh = self._payload is not None and (now - self._built_at) < self._ttl
            if fresh and not refresh:
                return self._payload  # type: ignore[return-value]
            logger.info("대시보드 스냅샷을 새로 만든다")
            self._payload = self._builder()
            self._built_at = now
            return self._payload


class _Handler(http.server.BaseHTTPRequestHandler):
    """라우팅. 클래스 속성으로 서버 인스턴스를 주입받는다."""

    cache: _SnapshotCache
    server_version = "koru-dashboard"
    sys_version = ""

    def do_GET(self) -> None:
        path, _, query = self.path.partition("?")
        if path in ("/", "/index.html"):
            self._send_file(STATIC_DIR / "dashboard.html", "text/html; charset=utf-8")
        elif path == "/api/snapshot":
            self._send_snapshot(refresh="refresh=1" in query)
        elif path == "/api/health":
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "not found", "path": path}, status=404)

    def _send_snapshot(self, *, refresh: bool) -> None:
        try:
            payload = self.cache.get(refresh=refresh)
        except Exception as exc:
            logger.exception("스냅샷 생성 실패")
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
            return
        self._send_bytes(payload.encode("utf-8"), "application/json; charset=utf-8")

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            self._send_json({"error": f"파일이 없다: {path.name}"}, status=404)
            return
        self._send_bytes(data, content_type)

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status=status)

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 로컬 전용 대시보드다. 외부 리소스를 전혀 쓰지 않으므로 CSP 를 좁게 건다.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; img-src data:; connect-src 'self'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """기본 stderr 로깅 대신 로거로 보낸다(요청 경로만)."""
        logger.debug("%s - %s", self.address_string(), format % args)


class DashboardServer:
    """대시보드 서버.

    Args:
        bars: 표시할 봉 시계열.
        cfg: 전략 설정.
        store: 실거래 상태 저장소(선택).
        host: 바인딩 호스트. 루프백이 아니면 거부한다.
        port: 포트. 사용 중이면 다음 빈 포트로 넘어간다.
        ttl: 스냅샷 캐시 수명(초).
    """

    def __init__(
        self,
        bars: Sequence[Bar],
        cfg: StrategyConfig,
        *,
        store: StateStore | None = None,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        ttl: float = DEFAULT_TTL,
    ) -> None:
        if not is_loopback(host):
            raise ValueError(
                f"대시보드는 루프백에만 열 수 있다: {host!r}. "
                "포지션과 손익은 개인 금융정보이므로 외부에 노출하면 안 된다"
            )
        self._bars = tuple(bars)
        self._cfg = cfg
        self._store = store
        self.host = host
        self.port = find_free_port(port, host)

        cache = _SnapshotCache(self._build, ttl=ttl)
        handler = type("_BoundHandler", (_Handler,), {"cache": cache})
        self._httpd = _ExclusiveHTTPServer((host, self.port), handler)
        self._httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    def _build(self) -> str:
        return snapshot_to_json(build_snapshot(self._bars, self._cfg, store=self._store))

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def start(self) -> str:
        """백그라운드 스레드에서 서버를 띄우고 URL 을 반환한다."""
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="koru-dashboard", daemon=True
        )
        self._thread.start()
        logger.info("대시보드를 열었다: %s", self.url)
        return self.url

    def stop(self) -> None:
        """서버를 내린다."""
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def serve_forever(self) -> None:
        """포그라운드로 서버를 돌린다. Ctrl+C 로 중단한다."""
        logger.info("대시보드를 열었다: %s", self.url)
        try:
            self._httpd.serve_forever()
        except KeyboardInterrupt:
            logger.info("중단 요청을 받았다")
        finally:
            self._httpd.server_close()

    def __enter__(self) -> DashboardServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


def serve(
    bars: Sequence[Bar],
    cfg: StrategyConfig,
    *,
    store: StateStore | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = False,
) -> None:
    """대시보드를 포그라운드로 띄운다. CLI 에서 쓴다."""
    server = DashboardServer(bars, cfg, store=store, host=host, port=port)
    if open_browser:
        import webbrowser

        threading.Timer(0.6, lambda: webbrowser.open(server.url)).start()
    server.serve_forever()
