"""알림 인터페이스.

:class:`SignalNotifier` 프로토콜만 지키면 무엇이든 알림 채널이 될 수 있다
(텔레그램, 슬랙, 이메일, 로컬 알림). 매매 루프는 채널을 모른다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["NotifyResult", "NullNotifier", "SignalNotifier"]


@dataclass(frozen=True, slots=True)
class NotifyResult:
    """전송 결과.

    실패해도 예외를 던지지 않는다. 매매 루프가 알림 때문에 죽으면 안 되기 때문이다.
    호출자는 필요하면 :attr:`ok` 를 보고 판단한다.
    """

    ok: bool
    detail: str = ""
    skipped: bool = False
    """중복이거나 알림이 꺼져 있어 보내지 않은 경우."""

    @classmethod
    def sent(cls) -> NotifyResult:
        return cls(ok=True, detail="전송됨")

    @classmethod
    def skip(cls, why: str) -> NotifyResult:
        return cls(ok=True, detail=why, skipped=True)

    @classmethod
    def fail(cls, why: str) -> NotifyResult:
        return cls(ok=False, detail=why)


@runtime_checkable
class SignalNotifier(Protocol):
    """알림 채널이 지켜야 하는 최소 인터페이스."""

    @property
    def enabled(self) -> bool:
        """알림이 켜져 있는지. 자격증명이 없으면 False."""
        ...

    def send(self, text: str, *, dedupe_key: str | None = None) -> NotifyResult:
        """메시지를 보낸다.

        Args:
            text: 보낼 본문.
            dedupe_key: 같은 키로 이미 보냈으면 건너뛴다.
                같은 봉에서 같은 신호가 반복될 때 도배를 막는다.

        Returns:
            :class:`NotifyResult`. **예외를 던지지 않는다.**
        """
        ...


class NullNotifier:
    """아무것도 보내지 않는 알림기.

    알림을 설정하지 않았을 때의 기본값이다. 호출부가
    ``if notifier is not None`` 같은 분기를 두지 않아도 되게 한다.
    """

    @property
    def enabled(self) -> bool:
        return False

    def send(self, text: str, *, dedupe_key: str | None = None) -> NotifyResult:  # noqa: ARG002
        return NotifyResult.skip("알림이 설정되지 않았다")
