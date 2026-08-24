"""상태 영속화.

봇이 죽었다 살아났을 때 **포지션과 리스크 카운터를 정확히 복구**해야 한다.
복구에 실패하면 이미 보유 중인 포지션을 모른 채 새로 진입하거나,
당일 손실 한도를 초과한 상태에서 매매를 재개하게 된다.

저장 위치는 기본이 ``state/koru_state.db`` 이며 ``.gitignore`` 에 등록되어 있다.
이 파일에는 체결 이력이 들어가므로 **절대 커밋하면 안 된다.**
계좌번호나 API 키는 여기 저장하지 않는다.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from koru_trade.models import Lot, Position
from koru_trade.risk import RiskState

logger = logging.getLogger(__name__)

__all__ = ["StateStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS position (
    symbol           TEXT PRIMARY KEY,
    payload          TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk (
    trade_date       TEXT PRIMARY KEY,
    payload          TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submitted_orders (
    client_order_id  TEXT PRIMARY KEY,
    broker_order_id  TEXT,
    side             TEXT NOT NULL,
    qty              INTEGER NOT NULL,
    limit_price      REAL,
    status           TEXT NOT NULL,
    submitted_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT NOT NULL,
    action           TEXT NOT NULL,
    qty              INTEGER NOT NULL,
    price_usd        REAL,
    fx_rate          REAL,
    krw_return       REAL,
    rationale        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
"""


class StateStore:
    """SQLite 기반 상태 저장소.

    Args:
        path: DB 파일 경로. ``:memory:`` 를 주면 메모리 DB(테스트용).
    """

    def __init__(self, path: str | Path = "state/koru_state.db") -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._memory_conn: sqlite3.Connection | None = None
        if self._path == ":memory:":
            # 메모리 DB 는 연결이 끊기면 사라지므로 하나를 계속 들고 있는다.
            self._memory_conn = sqlite3.connect(self._path)
            self._memory_conn.row_factory = sqlite3.Row
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self._memory_conn is not None:
            yield self._memory_conn
            self._memory_conn.commit()
            return
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def close(self) -> None:
        """메모리 DB 연결을 닫는다. 파일 DB 는 매 호출마다 닫히므로 할 일이 없다."""
        if self._memory_conn is not None:
            self._memory_conn.close()
            self._memory_conn = None

    # -- 포지션 -------------------------------------------------------------

    def save_position(self, position: Position) -> None:
        """포지션을 저장한다. 수량이 0이면 삭제한다."""
        with self._connect() as conn:
            if not position.is_open:
                conn.execute("DELETE FROM position WHERE symbol = ?", (position.symbol,))
                return
            conn.execute(
                "INSERT INTO position(symbol, payload, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET payload=excluded.payload, "
                "updated_at=excluded.updated_at",
                (
                    position.symbol,
                    json.dumps(_position_to_dict(position), ensure_ascii=False),
                    dt.datetime.now().isoformat(),
                ),
            )

    def load_position(self, symbol: str) -> Position:
        """저장된 포지션을 복구한다. 없으면 빈 포지션."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM position WHERE symbol = ?", (symbol,)
            ).fetchone()
        if row is None:
            return Position(symbol)
        return _position_from_dict(json.loads(row["payload"]))

    # -- 리스크 -------------------------------------------------------------

    def save_risk(self, state: RiskState) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO risk(trade_date, payload, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(trade_date) DO UPDATE SET payload=excluded.payload, "
                "updated_at=excluded.updated_at",
                (
                    state.trade_date.isoformat(),
                    json.dumps(_risk_to_dict(state), ensure_ascii=False),
                    dt.datetime.now().isoformat(),
                ),
            )

    def load_risk(self, trade_date: dt.date) -> RiskState:
        """해당 거래일의 리스크 상태를 복구한다.

        같은 날짜 기록이 없으면 **가장 최근 기록에서 연속손실·수동정지만 이어받는다.**
        일일 집계는 0에서 시작한다.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM risk WHERE trade_date = ?", (trade_date.isoformat(),)
            ).fetchone()
            if row is not None:
                return _risk_from_dict(json.loads(row["payload"]))
            prev = conn.execute(
                "SELECT payload FROM risk ORDER BY trade_date DESC LIMIT 1"
            ).fetchone()
        if prev is None:
            return RiskState(trade_date=trade_date)
        return _risk_from_dict(json.loads(prev["payload"])).rolled_to(trade_date)

    # -- 주문 멱등성 --------------------------------------------------------

    def record_order(
        self,
        client_order_id: str,
        *,
        broker_order_id: str,
        side: str,
        qty: int,
        limit_price: float | None,
        status: str,
    ) -> None:
        """제출한 주문을 기록한다. 재시작 후에도 멱등성이 유지된다."""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO submitted_orders"
                "(client_order_id, broker_order_id, side, qty, limit_price, status, submitted_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    client_order_id,
                    broker_order_id,
                    side,
                    qty,
                    limit_price,
                    status,
                    dt.datetime.now().isoformat(),
                ),
            )

    def has_order(self, client_order_id: str) -> bool:
        """이 멱등키로 이미 제출한 주문이 있는지."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM submitted_orders WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        return row is not None

    # -- 감사 추적 ----------------------------------------------------------

    def log_decision(
        self,
        *,
        ts: dt.datetime,
        action: str,
        qty: int,
        price_usd: float,
        fx_rate: float,
        krw_return: float,
        rationale: str,
    ) -> None:
        """판단 근거를 남긴다. 사후에 "왜 그때 샀나" 를 답할 수 있어야 한다."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO audit(ts, action, qty, price_usd, fx_rate, krw_return, rationale)"
                " VALUES(?,?,?,?,?,?,?)",
                (ts.isoformat(), action, qty, price_usd, fx_rate, krw_return, rationale),
            )

    def recent_decisions(self, limit: int = 50) -> list[dict[str, Any]]:
        """최근 판단 기록."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, action, qty, price_usd, fx_rate, krw_return, rationale "
                "FROM audit ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 직렬화
# ---------------------------------------------------------------------------


def _position_to_dict(p: Position) -> dict[str, Any]:
    return {
        "symbol": p.symbol,
        "realized_krw": p.realized_krw,
        "tp_levels_hit": sorted(p.tp_levels_hit),
        "peak_krw_return": p.peak_krw_return,
        "ladder_base_qty": p.ladder_base_qty,
        "entry_atr_usd": p.entry_atr_usd,
        "planned_tranche_qty": list(p.planned_tranche_qty),
        "lots": [
            {
                "lot_id": lot.lot_id,
                "qty": lot.qty,
                "price_usd": lot.price_usd,
                "fx_rate": lot.fx_rate,
                "cost_krw": lot.cost_krw,
                "opened_at": lot.opened_at.isoformat(),
                "tranche_index": lot.tranche_index,
            }
            for lot in p.lots
        ],
    }


def _position_from_dict(d: dict[str, Any]) -> Position:
    return Position(
        symbol=d["symbol"],
        lots=tuple(
            Lot(
                lot_id=x["lot_id"],
                qty=int(x["qty"]),
                price_usd=float(x["price_usd"]),
                fx_rate=float(x["fx_rate"]),
                cost_krw=float(x["cost_krw"]),
                opened_at=dt.datetime.fromisoformat(x["opened_at"]),
                tranche_index=int(x["tranche_index"]),
            )
            for x in d.get("lots", [])
        ),
        realized_krw=float(d.get("realized_krw", 0.0)),
        tp_levels_hit=frozenset(int(i) for i in d.get("tp_levels_hit", [])),
        peak_krw_return=float(d.get("peak_krw_return", 0.0)),
        ladder_base_qty=int(d.get("ladder_base_qty", 0)),
        entry_atr_usd=float(d.get("entry_atr_usd", 0.0)),
        planned_tranche_qty=tuple(int(q) for q in d.get("planned_tranche_qty", [])),
    )


def _risk_to_dict(s: RiskState) -> dict[str, Any]:
    return {
        "trade_date": s.trade_date.isoformat(),
        "realized_krw_today": s.realized_krw_today,
        "notional_krw_today": s.notional_krw_today,
        "entries_today": s.entries_today,
        "consecutive_losses": s.consecutive_losses,
        "manual_halt": s.manual_halt,
        "halt_reasons": list(s.halt_reasons),
        "cooldown_until": s.cooldown_until.isoformat() if s.cooldown_until else None,
    }


def _risk_from_dict(d: dict[str, Any]) -> RiskState:
    return RiskState(
        trade_date=dt.date.fromisoformat(d["trade_date"]),
        realized_krw_today=float(d.get("realized_krw_today", 0.0)),
        notional_krw_today=float(d.get("notional_krw_today", 0.0)),
        entries_today=int(d.get("entries_today", 0)),
        consecutive_losses=int(d.get("consecutive_losses", 0)),
        manual_halt=bool(d.get("manual_halt", False)),
        halt_reasons=tuple(d.get("halt_reasons", [])),
        cooldown_until=(
            dt.date.fromisoformat(d["cooldown_until"]) if d.get("cooldown_until") else None
        ),
    )
