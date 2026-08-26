"""텔레그램 알림 채널.

봇 토큰은 URL 경로에 들어간다(``/bot<TOKEN>/sendMessage``). 그래서
**요청 URL 을 절대 로그나 예외 메시지에 넣지 않는다.** requests 가 던지는 예외에는
URL 이 통째로 들어 있으므로 그대로 올리면 토큰이 로그에 남는다.
:meth:`TelegramNotifier.send` 가 모든 예외를 잡아 타입 이름만 남기는 이유다.

설정
----
환경변수 두 개만 있으면 된다. ``.env`` 에 넣고 절대 커밋하지 않는다.

* ``TELEGRAM_BOT_TOKEN`` — @BotFather 에서 발급
* ``TELEGRAM_CHAT_ID``   — 봇에게 아무 메시지나 보낸 뒤
  ``getUpdates`` 로 확인하거나 @userinfobot 으로 조회
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass

import requests

from koru_trade.broker.ratelimit import RateLimiter
from koru_trade.config import mask_secret
from koru_trade.notify.base import NotifyResult

logger = logging.getLogger(__name__)

__all__ = ["TelegramConfig", "TelegramNotifier", "load_telegram_config"]

API_BASE = "https://api.telegram.org"
MAX_TEXT = 4096
"""텔레그램 메시지 길이 상한."""

DEDUPE_MEMORY = 400
"""중복 판정에 기억할 최근 키 개수."""


@dataclass(frozen=True)
class TelegramConfig:
    """텔레그램 자격증명.

    ``__repr__`` 이 마스킹되어 있다. print 한 줄로 토큰이 새는 것을 막는다.
    ``slots`` 을 쓰지 않는 이유는 repr 재정의를 명확히 제어하기 위해서다.
    """

    bot_token: str
    chat_id: str

    def __post_init__(self) -> None:
        if not self.bot_token or not self.chat_id:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN 과 TELEGRAM_CHAT_ID 가 모두 필요하다. .env.example 을 참고하라"
            )
        if ":" not in self.bot_token:
            raise ValueError("봇 토큰 형식이 아니다(숫자ID:문자열 이어야 한다)")

    def __repr__(self) -> str:
        return (
            f"TelegramConfig(bot_token={mask_secret(self.bot_token, 6)}, "
            f"chat_id={mask_secret(self.chat_id, 3)})"
        )

    __str__ = __repr__


def load_telegram_config(env: dict[str, str] | None = None) -> TelegramConfig | None:
    """환경변수에서 텔레그램 설정을 읽는다.

    Returns:
        설정. 둘 중 하나라도 없으면 **None** (알림을 끄고 계속 진행한다).
        알림이 없다고 매매를 막지는 않는다.
    """
    if env is None:
        env = dict(os.environ)
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = env.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        return None
    try:
        return TelegramConfig(bot_token=token, chat_id=chat)
    except ValueError as exc:
        logger.warning("텔레그램 설정이 올바르지 않아 알림을 끈다: %s", exc)
        return None


class TelegramNotifier:
    """텔레그램 봇 API 로 메시지를 보낸다.

    Args:
        config: 자격증명.
        timeout: HTTP 타임아웃(초). 짧게 잡는다 — 알림 때문에 매매 루프가
            멈춰 있으면 안 된다.
        session: 테스트용 주입구.
        rate_limit: 초당 최대 전송 수. 텔레그램은 같은 채팅에 분당 20건을 넘기면 제한한다.
    """

    def __init__(
        self,
        config: TelegramConfig,
        *,
        timeout: float = 6.0,
        session: requests.Session | None = None,
        rate_limit: int = 1,
    ) -> None:
        self._cfg = config
        self._timeout = timeout
        self._session = session or requests.Session()
        self._limiter = RateLimiter(max(1, rate_limit), 1.0)
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return True

    def _url(self, method: str) -> str:
        """API URL. **이 문자열을 로그에 남기지 마라. 토큰이 들어 있다.**"""
        return f"{API_BASE}/bot{self._cfg.bot_token}/{method}"

    def _is_duplicate(self, key: str) -> bool:
        with self._lock:
            if key in self._seen:
                return True
            self._seen[key] = None
            while len(self._seen) > DEDUPE_MEMORY:
                self._seen.popitem(last=False)
            return False

    def send(self, text: str, *, dedupe_key: str | None = None) -> NotifyResult:
        """메시지를 보낸다. **어떤 경우에도 예외를 던지지 않는다.**

        매매 루프에서 호출되므로, 여기서 예외가 새어 나가면 알림 장애가
        곧 매매 장애가 된다. 실패는 전부 결과 객체로 돌려준다.
        """
        if dedupe_key and self._is_duplicate(dedupe_key):
            logger.debug("중복 알림을 건너뛴다: %s", dedupe_key)
            return NotifyResult.skip("같은 신호를 이미 보냈다")

        body = text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"
        try:
            self._limiter.acquire(timeout=5.0)
            resp = self._session.post(
                self._url("sendMessage"),
                json={
                    "chat_id": self._cfg.chat_id,
                    "text": body,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=self._timeout,
            )
        except TimeoutError:
            logger.warning("텔레그램 전송 유량 대기 시간을 초과했다")
            return NotifyResult.fail("유량 대기 초과")
        except Exception as exc:
            # 예외 문자열에 URL(=토큰)이 들어갈 수 있어 타입 이름만 남긴다.
            logger.warning("텔레그램 전송 실패: %s", type(exc).__name__)
            return NotifyResult.fail(f"전송 실패: {type(exc).__name__}")

        if resp.status_code != 200:
            desc = ""
            with contextlib.suppress(ValueError):
                desc = str(resp.json().get("description", ""))[:200]
            logger.warning("텔레그램 응답 오류 HTTP %d %s", resp.status_code, desc)
            return NotifyResult.fail(f"HTTP {resp.status_code} {desc}".strip())

        return NotifyResult.sent()

    def check(self) -> NotifyResult:
        """자격증명이 살아 있는지 확인한다(``getMe``). 설정 점검용."""
        try:
            self._limiter.acquire(timeout=5.0)
            resp = self._session.get(self._url("getMe"), timeout=self._timeout)
        except Exception as exc:
            return NotifyResult.fail(f"연결 실패: {type(exc).__name__}")
        if resp.status_code != 200:
            return NotifyResult.fail(f"HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            return NotifyResult.fail("응답이 JSON 이 아니다")
        name = str((data.get("result") or {}).get("username", "?"))
        return NotifyResult(ok=True, detail=f"봇 @{name} 연결 확인")
