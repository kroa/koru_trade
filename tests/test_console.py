"""콘솔 UTF-8 고정 검증.

한국어 Windows 에서 파이썬 출력이 파이프로 나가면 cp949 로 잡혀, ``—`` 같은 문자 하나에
``UnicodeEncodeError`` 로 죽는다. 커밋 훅이 이 이유로 막혔다. 사람이 환경변수를 설정하게
두지 않고 진입점이 스스로 고치는지 확인한다.
"""

from __future__ import annotations

import io
import os
import sys

import pytest

from koru_trade.console import use_utf8_console


class _Stream:
    """``reconfigure`` 호출을 기록하는 가짜 스트림."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, str]] = []
        self._fail = fail

    def reconfigure(self, **kw: str) -> None:
        if self._fail:
            raise ValueError("이미 쓰기 시작한 스트림")
        self.calls.append(kw)


class TestUseUtf8Console:
    def test_표준출력과_오류를_UTF8_로_바꾼다(self, monkeypatch: pytest.MonkeyPatch) -> None:
        out, err = _Stream(), _Stream()
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)
        use_utf8_console()
        assert out.calls == [{"encoding": "utf-8", "errors": "replace"}]
        assert err.calls == [{"encoding": "utf-8", "errors": "replace"}]

    def test_cp949_로_죽던_문자가_출력된다(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """실제 사고의 재현: cp949 파이프에 '—' 를 쓰면 죽는다. 고친 뒤에는 안 죽는다."""
        buf = io.BytesIO()
        pipe = io.TextIOWrapper(buf, encoding="cp949")
        with pytest.raises(UnicodeEncodeError):
            pipe.write("KORU 하네스 검증 — 5개 게이트")
            pipe.flush()

        buf2 = io.BytesIO()
        pipe2 = io.TextIOWrapper(buf2, encoding="cp949")
        monkeypatch.setattr(sys, "stdout", pipe2)
        use_utf8_console()
        print("KORU 하네스 검증 — 5개 게이트")
        sys.stdout.flush()
        assert "—".encode() in buf2.getvalue()

    def test_reconfigure_가_없는_스트림은_넘어간다(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdout", object())
        monkeypatch.setattr(sys, "stderr", object())
        use_utf8_console()  # 예외 없이 끝나야 한다

    def test_바꿀_수_없는_스트림에서도_죽지_않는다(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdout", _Stream(fail=True))
        monkeypatch.setattr(sys, "stderr", _Stream(fail=True))
        use_utf8_console()

    def test_자식_프로세스용_환경변수를_넣는다(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PYTHONIOENCODING", raising=False)
        monkeypatch.delenv("PYTHONUTF8", raising=False)
        monkeypatch.setattr(sys, "stdout", _Stream())
        monkeypatch.setattr(sys, "stderr", _Stream())
        use_utf8_console()
        assert os.environ["PYTHONIOENCODING"] == "utf-8"
        assert os.environ["PYTHONUTF8"] == "1"

    def test_사용자가_정한_값은_덮어쓰지_않는다(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PYTHONIOENCODING", "latin-1")
        monkeypatch.setattr(sys, "stdout", _Stream())
        monkeypatch.setattr(sys, "stderr", _Stream())
        use_utf8_console()
        assert os.environ["PYTHONIOENCODING"] == "latin-1"
