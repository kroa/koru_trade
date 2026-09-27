"""콘솔 출력을 UTF-8 로 고정한다.

한국어 Windows 는 파이썬 출력이 파이프로 나갈 때(git 훅, 에디터 터미널, 작업 스케줄러)
인코딩을 cp949 로 잡는다. cp949 에는 한글은 있지만 ``—``(U+2014), ``≥``, ``×`` 같은
문자가 없어서, 이런 문자를 ``print`` 하는 순간 ``UnicodeEncodeError`` 로 프로그램이 죽는다.
실제로 커밋 훅이 돌리는 ``scripts/verify.py`` 가 이 이유로 죽어 커밋이 막혔고, 그때마다
사람이 ``PYTHONIOENCODING`` 을 설정해야 했다.

사람에게 맡기지 않는다. 진입점(``python -m koru_trade``, 사이트 생성기)이 시작할 때
:func:`use_utf8_console` 을 부른다. ``scripts/verify.py`` 는 패키지 없이도 돌아야 하므로
같은 일을 직접 한다.
"""

from __future__ import annotations

import os
import sys

__all__ = ["use_utf8_console"]


def use_utf8_console() -> None:
    """표준 출력·오류를 UTF-8 로 바꾸고, 자식 파이썬도 그렇게 돌게 한다.

    - 이미 UTF-8 이거나 ``reconfigure`` 가 없는 스트림(테스트 캡처 등)은 건드리지 않는다.
    - 인코딩할 수 없는 문자는 죽지 않고 ``?`` 로 바꾼다. 출력 한 줄 때문에 매매 루프나
      검증 게이트가 멈추면 안 된다.
    - 환경변수는 ``setdefault`` 로만 넣는다. 사용자가 일부러 정한 값은 존중한다.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            continue
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")
