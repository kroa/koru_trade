"""매매 신호 알림.

봇이 매수/매도 신호를 냈을 때 텔레그램으로 알린다.

설계 원칙
---------
**알림 실패가 매매를 막으면 안 된다.** 텔레그램이 죽어도, 네트워크가 끊겨도,
토큰이 만료돼도 매매 루프는 그대로 돌아야 한다. 모든 전송 실패는 삼키고 로그만 남긴다.
반대는 성립하지 않는다 — 알림을 못 보내서 주문을 못 내는 상황은 없어야 한다.

봇 토큰은 :mod:`koru_trade.config` 의 자격증명과 같은 규칙을 따른다.
환경변수에서만 읽고, 파일에 저장하지 않고, 로그에 원문이 실리지 않는다.
"""

from koru_trade.notify.base import (
    NotifyResult,
    NullNotifier,
    SignalNotifier,
)
from koru_trade.notify.format import (
    format_blocked,
    format_daily_summary,
    format_decision,
    format_error,
)
from koru_trade.notify.telegram import TelegramConfig, TelegramNotifier, load_telegram_config

__all__ = [
    "NotifyResult",
    "NullNotifier",
    "SignalNotifier",
    "TelegramConfig",
    "TelegramNotifier",
    "format_blocked",
    "format_daily_summary",
    "format_decision",
    "format_error",
    "load_telegram_config",
]
