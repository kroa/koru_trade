"""로컬 웹 대시보드.

매수/매도 신호 이력, 현재 전략 상태, 원화 수익률과 수익금을 브라우저로 본다.

**서버는 루프백(127.0.0.1)에만 바인딩한다.** 포지션과 손익은 개인 금융정보이므로
같은 네트워크의 다른 기기에서 접근 가능한 상태로 열어 두면 안 된다.
"""

from koru_trade.web.server import (
    DashboardServer,
    find_free_port,
    is_loopback,
    is_port_free,
    serve,
)
from koru_trade.web.snapshot import DashboardSnapshot, build_snapshot, snapshot_to_json

__all__ = [
    "DashboardServer",
    "DashboardSnapshot",
    "build_snapshot",
    "find_free_port",
    "is_loopback",
    "is_port_free",
    "serve",
    "snapshot_to_json",
]
