"""watch 루프의 오류 알림이 폭주하지 않는지 검증한다.

dedupe 키가 없으면 네트워크 장애가 지속될 때 주기마다 알림이 나간다.
15분 주기면 하루 96통이다. 이 테스트가 그 회귀를 막는다.
"""

from __future__ import annotations

import datetime as dt

from koru_trade.notify.base import NotifyResult, SignalNotifier


class _RecordingNotifier(SignalNotifier):
    """보낸 키를 기록하고 중복은 건너뛰는 최소 구현."""

    def __init__(self) -> None:
        self.sent: list[str | None] = []
        self._seen: set[str] = set()

    @property
    def enabled(self) -> bool:
        return True

    def send(self, text: str, *, dedupe_key: str | None = None) -> NotifyResult:
        if dedupe_key is not None and dedupe_key in self._seen:
            return NotifyResult.skip("중복")
        if dedupe_key is not None:
            self._seen.add(dedupe_key)
        self.sent.append(dedupe_key)
        return NotifyResult.sent()


def _error_key(exc: BaseException, day: dt.date) -> str:
    """cli._cmd_watch 가 쓰는 키와 같은 규칙."""
    return f"error-{type(exc).__name__}-{day.isoformat()}"


def test_같은_오류가_반복돼도_하루에_한_번만_알린다() -> None:
    n = _RecordingNotifier()
    day = dt.date(2026, 8, 30)
    exc = ConnectionError("네트워크 끊김")

    for _ in range(96):  # 15분 주기 하루치
        n.send("오류", dedupe_key=_error_key(exc, day))

    assert len(n.sent) == 1, f"하루 96회 실패에 알림이 {len(n.sent)}통 나갔다"


def test_다른_종류의_오류는_따로_알린다() -> None:
    n = _RecordingNotifier()
    day = dt.date(2026, 8, 30)

    n.send("a", dedupe_key=_error_key(ConnectionError("x"), day))
    n.send("b", dedupe_key=_error_key(ValueError("y"), day))
    n.send("c", dedupe_key=_error_key(ConnectionError("z"), day))

    assert len(n.sent) == 2


def test_날짜가_바뀌면_다시_알린다() -> None:
    n = _RecordingNotifier()
    exc = ConnectionError("계속 끊김")

    n.send("a", dedupe_key=_error_key(exc, dt.date(2026, 8, 30)))
    n.send("b", dedupe_key=_error_key(exc, dt.date(2026, 8, 31)))

    assert len(n.sent) == 2, "문제가 이어지면 다음 날 다시 알려야 한다"


def test_cli_가_실제로_dedupe_키를_넘긴다() -> None:
    """구현이 키를 빼먹는 회귀를 막는다."""
    import inspect

    from koru_trade import cli

    src = inspect.getsource(cli._cmd_watch)
    assert "dedupe_key=" in src, "watch 의 오류 알림에 dedupe_key 가 없다"
    assert "format_error" in src
